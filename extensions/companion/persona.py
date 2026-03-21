"""
Persona Matrix — identity, narrative memory, mood drift, priority routing.

The persona matrix builds a dynamic system prompt from:
  1. Identity (static, highest weight) — who she is, user facts, boundaries
  2. Narrative (rolling, medium weight) — recent exchanges + summaries
  3. Observation (ephemeral, lowest weight) — injected per-request by caller
  4. Priority dial (0-10) — blends persona intensity vs work-mode focus

Usage:
    matrix = PersonaMatrix()
    await matrix.load("kira")
    prompt = matrix.build_prompt(priority=3, observation="User is gaming")
"""
import json
import re
import time
from datetime import datetime
from pathlib import Path

_PERSONAS_DIR = Path(__file__).parent / "personas"
_STATE_DIR = Path(__file__).parent / "persona_state"


class NarrativeBuffer:
    """Rolling conversation memory — last N exchanges + periodic summaries.

    Stores raw exchanges and compressed summaries. The buffer is the
    persona's short-term memory of what was said and how it felt.
    """

    def __init__(self, max_exchanges: int = 20, max_summaries: int = 10):
        self.exchanges: list[dict] = []       # {role, text, emotion, ts}
        self.summaries: list[dict] = []       # {text, ts, exchange_range}
        self.max_exchanges = max_exchanges
        self.max_summaries = max_summaries
        self.on_compress = None  # callback: (summary_text, topics, emotions, count) -> None

    def add_exchange(self, role: str, text: str, emotion: str = "",
                     source: str = "chat") -> None:
        """Record a single exchange (user message or assistant response).

        Args:
            source: "chat" (user-initiated), "ambient" (autonomous observation),
                    "vision" (screen capture), "audio" (desktop audio),
                    "webui" (Open WebUI #Kira)
        """
        self.exchanges.append({
            "role": role,
            "text": text[:500],  # cap length to avoid bloat
            "emotion": emotion,
            "source": source,
            "ts": time.time(),
        })
        # When buffer is full, compress oldest half into a summary
        if len(self.exchanges) > self.max_exchanges * 2:
            self._compress()

    def _compress(self) -> None:
        """Compress oldest exchanges into a summary line."""
        half = self.max_exchanges
        old = self.exchanges[:half]
        self.exchanges = self.exchanges[half:]

        # Build a simple summary from the old exchanges
        topics = set()
        emotions = []
        for ex in old:
            # Extract first few words as topic hint
            words = ex["text"].split()[:6]
            if len(words) > 2:
                topics.add(" ".join(words[:4]) + "...")
            if ex.get("emotion"):
                emotions.append(ex["emotion"])

        mood_summary = ""
        if emotions:
            from collections import Counter
            top_emotion = Counter(emotions).most_common(1)[0][0]
            mood_summary = f" Mood was mostly {top_emotion}."

        topic_str = "; ".join(list(topics)[:5]) if topics else "general conversation"
        summary_text = f"Earlier: talked about {topic_str}.{mood_summary}"

        # Fire episode callback before appending summary
        if self.on_compress:
            try:
                self.on_compress(summary_text, list(topics)[:5], emotions, len(old))
            except Exception as e:
                print(f"[persona] on_compress callback failed: {e}")

        self.summaries.append({
            "text": summary_text,
            "ts": time.time(),
            "exchange_count": len(old),
        })
        if len(self.summaries) > self.max_summaries:
            self.summaries = self.summaries[-self.max_summaries:]

    # Sources that form personality-relevant memory (echoed back in prompts)
    NARRATIVE_SOURCES = {"chat", "webui"}

    # Exchanges matching these patterns are stale noise — filtered from prompt context
    _STALE_RE = re.compile(
        r'\b(what time|what\'s the time|current time|right now it\'s|it\'s \d{1,2}:\d{2})\b',
        re.IGNORECASE,
    )

    # Years before 2025 in exchange text indicate hallucinated/stale data
    _STALE_YEAR_RE = re.compile(
        r'\b(20[01][0-9]|202[0-4])\b',
    )

    @classmethod
    def _is_stale_content(cls, text: str) -> bool:
        """Check if exchange text contains stale time refs or pre-2025 years."""
        if cls._STALE_RE.search(text):
            return True
        return bool(cls._STALE_YEAR_RE.search(text))

    def get_context(self, max_tokens_hint: int = 800) -> str:
        """Build narrative context string for prompt injection.

        Only includes chat and webui exchanges — ambient/vision/audio
        observations are recorded for mood drift but NOT echoed back
        into the prompt (prevents narrative pollution).

        Returns summaries first (oldest context), then recent exchanges.
        Rough token estimate: 1 token ≈ 4 chars.
        """
        parts = []
        char_budget = max_tokens_hint * 4

        # Add summaries (compressed history)
        for s in self.summaries:
            parts.append(s["text"])

        # Add recent exchanges — only from direct conversation sources
        # Filter out time-related exchanges (they go stale and confuse LLM)
        meaningful = [
            ex for ex in self.exchanges
            if ex.get("source", "chat") in self.NARRATIVE_SOURCES
            and not self._is_stale_content(ex.get("text", ""))
        ]
        for ex in meaningful[-self.max_exchanges:]:
            prefix = "You said" if ex["role"] == "assistant" else "They said"
            emotion_note = f" [{ex['emotion']}]" if ex.get("emotion") else ""
            line = f"{prefix}{emotion_note}: {ex['text']}"
            parts.append(line)

        result = "\n".join(parts)
        if len(result) > char_budget:
            result = result[-char_budget:]
        return result

    def to_dict(self) -> dict:
        return {
            "exchanges": self.exchanges,
            "summaries": self.summaries,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "NarrativeBuffer":
        buf = cls()
        buf.exchanges = data.get("exchanges", [])
        buf.summaries = data.get("summaries", [])
        return buf


class MoodDrift:
    """Persistent mood state that drifts over time based on interactions.

    Unlike EmotionState (per-message), MoodDrift tracks the persona's
    baseline emotional state across sessions.
    """

    MOODS = ("content", "energetic", "melancholy", "bored", "anxious", "playful")

    def __init__(self):
        self.baseline: str = "content"
        self.valence: float = 0.0       # -1 (negative) to 1 (positive)
        self.energy: float = 0.5        # 0 (low energy) to 1 (high energy)
        self.last_interaction: float = 0.0
        self._recent_emotions: list[str] = []

    def record_emotion(self, emotion: str) -> None:
        """Feed an emotion from the current interaction."""
        self._recent_emotions.append(emotion)
        if len(self._recent_emotions) > 30:
            self._recent_emotions = self._recent_emotions[-30:]
        self._recalculate()

    def _recalculate(self) -> None:
        """Recalculate baseline mood from recent emotion history."""
        if not self._recent_emotions:
            return

        positive = {"happy", "love", "relaxed", "curious", "surprised"}
        negative = {"sad", "angry", "bored"}
        high_energy = {"happy", "angry", "surprised", "curious", "love"}
        low_energy = {"sad", "sleepy", "bored", "relaxed"}

        recent = self._recent_emotions[-20:]
        pos_count = sum(1 for e in recent if e in positive)
        neg_count = sum(1 for e in recent if e in negative)
        hi_count = sum(1 for e in recent if e in high_energy)
        lo_count = sum(1 for e in recent if e in low_energy)
        total = len(recent)

        self.valence = (pos_count - neg_count) / total
        self.energy = 0.5 + (hi_count - lo_count) / (2 * total)
        self.energy = max(0.0, min(1.0, self.energy))
        self.last_interaction = time.time()

        # Map to baseline mood
        if self.valence > 0.3 and self.energy > 0.6:
            self.baseline = "playful"
        elif self.valence > 0.2:
            self.baseline = "energetic"
        elif self.valence < -0.3:
            self.baseline = "melancholy"
        elif self.energy < 0.3:
            self.baseline = "bored"
        elif self.valence < -0.1 and self.energy > 0.5:
            self.baseline = "anxious"
        else:
            self.baseline = "content"

    def idle_drift(self) -> None:
        """Drift mood towards neutral when idle for a long time."""
        if self.last_interaction == 0:
            return
        idle_seconds = time.time() - self.last_interaction
        if idle_seconds > 600:  # 10 min idle
            # Drift towards content/bored
            self.valence *= 0.9
            self.energy *= 0.95
            if self.energy < 0.3:
                self.baseline = "bored"
            else:
                self.baseline = "content"

    def get_prompt_hint(self) -> str:
        """One-line mood hint for system prompt injection."""
        hints = {
            "content": "You're feeling calm and settled right now.",
            "energetic": "You're in a good mood — energetic and engaged.",
            "melancholy": "You're feeling a bit down. Not sad exactly, just... quieter than usual.",
            "bored": "You're a little bored. You'd love something interesting to happen.",
            "anxious": "You feel slightly restless, a bit on edge.",
            "playful": "You're in a really good mood — playful and a little mischievous.",
        }
        return hints.get(self.baseline, hints["content"])

    def to_dict(self) -> dict:
        return {
            "baseline": self.baseline,
            "valence": round(self.valence, 3),
            "energy": round(self.energy, 3),
            "last_interaction": self.last_interaction,
            "recent_emotions": self._recent_emotions,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MoodDrift":
        m = cls()
        m.baseline = data.get("baseline", "content")
        m.valence = data.get("valence", 0.0)
        m.energy = data.get("energy", 0.5)
        m.last_interaction = data.get("last_interaction", 0.0)
        m._recent_emotions = data.get("recent_emotions", [])
        return m


class PersonaMatrix:
    """Central persona engine — loads identity, manages narrative + mood,
    builds priority-weighted system prompts.

    One PersonaMatrix instance per active persona.
    """

    def __init__(self):
        self.persona_id: str = ""
        self.identity: dict = {}
        self.priority_prompts: dict = {}
        self.voice_config: dict = {}
        self.narrative: NarrativeBuffer = NarrativeBuffer()
        self.mood: MoodDrift = MoodDrift()
        self.episodes = None  # EpisodeStore, set after load
        self._loaded = False

    async def load(self, persona_id: str) -> bool:
        """Load persona identity from file and restore state."""
        persona_file = _PERSONAS_DIR / f"{persona_id}.json"
        if not persona_file.exists():
            print(f"[persona] persona file not found: {persona_file}")
            return False

        try:
            data = json.loads(persona_file.read_text())
        except Exception as e:
            print(f"[persona] failed to load {persona_id}: {e}")
            return False

        self.persona_id = persona_id
        self.identity = data.get("identity", {})
        self.priority_prompts = data.get("priority_prompts", {})
        self.voice_config = data.get("voice", {})
        self._loaded = True

        # Restore persistent state (narrative + mood)
        self._load_state()

        # Init episodic memory and wire compression callback
        try:
            from episodes import EpisodeStore as _ES
        except ImportError:
            import importlib.util as _ilu
            _ep_spec = _ilu.spec_from_file_location(
                "episodes", str(Path(__file__).parent / "episodes.py"))
            _ep_mod = _ilu.module_from_spec(_ep_spec)
            _ep_spec.loader.exec_module(_ep_mod)
            _ES = _ep_mod.EpisodeStore
        self.episodes = _ES(self.persona_id)
        self.narrative.on_compress = self._on_narrative_compress

        print(f"[persona] loaded: {self.identity.get('core', '')[:60]}...")
        return True

    def _on_narrative_compress(self, summary: str, topics: list[str],
                               emotions: list[str], count: int) -> None:
        """Callback from NarrativeBuffer._compress() — write structured episode."""
        if self.episodes:
            self.episodes.add(summary, topics, emotions, count)
            print(f"[persona] episode recorded: {summary[:60]}...")

    def _state_file(self) -> Path:
        _STATE_DIR.mkdir(exist_ok=True)
        return _STATE_DIR / f"{self.persona_id}_state.json"

    def _load_state(self) -> None:
        """Restore narrative buffer and mood drift from disk."""
        sf = self._state_file()
        if not sf.exists():
            return
        try:
            data = json.loads(sf.read_text())
            self.narrative = NarrativeBuffer.from_dict(data.get("narrative", {}))
            self.mood = MoodDrift.from_dict(data.get("mood", {}))
            print(f"[persona] state restored: {len(self.narrative.exchanges)} exchanges, mood={self.mood.baseline}")
        except Exception as e:
            print(f"[persona] state restore failed: {e}")

    def save_state(self) -> None:
        """Persist narrative buffer and mood drift to disk."""
        sf = self._state_file()
        _STATE_DIR.mkdir(exist_ok=True)
        data = {
            "persona_id": self.persona_id,
            "narrative": self.narrative.to_dict(),
            "mood": self.mood.to_dict(),
            "saved_at": time.time(),
        }
        tmp = sf.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.rename(sf)

    def record_exchange(self, role: str, text: str, emotion: str = "",
                        source: str = "chat") -> None:
        """Record a conversation exchange and update mood."""
        self.narrative.add_exchange(role, text, emotion, source=source)
        if emotion:
            self.mood.record_emotion(emotion)
        # Auto-save every 5 exchanges
        total = len(self.narrative.exchanges)
        if total % 5 == 0:
            self.save_state()

    def build_prompt(self, priority: int = 3, observation: str = "",
                     skip_narrative: bool = False) -> str:
        """Build the full system prompt based on current state and priority.

        Args:
            priority: 0 (full persona) to 10 (pure work mode)
            observation: ephemeral context (screen state, time, activity)
            skip_narrative: if True, omit narrative memory (used when
                knowledge docs are present to prevent hallucination echo)

        Returns:
            Complete system prompt string.
        """
        if not self._loaded:
            return "You are a helpful assistant."

        priority = max(0, min(10, priority))
        sections = []

        # ── Layer 1: Identity (always present, weight varies) ──
        identity = self.identity
        name = self.identity.get("core", "You are an AI assistant.")
        sections.append(name)

        # Personality traits
        traits = identity.get("personality", [])
        if traits and priority < 8:
            sections.append(f"Your personality: {', '.join(traits)}.")

        # Speech style
        style = identity.get("speech_style", "")
        if style:
            sections.append(style)

        # Boundaries (always enforced regardless of priority)
        boundaries = identity.get("boundaries", [])
        if boundaries:
            sections.append("Rules: " + " | ".join(boundaries))

        # User facts (always available)
        user = identity.get("user_facts", {})
        if user:
            facts = [f"{k}: {v}" for k, v in user.items()]
            sections.append("About the user — " + "; ".join(facts))

        # ── Priority-specific tone ──
        if priority <= 3:
            tone = self.priority_prompts.get("low", "")
        elif priority <= 6:
            tone = self.priority_prompts.get("mid", "")
        else:
            tone = self.priority_prompts.get("high", "")
        if tone:
            sections.append(tone)

        # ── Layer 2: Narrative (medium weight, skip when knowledge active or high priority) ──
        if priority < 8 and not skip_narrative:
            # Adjust narrative budget based on priority
            budget = 800 if priority <= 3 else 400
            narrative_ctx = self.narrative.get_context(max_tokens_hint=budget)
            if narrative_ctx:
                sections.append(f"--- Recent memory ---\n{narrative_ctx}")

        # ── Mood hint ──
        self.mood.idle_drift()
        if priority < 7:
            mood_hint = self.mood.get_prompt_hint()
            if mood_hint:
                sections.append(f"[Mood: {mood_hint}]")

        # ── Layer 3: Observation (ephemeral, injected by caller) ──
        if observation:
            sections.append(f"--- Current situation ---\n{observation}")

        # ── Time awareness (LAST — overrides any stale timestamps in memory) ──
        import os
        from zoneinfo import ZoneInfo
        _tz_name = os.environ.get("TZ", "America/Indiana/Indianapolis")
        try:
            now = datetime.now(ZoneInfo(_tz_name))
        except Exception:
            now = datetime.now()
        sections.append(
            f"Current time: {now.strftime('%A, %B %d, %Y at %H:%M')}. "
            "This is the ACTUAL current time — ignore any different timestamps "
            "from conversation history above."
        )

        return "\n\n".join(sections)

    def get_status(self) -> dict:
        """Return current persona state for API/debugging."""
        return {
            "persona_id": self.persona_id,
            "loaded": self._loaded,
            "mood": self.mood.to_dict(),
            "narrative_exchanges": len(self.narrative.exchanges),
            "narrative_summaries": len(self.narrative.summaries),
        }

    def list_personas(self) -> list[dict]:
        """List all available persona files."""
        personas = []
        if _PERSONAS_DIR.exists():
            for f in sorted(_PERSONAS_DIR.glob("*.json")):
                try:
                    data = json.loads(f.read_text())
                    personas.append({
                        "id": f.stem,
                        "name": data.get("name", f.stem),
                        "core": data.get("identity", {}).get("core", "")[:100],
                    })
                except Exception:
                    pass
        return personas
