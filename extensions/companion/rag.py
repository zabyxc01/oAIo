"""RAG Router — waterfall enrichment across multiple knowledge sources.

Priority order:
  1. Knowledge docs (Open WebUI ChromaDB) — curated, highest signal
  2. Personal notes (local JSON) — user explicitly said "remember this"
  3. Episodic memory (local JSON) — structured conversation summaries
  4. Vision memory (local JSON) — screen descriptions (only for memory-referencing phrases)
  5. Web search (SearXNG) — only when local sources miss AND question is factual

Each source returns with provenance tags so the LLM knows where info came from.
"""
import asyncio
import re
from dataclasses import dataclass, field

import importlib.util as _ilu
from pathlib import Path

# Load sibling modules (not a package — extension loader uses spec_from_file_location)
def _load_module(name: str):
    spec = _ilu.spec_from_file_location(name, str(Path(__file__).parent / f"{name}.py"))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_notes_mod = _load_module("notes")
_episodes_mod = _load_module("episodes")
_vision_mod = _load_module("vision_memory")
_tools_mod = _load_module("tools")

NotesStore = _notes_mod.NotesStore
EpisodeStore = _episodes_mod.EpisodeStore
VisionMemoryStore = _vision_mod.VisionMemoryStore
search_web = _tools_mod.search_web


@dataclass
class RagResult:
    """Result from the RAG router."""
    source: str = ""          # "knowledge", "notes", "episodes", "vision", "web", ""
    docs: str = ""            # Formatted docs string for prompt injection
    confidence: float = 0.0   # 0.0-1.0
    git_context: str | None = None
    personal: bool = True     # True = personal RAG (keep persona), False = objective (strip persona)

# Sources classified by objectivity
_OBJECTIVE_SOURCES = {"knowledge", "web"}
_PERSONAL_SOURCES = {"notes", "episodes", "vision"}


# Patterns that indicate a factual question
# Factual lookup patterns — must look like an actual information request, not conversation
_FACTUAL_RE = re.compile(
    r'\b(what is|what are|what was|what were|what did|what does|what do|'
    r'who is|who are|who was|who won|who made|'
    r'when is|when was|when did|when does|'
    r'where is|where are|where was|where did|'
    r'how much|how many|how far|how long|how old|how does|how do you make|'
    r'which is|which are|'
    r'tell me about|explain|define|look up|search for|'
    r'price of|cost of|population of|capital of|weather in|temperature in)\b',
    re.IGNORECASE,
)

# Conversational patterns that should NOT trigger web search even if they match factual RE
_CONVERSATIONAL_RE = re.compile(
    r'\b(what do you (want|think|feel|like)|what are you|what should (we|i)|'
    r'how are you|how do you feel|why are you|why do you|'
    r'what.s (up|wrong|going on|happening)|what about you|'
    r'what are your thoughts|guess what)\b',
    re.IGNORECASE,
)

# Patterns that reference past memory / screen activity
_MEMORY_RE = re.compile(
    r'\b(remember when|earlier|what was i doing|what were we|last time|before|previously|recall)\b',
    re.IGNORECASE,
)


class RagRouter:
    """Waterfall RAG enrichment — queries sources in priority order, stops at first hit."""

    def __init__(self, knowledge_client=None, config: dict | None = None,
                 persona_id: str = ""):
        self._knowledge = knowledge_client
        self._config = config or {}
        self._persona_id = persona_id
        self._notes: NotesStore | None = None
        self._episodes: EpisodeStore | None = None
        self._vision: VisionMemoryStore | None = None

        if persona_id:
            self._notes = NotesStore(persona_id)
            self._episodes = EpisodeStore(persona_id)
            self._vision = VisionMemoryStore(persona_id)

    @property
    def notes(self) -> NotesStore | None:
        return self._notes

    @property
    def episodes(self) -> EpisodeStore | None:
        return self._episodes

    @property
    def vision(self) -> VisionMemoryStore | None:
        return self._vision

    def update_config(self, config: dict) -> None:
        self._config = config

    async def enrich(self, query: str, priority: int = 3,
                     context: str = "") -> RagResult:
        """Query sources in waterfall order, return first hit.

        Args:
            query: user's message text
            priority: persona priority dial (0-10)
            context: ambient/screen context
        """
        cfg = self._config
        chunk_chars = cfg.get("rag_chunk_chars", 800)
        max_results = cfg.get("rag_max_results", 3)
        min_priority = cfg.get("rag_min_priority", 2)

        result = RagResult()
        print(f"[rag] enrich: query='{query[:60]}' priority={priority} min={min_priority}")

        # Only run RAG if priority meets threshold
        if priority < min_priority:
            print(f"[rag] skipped: priority {priority} < min {min_priority}")
            return result

        # ── 1. Knowledge docs (ChromaDB) ──
        print(f"[rag] 1/5 knowledge: client={'YES' if self._knowledge else 'NO'} enabled={cfg.get('rag_knowledge_enabled', True)}")
        if self._knowledge and cfg.get("rag_knowledge_enabled", True):
            try:
                hits = await self._knowledge.query(query, n_results=max_results)
                print(f"[rag] 1/5 knowledge: {len(hits)} hits")
                if hits:
                    docs = "\n".join(f"- {r.text[:chunk_chars]}" for r in hits if r.text)
                    if docs:
                        result.source = "knowledge"
                        result.personal = False  # objective
                        result.docs = self._format_docs("knowledge", docs)
                        result.confidence = 1.0 - (hits[0].score if hits else 1.0)
                        print(f"[rag] HIT knowledge ({len(hits)} docs, best={hits[0].score:.3f})")
                        return self._maybe_add_git(result, priority, cfg)
            except Exception as e:
                print(f"[rag] 1/5 knowledge FAILED: {e}")

        # ── 2. Personal notes ──
        print(f"[rag] 2/5 notes: store={'YES' if self._notes else 'NO'} enabled={cfg.get('rag_notes_enabled', True)}")
        if self._notes and cfg.get("rag_notes_enabled", True):
            notes = self._notes.search(query, max_results=max_results)
            print(f"[rag] 2/5 notes: {len(notes)} matches")
            if notes:
                docs = "\n".join(
                    f"- {n['key']}: {n['value']} (saved {self._relative_time(n.get('ts', 0))})"
                    for n in notes
                )
                result.source = "notes"
                result.docs = self._format_docs("personal notes", docs)
                result.confidence = 0.8
                print(f"[rag] HIT notes ({len(notes)} matches)")
                return self._maybe_add_git(result, priority, cfg)

        # ── 3. Episodic memory ──
        print(f"[rag] 3/5 episodes: store={'YES' if self._episodes else 'NO'} enabled={cfg.get('rag_episodes_enabled', True)}")
        if self._episodes and cfg.get("rag_episodes_enabled", True):
            episodes = self._episodes.search(query, max_results=max_results)
            print(f"[rag] 3/5 episodes: {len(episodes)} matches")
            if episodes:
                docs = "\n".join(
                    f"- [{ep['date']}] {ep['summary']} ({self._relative_time(ep.get('ts', 0))})"
                    for ep in episodes
                )
                result.source = "episodes"
                result.docs = self._format_docs("conversation history", docs)
                result.confidence = 0.6
                print(f"[rag] HIT episodes ({len(episodes)} matches)")
                return self._maybe_add_git(result, priority, cfg)

        # ── 4. Vision memory (only for memory-referencing phrases) ──
        _is_memory_q = bool(_MEMORY_RE.search(query))
        print(f"[rag] 4/5 vision: store={'YES' if self._vision else 'NO'} enabled={cfg.get('rag_vision_memory_enabled', True)} memory_phrase={_is_memory_q}")
        if (self._vision and cfg.get("rag_vision_memory_enabled", True)
                and _is_memory_q):
            visions = self._vision.search(query, max_results=max_results)
            print(f"[rag] 4/5 vision: {len(visions)} matches")
            if visions:
                from datetime import datetime
                docs = "\n".join(
                    f"- [{datetime.fromtimestamp(v['ts']).strftime('%H:%M')}] {v['description']}"
                    for v in visions
                )
                result.source = "vision"
                result.docs = self._format_docs("screen observations", docs)
                result.confidence = 0.5
                print(f"[rag] HIT vision ({len(visions)} matches)")
                return self._maybe_add_git(result, priority, cfg)

        # ── 5. Web search (auto-triggers for factual questions) ──
        _is_factual = self._is_factual_question(query)
        _auto_web = cfg.get("rag_auto_web_search", True)
        print(f"[rag] 5/5 web: auto={_auto_web} factual={_is_factual}")
        if _auto_web and _is_factual:
            try:
                print(f"[rag] 5/5 web: searching '{query[:40]}'...")
                web_results = await search_web(query, max_results=3)
                # Filter out error results
                valid = [r for r in web_results if "error" not in r]
                print(f"[rag] 5/5 web: {len(web_results)} raw, {len(valid)} valid")
                if valid:
                    docs_lines = []
                    for r in valid:
                        line = f"- {r['title']}: {r['content']} ({r['url']})"
                        if r.get('page_content'):
                            line += f"\n  Full excerpt: {r['page_content'][:1500]}"
                        docs_lines.append(line)
                    docs = "\n".join(docs_lines)
                    result.source = "web"
                    result.personal = False  # objective
                    result.docs = self._format_docs("web search", docs)
                    result.confidence = 0.4
                    print(f"[rag] HIT web ({len(valid)} results)")
                    return self._maybe_add_git(result, priority, cfg)
            except Exception as e:
                print(f"[rag] web search failed: {e}")

        # ── 6. Git context (standalone if priority high enough) ──
        return self._maybe_add_git(result, priority, cfg)

    def _is_factual_question(self, text: str) -> bool:
        # Conversational questions are not factual lookups
        if _CONVERSATIONAL_RE.search(text):
            return False
        return bool(_FACTUAL_RE.search(text))

    @staticmethod
    def _relative_time(ts: float) -> str:
        """Human-readable age from timestamp."""
        import time
        age = time.time() - ts
        if age < 60:
            return "just now"
        elif age < 3600:
            return f"{int(age/60)} min ago"
        elif age < 86400:
            return f"{int(age/3600)} hours ago"
        elif age < 604800:
            return f"{int(age/86400)} days ago"
        else:
            return f"{int(age/604800)} weeks ago"

    def _format_docs(self, source: str, items: str) -> str:
        labels = {
            "knowledge": "Source: UPLOADED KNOWLEDGE DOCS (curated, high confidence)",
            "personal notes": "Source: YOUR PERSONAL NOTES — check timestamps, if data is old and user asks about current info, suggest 'search for X' to get fresh data",
            "conversation history": "Source: PAST CONVERSATION EPISODES — check timestamps for relevance",
            "screen observations": "Source: SCREEN OBSERVATIONS — check timestamps, these are snapshots in time",
            "web search": "Source: LIVE WEB SEARCH (retrieved just now, cite URLs)",
        }
        label = labels.get(source, f"Source: {source.upper()}")
        return f"[{label}]\n{items}"

    def _maybe_add_git(self, result: RagResult, priority: int,
                       cfg: dict) -> RagResult:
        """Attach git context if priority >= 7 and git is enabled."""
        if (priority >= 7 and cfg.get("rag_git_enabled", False)
                and cfg.get("git_repo_path", "")):
            # Git is async — we set a flag; caller resolves it
            result.git_context = cfg["git_repo_path"]
        return result

    async def resolve_git_context(self, repo_path: str) -> str | None:
        """Run git commands and return a summary. Called by backend after enrich()."""
        try:
            log_proc = await asyncio.create_subprocess_exec(
                "git", "-C", repo_path, "log", "--oneline", "-5",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            log_out, _ = await asyncio.wait_for(log_proc.communicate(), timeout=5.0)

            diff_proc = await asyncio.create_subprocess_exec(
                "git", "-C", repo_path, "diff", "--stat",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            diff_out, _ = await asyncio.wait_for(diff_proc.communicate(), timeout=5.0)

            parts = []
            log_text = log_out.decode(errors="replace").strip()
            if log_text:
                parts.append(f"Recent commits:\n{log_text}")
            diff_text = diff_out.decode(errors="replace").strip()
            if diff_text:
                parts.append(f"Uncommitted changes:\n{diff_text}")

            combined = "\n".join(parts)
            return combined[:500] if combined else None
        except Exception as e:
            print(f"[rag] git context failed: {e}")
            return None
