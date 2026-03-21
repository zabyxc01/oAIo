"""
Companion extension — WebSocket relay for desktop + phone AI companion clients.
Mounted at /extensions/companion by the extension loader.

Endpoints:
  WS  /ws             — bidirectional companion protocol
  GET  /config        — companion configuration (model, voice, system prompt)
  PATCH /config       — update configuration
  GET  /clients       — list connected clients
  GET  /emotion       — current emotion state
"""
import asyncio
import base64
import json
import re
import time
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

# Extension loader uses spec_from_file_location (not a package),
# so we import persona.py via direct path.
import importlib.util as _ilu
_persona_spec = _ilu.spec_from_file_location(
    "persona", str(Path(__file__).parent / "persona.py")
)
_persona_mod = _ilu.module_from_spec(_persona_spec)
_persona_spec.loader.exec_module(_persona_mod)
PersonaMatrix = _persona_mod.PersonaMatrix

_knowledge_spec = _ilu.spec_from_file_location(
    "knowledge", str(Path(__file__).parent / "knowledge.py")
)
_knowledge_mod = _ilu.module_from_spec(_knowledge_spec)
_knowledge_spec.loader.exec_module(_knowledge_mod)
KnowledgeClient = _knowledge_mod.KnowledgeClient

_rag_spec = _ilu.spec_from_file_location(
    "rag", str(Path(__file__).parent / "rag.py")
)
_rag_mod = _ilu.module_from_spec(_rag_spec)
_rag_spec.loader.exec_module(_rag_mod)
RagRouter = _rag_mod.RagRouter
RagResult = _rag_mod.RagResult

_notes_spec = _ilu.spec_from_file_location(
    "notes", str(Path(__file__).parent / "notes.py")
)
_notes_mod = _ilu.module_from_spec(_notes_spec)
_notes_spec.loader.exec_module(_notes_mod)
NotesStore = _notes_mod.NotesStore

_vision_spec = _ilu.spec_from_file_location(
    "vision_memory", str(Path(__file__).parent / "vision_memory.py")
)
_vision_mod = _ilu.module_from_spec(_vision_spec)
_vision_spec.loader.exec_module(_vision_mod)
VisionMemoryStore = _vision_mod.VisionMemoryStore

router = APIRouter()

# ── Paths ────────────────────────────────────────────────────────────────────
_EXT_DIR = Path(__file__).parent
_STATE_FILE = _EXT_DIR / "companion.json"
_SERVICES_FILE = _EXT_DIR.parent.parent / "config" / "services.json"

# ── State ────────────────────────────────────────────────────────────────────
_connected_clients: dict[str, dict] = {}  # client_id → {ws, info, connected_at}


# ── 14 canonical emotions (Amica research + VRM standard) ────────────────────
EMOTIONS = frozenset({
    "happy", "angry", "sad", "surprised", "relaxed", "neutral",
    "blush", "sleepy", "thinking", "shy", "bored", "serious", "curious", "love",
})


# ── Emotion State Engine ────────────────────────────────────────────────────
class EmotionState:
    """Persistent, blended emotion state — not per-message.

    Tracks primary + secondary emotions with intensities, plus a
    slowly-drifting mood baseline.
    """

    def __init__(self):
        self.primary: str = "neutral"
        self.primary_intensity: float = 0.0
        self.secondary: str = ""
        self.secondary_intensity: float = 0.0
        self.mood: str = "content"           # baseline: content, bored, energetic, melancholy
        self.mood_momentum: float = 0.0      # -1.0 (declining) to 1.0 (improving)
        self._history: list[str] = []        # last N emotions for mood drift
        self._history_max = 20

    def update(self, primary: str, intensity: float = 0.7,
               secondary: str = "", secondary_intensity: float = 0.0) -> None:
        """Set new emotion state from a parsed LLM response."""
        self.primary = primary if primary in EMOTIONS else "neutral"
        self.primary_intensity = max(0.0, min(1.0, intensity))
        self.secondary = secondary if secondary in EMOTIONS else ""
        self.secondary_intensity = max(0.0, min(1.0, secondary_intensity))

        # Track history for mood drift
        self._history.append(self.primary)
        if len(self._history) > self._history_max:
            self._history = self._history[-self._history_max:]
        self._update_mood()

    def _update_mood(self) -> None:
        """Drift mood based on recent emotion history."""
        if len(self._history) < 3:
            return

        recent = self._history[-10:]
        positive = sum(1 for e in recent if e in ("happy", "love", "relaxed", "curious"))
        negative = sum(1 for e in recent if e in ("sad", "angry", "bored"))
        neutral = len(recent) - positive - negative

        ratio = (positive - negative) / len(recent)
        self.mood_momentum = max(-1.0, min(1.0, ratio))

        if ratio > 0.3:
            self.mood = "energetic"
        elif ratio < -0.3:
            self.mood = "melancholy"
        elif neutral > len(recent) * 0.6:
            self.mood = "bored"
        else:
            self.mood = "content"

    def to_dict(self) -> dict:
        """Serialise for WebSocket payload."""
        return {
            "primary": self.primary,
            "primary_intensity": round(self.primary_intensity, 2),
            "secondary": self.secondary,
            "secondary_intensity": round(self.secondary_intensity, 2),
            "mood": self.mood,
            "mood_momentum": round(self.mood_momentum, 2),
        }


# Global emotion state (shared across all clients for now)
_emotion_state = EmotionState()

# ── Persona Matrix ──────────────────────────────────────────────────────────
_persona_matrix = PersonaMatrix()
_persona_priority: int = 3     # 0=full persona, 10=work mode
_persona_enabled: bool = False  # OFF by default — must be explicitly enabled
_knowledge_client: KnowledgeClient | None = None
_rag_router: RagRouter | None = None


# ── Config ───────────────────────────────────────────────────────────────────

_EMOTION_TAG_INSTRUCTION = (
    "IMPORTANT: Begin EVERY response with an emotion tag in this exact format: "
    "[emotion:intensity] where emotion is one of: happy, angry, sad, surprised, "
    "relaxed, neutral, blush, sleepy, thinking, shy, bored, serious, curious, love "
    "and intensity is a decimal from 0.0 to 1.0 indicating strength. "
    "You may optionally add a second tag for blended emotions. "
    "Examples: [happy:0.8] Hey that's really cool! ... "
    "[sad:0.6] I'm sorry to hear that. ... [thinking:0.4] Hmm let me consider that. "
    "Never explain the tags. They control your avatar's facial expression."
)


def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "config": {
            "ollama_model": "qwen2.5:7b",  # default model
            "tts_voice": "af_heart",
            "system_prompt": (
                "Your name is Kira. You are a desktop companion AI with an anime avatar. "
                "You speak casually and naturally. Keep responses SHORT (1-3 sentences unless "
                "asked for detail). Be warm, slightly playful, and genuine. Never use emojis or markdown."
            ),
        },
        "clients": {},
    }


def _save_state(state: dict) -> None:
    tmp = _STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(_STATE_FILE)


_state = _load_state()


# ── Persona init ────────────────────────────────────────────────────────────
_persona_init_done = False


async def _ensure_persona() -> None:
    """Load persona if enabled in config. Only tries once per startup."""
    global _persona_matrix, _persona_enabled, _persona_priority, _persona_init_done, _knowledge_client, _rag_router
    if _persona_init_done:
        return
    _persona_init_done = True

    cfg = _state.get("config", {})
    _persona_enabled = cfg.get("persona_enabled", False)
    _persona_priority = cfg.get("persona_priority", 3)

    # Init knowledge client (available regardless of persona state)
    webui_key = cfg.get("webui_api_key", "")
    if webui_key:
        _knowledge_client = KnowledgeClient(api_key=webui_key)
        print(f"[companion] knowledge client ready")

    if not _persona_enabled:
        print("[companion] persona matrix disabled")
        return

    persona_id = cfg.get("persona", "kira")
    ok = await _persona_matrix.load(persona_id)
    if ok:
        print(f"[companion] persona matrix loaded: {persona_id}")
        # Init RAG router with all sources
        _rag_router = RagRouter(
            knowledge_client=_knowledge_client,
            config=cfg,
            persona_id=persona_id,
        )
        print(f"[companion] RAG router ready (persona={persona_id})")
    else:
        print(f"[companion] persona matrix failed to load: {persona_id}")
        _persona_enabled = False


# ── Service URL resolution ───────────────────────────────────────────────────
def _get_service_url(service_name: str) -> str:
    try:
        cfg = json.loads(_SERVICES_FILE.read_text())
        svc = cfg.get("services", {}).get(service_name, {})
        container = svc.get("container", service_name)
        port = svc.get("port")
        if container and port:
            return f"http://{container}:{port}"
    except Exception:
        pass
    defaults = {
        "ollama": "http://ollama:11434",
        "kokoro-tts": "http://kokoro-tts:8000",
        "faster-whisper": "http://faster-whisper:8003",
    }
    return defaults.get(service_name, f"http://{service_name}:8000")


# ── Fleet integration ────────────────────────────────────────────────────────
async def _register_fleet_node(client_id: str, client_info: dict) -> None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(
                "http://127.0.0.1:9000/extensions/fleet/nodes/register",
                json={
                    "name": client_info.get("name", f"companion-{client_id[:6]}"),
                    "url": f"companion://{client_id}",
                    "tags": ["companion", "thin-client"],
                    "node_type": "thin-client",
                    "client_info": client_info,
                },
            )
            result = r.json()
            print(f"[companion] fleet registration: {result}")
    except Exception as e:
        print(f"[companion] fleet registration failed: {e}")


async def _deregister_fleet_node(client_id: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get("http://127.0.0.1:9000/extensions/fleet/nodes")
            if r.status_code != 200:
                return
            nodes = r.json()
            for node in nodes:
                if node.get("url") == f"companion://{client_id}":
                    await client.delete(
                        f"http://127.0.0.1:9000/extensions/fleet/nodes/{node['id']}"
                    )
                    print(f"[companion] fleet node {node['id']} deregistered")
                    break
    except Exception as e:
        print(f"[companion] fleet deregistration failed: {e}")


# ── Message helpers ──────────────────────────────────────────────────────────
def _msg(msg_type: str, payload: dict, ref_id: str | None = None) -> str:
    return json.dumps({
        "type": msg_type,
        "id": ref_id or uuid.uuid4().hex[:12],
        "ts": time.time(),
        "payload": payload,
    })


# ── Emotion detection ───────────────────────────────────────────────────────
# Priority: [emotion:intensity] tags > (stage directions) > keyword fallback

# Regex for [emotion:intensity] tags — captures emotion and optional intensity
_EMOTION_TAG_RE = re.compile(r'\[(\w+):([\d.]+)\]')

# Regex for bare bracket tags [happy] without intensity
_BARE_TAG_RE = re.compile(r'\[(\w+)\]')

_EMOTION_KEYWORDS = {
    "happy": [
        "haha", "lol", "glad", "awesome", "great", "love", "yay", "nice",
        "wonderful", "fantastic", "excited", "fun", "enjoy", "happy", "laugh",
        "hehe", "sweet", "cool", "amazing",
    ],
    "angry": [
        "angry", "furious", "mad", "annoyed", "frustrated", "ugh",
        "hate", "pissed", "irritated", "damn", "hell",
    ],
    "sad": [
        "sad", "sorry", "unfortunately", "miss", "lonely", "cry",
        "disappointing", "sigh", "heartbreaking",
    ],
    "surprised": [
        "wow", "whoa", "oh!", "really?", "seriously?", "no way",
        "unexpected", "surprised", "shocking", "what?!",
    ],
    "relaxed": [
        "chill", "relax", "calm", "peaceful", "cozy", "comfy",
        "easy", "mellow", "gentle", "soft", "quiet",
    ],
    "blush": ["blush", "embarrass", "fluster"],
    "shy": ["shy", "nervous", "timid"],
    "sleepy": ["sleepy", "tired", "yawn", "exhausted", "drowsy", "nap", "bed"],
    "thinking": [
        "think", "wonder", "hmm", "ponder", "consider",
        "puzzl", "confus", "interesting",
    ],
    "curious": ["curious", "fascinating", "intriguing", "what if"],
    "bored": ["bored", "boring", "dull", "meh", "whatever"],
    "serious": ["serious", "important", "listen", "careful", "warning"],
    "love": ["love", "adore", "darling", "sweetheart", "dear", "heart"],
}

_STAGE_DIRECTION_MAP = {
    "smile": "happy", "smiling": "happy", "grin": "happy", "beam": "happy",
    "laugh": "happy", "giggle": "happy", "chuckle": "happy",
    "excited": "happy", "cheerful": "happy", "bright": "happy",
    "wink": "happy", "playful": "happy",
    "sad": "sad", "frown": "sad", "sigh": "sad", "tear": "sad",
    "disappointed": "sad", "down": "sad", "melancholy": "sad",
    "angry": "angry", "glare": "angry", "scowl": "angry", "furious": "angry",
    "irritat": "angry", "annoyed": "angry",
    "surprise": "surprised", "shock": "surprised", "gasp": "surprised",
    "wide eye": "surprised", "stunned": "surprised",
    "calm": "relaxed", "relax": "relaxed", "peaceful": "relaxed",
    "gentle": "relaxed", "warm": "relaxed", "nod": "relaxed",
    "blush": "blush", "fluster": "blush",
    "shy": "shy", "embarrass": "shy", "nervous": "shy",
    "sleepy": "sleepy", "yawn": "sleepy", "tired": "sleepy",
    "drowsy": "sleepy", "exhausted": "sleepy",
    "think": "thinking", "ponder": "thinking", "hmm": "thinking",
    "consider": "thinking", "wonder": "thinking", "puzzl": "thinking",
    "confused": "thinking", "tilt": "thinking", "thoughtful": "thinking",
    "curious": "curious", "intrigued": "curious", "fascinated": "curious",
    "bored": "bored", "uninterested": "bored",
    "serious": "serious", "stern": "serious", "firm": "serious",
    "loving": "love", "adoring": "love", "affectionate": "love",
    "cute": "love",
}


def _detect_emotion(text: str) -> dict:
    """Detect emotion from LLM response text.

    Returns dict with primary, primary_intensity, secondary, secondary_intensity.
    Detection priority: [emotion:intensity] tags > (stage directions) > keywords.
    """
    result = {
        "primary": "neutral",
        "primary_intensity": 0.5,
        "secondary": "",
        "secondary_intensity": 0.0,
    }

    text_stripped = text.strip()

    # 1. Try [emotion:intensity] tags (highest priority — ChatVRM pattern)
    tags = _EMOTION_TAG_RE.findall(text_stripped)
    if tags:
        emotion, intensity = tags[0]
        emotion = emotion.lower()
        if emotion in EMOTIONS:
            result["primary"] = emotion
            result["primary_intensity"] = max(0.0, min(1.0, float(intensity)))
        if len(tags) > 1:
            emotion2, intensity2 = tags[1]
            emotion2 = emotion2.lower()
            if emotion2 in EMOTIONS:
                result["secondary"] = emotion2
                result["secondary_intensity"] = max(0.0, min(1.0, float(intensity2)))
        _emotion_state.update(
            result["primary"], result["primary_intensity"],
            result["secondary"], result["secondary_intensity"],
        )
        return result

    # 2. Try bare bracket tags [happy] (without intensity)
    bare_tags = _BARE_TAG_RE.findall(text_stripped)
    if bare_tags:
        tag = bare_tags[0].lower()
        if tag in EMOTIONS:
            result["primary"] = tag
            result["primary_intensity"] = 0.7
            _emotion_state.update(tag, 0.7)
            return result

    # 3. Try parenthetical stage directions: (smiling), (puzzled look), etc.
    paren_match = re.match(r'^\(([^)]+)\)', text_stripped)
    if paren_match:
        direction = paren_match.group(1).lower()
        for keyword, emotion in _STAGE_DIRECTION_MAP.items():
            if keyword in direction:
                result["primary"] = emotion
                result["primary_intensity"] = 0.6
                _emotion_state.update(emotion, 0.6)
                return result

    # 4. Fallback: keyword scoring
    text_lower = text.lower()
    scores: dict[str, int] = {}
    for emotion, keywords in _EMOTION_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > 0:
            scores[emotion] = score

    if scores:
        sorted_emotions = sorted(scores.items(), key=lambda x: -x[1])
        best_emotion, best_score = sorted_emotions[0]
        result["primary"] = best_emotion
        result["primary_intensity"] = min(0.3 + best_score * 0.15, 0.9)
        if len(sorted_emotions) > 1:
            second_emotion, second_score = sorted_emotions[1]
            result["secondary"] = second_emotion
            result["secondary_intensity"] = min(0.2 + second_score * 0.1, 0.5)
        _emotion_state.update(
            result["primary"], result["primary_intensity"],
            result["secondary"], result["secondary_intensity"],
        )
        return result

    # 5. Nothing detected — neutral
    _emotion_state.update("neutral", 0.3)
    return result


def _strip_emotion_tags(text: str) -> str:
    """Remove [emotion:intensity] tags from text."""
    return _EMOTION_TAG_RE.sub('', text).strip()


def _strip_bare_tags(text: str) -> str:
    """Remove bare [emotion] tags from text."""
    return _BARE_TAG_RE.sub('', text).strip()


def _extract_stage_directions(text: str) -> list[str]:
    return re.findall(r'\(([^)]+)\)', text)


def _strip_stage_directions(text: str) -> str:
    return re.sub(r'\([^)]+\)\s*', '', text).strip()


def _clean_for_tts(text: str) -> str:
    """Remove all markup (emotion tags, stage directions) for TTS."""
    text = _strip_emotion_tags(text)
    text = _strip_bare_tags(text)
    text = _strip_stage_directions(text)
    return text.strip()


def _clean_for_display(text: str) -> str:
    """Remove emotion tags but keep stage directions for chat display."""
    text = _strip_emotion_tags(text)
    text = _strip_bare_tags(text)
    return text.strip()


# ── Pipeline handlers ────────────────────────────────────────────────────────
async def _handle_chat_request(ws: WebSocket, msg: dict) -> None:
    """Process chat.request: call ollama, detect emotion, TTS, send back."""
    payload = msg.get("payload", {})
    msg_id = msg.get("id", "")
    user_text = payload.get("text", "").strip()
    history = payload.get("history", [])
    context = payload.get("context", "").strip()  # ambient/screen context — merged into system prompt

    if not user_text:
        return

    cfg = _state["config"]
    ollama_url = _get_service_url("ollama")

    # ── Persona-aware prompt building ──
    await _ensure_persona()

    # ── Note detection: save note, then let LLM respond naturally ──
    _note_match = re.match(
        r'(?:remember\s+that\s+|remember:\s*|note:\s*)(.+)',
        user_text, re.IGNORECASE,
    )
    if _note_match and _rag_router and _rag_router.notes and cfg.get("rag_notes_enabled", True):
        note_text = _note_match.group(1).strip()
        key_words = note_text.split()[:5]
        key = " ".join(key_words)
        _rag_router.notes.add(key, note_text)
        print(f"[companion] note saved: '{key}'")
        # Inject system note into user message so LLM responds naturally
        user_text = f"[System: Note saved: '{note_text}']\n{user_text}"

    # ── Explicit search: force web search on through normal pipeline ──
    _explicit_search = bool(re.match(
        r'(?:search\s+(?:for\s+)?|look\s+up\s+|google\s+|web\s+search\s+)',
        user_text, re.IGNORECASE))
    if _explicit_search:
        cfg = {**cfg, "rag_auto_web_search": True}

    # ── RAG router ──
    _knowledge_context = ""
    _git_context = ""
    _rag_is_objective = False
    print(f"[companion] RAG check: router={'YES' if _rag_router else 'NO'} persona={_persona_enabled}")
    if _rag_router and _persona_enabled:
        _rag_router.update_config(cfg)
        rag_result = await _rag_router.enrich(user_text, _persona_priority, context)
        _knowledge_context = rag_result.docs
        _rag_is_objective = not rag_result.personal and bool(rag_result.docs)
        # Resolve git context if flagged
        if rag_result.git_context:
            git_text = await _rag_router.resolve_git_context(rag_result.git_context)
            if git_text:
                _git_context = git_text
                _rag_is_objective = True

    if _rag_is_objective:
        # Objective mode: strip persona, pure factual response
        # Only keep identity core + boundaries + user facts — no personality, narrative, mood
        identity = _persona_matrix.identity if _persona_matrix._loaded else {}
        system_prompt = identity.get("core", "You are a helpful assistant.")
        boundaries = identity.get("boundaries", [])
        if boundaries:
            system_prompt += "\n\nRules: " + " | ".join(boundaries)
        user = identity.get("user_facts", {})
        if user:
            facts = [f"{k}: {v}" for k, v in user.items()]
            system_prompt += "\n\nAbout the user — " + "; ".join(facts)
        print(f"[companion] objective mode: persona stripped for factual response")
    elif _persona_enabled and _persona_matrix._loaded:
        system_prompt = _persona_matrix.build_prompt(
            priority=_persona_priority,
            observation=context,
            skip_narrative=bool(_knowledge_context),
        )
    else:
        # Static prompt — persona disabled or not loaded
        system_prompt = cfg["system_prompt"]
        if context:
            system_prompt += "\n\n--- Current situation ---\n" + context

    if _git_context:
        system_prompt += "\n\n--- Recent code changes ---\n" + _git_context

    # In objective mode, skip emotion tag instruction — pure factual output
    if _rag_is_objective:
        system_prompt += (
            "\n\nYou are in FACTUAL MODE. Respond with facts only. "
            "No roleplay, no personality, no emotion tags, no stage directions. "
            "Be concise and cite your sources."
        )
    else:
        system_prompt += "\n\n" + _EMOTION_TAG_INSTRUCTION
    messages = [{"role": "system", "content": system_prompt}]

    # Sanitize history: remove consecutive same-role messages (broken turns)
    clean_history = []
    for h_msg in history:
        if clean_history and h_msg.get("role") == clean_history[-1].get("role"):
            clean_history[-1] = h_msg  # keep latest of consecutive same-role
        else:
            clean_history.append(h_msg)

    if _knowledge_context:
        # Trim history, remove trailing user msg (we replace with knowledge version)
        trimmed = clean_history[-4:] if len(clean_history) > 4 else list(clean_history)
        if trimmed and trimmed[-1].get("role") == "user":
            trimmed = trimmed[:-1]
        messages.extend(trimmed)
        messages.append({"role": "user", "content": (
            "[FACTUAL LOOKUP — OBJECTIVITY REQUIRED]\n"
            "The following data was retrieved from external sources. "
            "You MUST:\n"
            "1. State the facts EXACTLY as provided — do not paraphrase, round, or embellish\n"
            "2. Name the source (e.g. 'According to web search...' or 'From your notes...')\n"
            "3. If the data doesn't answer the question, say you don't know — do NOT guess\n"
            "4. Keep your personality in the delivery, but the FACTS must be untouched\n"
            "5. Do not adopt this information as part of your identity\n\n"
            + _knowledge_context
            + "\n\n[Question]\n" + user_text
        )})
    else:
        messages.extend(clean_history)
        messages.append({"role": "user", "content": user_text})

    # Call ollama
    try:
        # LLM options from config (adjustable via PATCH /config)
        opts = {"num_ctx": cfg.get("llm_num_ctx", 4096)}
        if _knowledge_context:
            opts["num_ctx"] = max(opts["num_ctx"], 8192)
        if _knowledge_context:
            opts["temperature"] = cfg.get("rag_temperature", 0.2)
        else:
            temp = cfg.get("llm_temperature")
            if temp is not None:
                opts["temperature"] = temp
        # Debug: dump what we're sending
        print(f"[companion] DEBUG: {len(messages)} messages, opts={opts}, knowledge={'YES' if _knowledge_context else 'NO'}")
        for i, m in enumerate(messages):
            print(f"[companion] DEBUG msg[{i}] role={m['role']} len={len(m['content'])} preview={m['content'][:80]}")
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{ollama_url}/api/chat",
                json={
                    "model": cfg["ollama_model"],
                    "messages": messages,
                    "stream": False,
                    "options": opts,
                },
            )
            r.raise_for_status()
            data = r.json()
            response_text = data.get("message", {}).get("content", "")
    except Exception as e:
        await ws.send_text(_msg("chat.response", {
            "text": f"[LLM error: {e}]",
            "done": True,
        }, msg_id))
        return

    # Detect emotion (updates global _emotion_state)
    emotion_data = _detect_emotion(response_text)

    # Clean text for display (remove emotion tags, keep stage directions)
    display_text = _clean_for_display(response_text)
    # Clean text for TTS (remove all markup)
    tts_text = _clean_for_tts(response_text)

    directions = _extract_stage_directions(response_text)
    if directions:
        print(f"[companion] stage directions: {directions}")

    print(f"[companion] emotion: {emotion_data['primary']}:{emotion_data['primary_intensity']}"
          + (f" + {emotion_data['secondary']}:{emotion_data['secondary_intensity']}" if emotion_data['secondary'] else "")
          + f" | mood: {_emotion_state.mood}")

    # ── Record exchanges in persona narrative ──
    # Tag source: ambient observations have context (screen state), user chats don't
    _exchange_source = "ambient" if context else "chat"
    if _persona_enabled and _persona_matrix._loaded:
        _persona_matrix.record_exchange("user", user_text, source=_exchange_source)
        _persona_matrix.record_exchange("assistant", display_text, emotion_data["primary"], source=_exchange_source)

    tts_mode = cfg.get("tts_mode", "batch")

    # Build response with full emotion payload
    _debug = {
        "rag_source": rag_result.source if (_rag_router and _persona_enabled) else "",
        "rag_confidence": round(rag_result.confidence, 2) if (_rag_router and _persona_enabled) else 0,
        "rag_personal": rag_result.personal if (_rag_router and _persona_enabled) else True,
        "rag_objective": _rag_is_objective,
        "rag_docs_len": len(_knowledge_context),
        "git_context": bool(_git_context),
        "persona_enabled": _persona_enabled,
        "priority": _persona_priority,
        "model": cfg.get("ollama_model", "?"),
        "temperature": cfg.get("rag_temperature", 0.2) if _knowledge_context else cfg.get("llm_temperature", "default"),
        "num_ctx": cfg.get("llm_num_ctx", 4096),
        "emotion_detected": f"{emotion_data['primary']}:{emotion_data['primary_intensity']}",
        "mood": _emotion_state.mood,
        "narrative_exchanges": len(_persona_matrix.narrative.exchanges) if _persona_matrix._loaded else 0,
        "tts_engine": cfg.get("tts_engine", "kokoro"),
    }
    chat_payload = {
        "text": display_text,
        "done": True,
        "emotion": emotion_data,
        "factual": bool(_knowledge_context),
        "objective": _rag_is_objective,
        "debug": _debug,
    }

    if tts_mode == "stream" and tts_text:
        await ws.send_text(_msg("chat.response", chat_payload, msg_id))
        await _stream_tts_sentences(ws, tts_text, msg_id, cfg)
    else:
        await ws.send_text(_msg("chat.response", chat_payload, msg_id))
        if tts_text:
            await _generate_and_send_tts(ws, tts_text, msg_id)


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    merged = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if merged and len(part) < 10:
            merged[-1] += " " + part
        else:
            merged.append(part)
    return merged if merged else [text]


async def _stream_tts_sentences(ws: WebSocket, text: str, ref_id: str, cfg: dict) -> None:
    sentences = _split_sentences(text)
    print(f"[companion] Streaming {len(sentences)} sentences")

    for i, sentence in enumerate(sentences):
        try:
            tts_engine = cfg.get("tts_engine", "kokoro")
            audio = await _generate_audio_bytes(sentence, cfg, tts_engine)
            if audio:
                await _send_audio(ws, audio, f"{ref_id}_chunk{i}", cfg)
        except Exception as e:
            print(f"[companion] Stream TTS error on sentence {i}: {e}")


async def _generate_audio_bytes(text: str, cfg: dict, engine: str) -> bytes | None:
    try:
        if engine == "kokoro":
            tts_url = _get_service_url("kokoro-tts")
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(
                    f"{tts_url}/v1/audio/speech",
                    json={"input": text, "voice": cfg.get("tts_voice", "af_heart"), "response_format": "wav"},
                )
                r.raise_for_status()
                return r.content

        elif engine == "indextts":
            tts_url = _get_service_url("indextts")
            ref_path = Path(cfg.get("ref_audio", "/mnt/oaio/ref-audio/avatar_voice.wav"))
            if not ref_path.exists():
                return None
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.post(
                    f"{tts_url}/synthesize",
                    files={"ref_audio": ("ref.wav", ref_path.read_bytes(), "audio/wav")},
                    data={"text": text},
                )
                r.raise_for_status()
                return r.content

        elif engine == "oaudio":
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.post(
                    "http://localhost:8002/speak",
                    json={"text": text, "voice": cfg.get("tts_voice", "af_heart"),
                          "model": cfg.get("rvc_model", "GOTHMOMMY.pth")},
                )
                r.raise_for_status()
                return r.content

        elif engine == "f5":
            ref_path = Path(cfg.get("ref_audio", "/mnt/oaio/ref-audio/avatar_voice.wav"))
            if not ref_path.exists():
                return None
            async with httpx.AsyncClient(timeout=180.0) as client:
                r = await client.post(
                    "http://localhost:8002/clone",
                    files={"ref_audio": ("ref.wav", ref_path.read_bytes(), "audio/wav")},
                    data={"target_text": text},
                )
                r.raise_for_status()
                return r.content
    except Exception as e:
        print(f"[companion] _generate_audio_bytes ({engine}): {e}")
        return None


async def _generate_and_send_tts(ws: WebSocket, text: str, ref_id: str) -> None:
    cfg = _state["config"]
    tts_engine = cfg.get("tts_engine", "kokoro")
    try:
        if tts_engine == "indextts":
            await _tts_indextts(ws, text, ref_id, cfg)
        elif tts_engine == "oaudio":
            await _tts_oaudio(ws, text, ref_id, cfg)
        elif tts_engine == "f5":
            await _tts_f5(ws, text, ref_id, cfg)
        else:
            await _tts_kokoro(ws, text, ref_id, cfg)
    except Exception as e:
        print(f"[companion] TTS ({tts_engine}) error: {e}")


async def _send_audio(ws: WebSocket, audio_bytes: bytes, ref_id: str, cfg: dict) -> None:
    compress = cfg.get("tts_compress", "always")
    should_compress = (compress == "always") or (compress == "auto" and len(audio_bytes) > 1_000_000)

    if should_compress and compress != "never":
        compressed = await _compress_wav_to_mp3(audio_bytes)
        audio_b64 = base64.b64encode(compressed).decode("ascii")
        fmt = "mp3"
    else:
        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
        fmt = "wav"

    print(f"[companion] TTS: sending {len(audio_b64)//1024}KB ({fmt})")
    await ws.send_text(_msg("tts.audio", {
        "audio_b64": audio_b64,
        "format": fmt,
        "sample_rate": 24000,
    }, ref_id))


async def _tts_kokoro(ws: WebSocket, text: str, ref_id: str, cfg: dict) -> None:
    tts_url = _get_service_url("kokoro-tts")
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{tts_url}/v1/audio/speech",
            json={"input": text, "voice": cfg.get("tts_voice", "af_heart"), "response_format": "wav"},
        )
        r.raise_for_status()
    await _send_audio(ws, r.content, ref_id, cfg)


async def _compress_wav_to_mp3(wav_bytes: bytes) -> bytes:
    import subprocess
    proc = subprocess.run(
        ["ffmpeg", "-i", "pipe:0", "-f", "mp3", "-ab", "48k", "-ar", "24000", "-ac", "1", "pipe:1"],
        input=wav_bytes, capture_output=True,
    )
    if proc.returncode != 0:
        print(f"[companion] ffmpeg compress failed: {proc.stderr[:200]}")
        return wav_bytes
    return proc.stdout


async def _tts_indextts(ws: WebSocket, text: str, ref_id: str, cfg: dict) -> None:
    tts_url = _get_service_url("indextts")
    ref_audio_path = cfg.get("ref_audio", "/mnt/oaio/ref-audio/avatar_voice.wav")
    ref_path = Path(ref_audio_path)
    if not ref_path.exists():
        print(f"[companion] IndexTTS ref audio not found: {ref_audio_path}")
        return

    ref_bytes = ref_path.read_bytes()
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{tts_url}/synthesize",
            files={"ref_audio": ("ref.wav", ref_bytes, "audio/wav")},
            data={"text": text},
        )
        r.raise_for_status()

    import subprocess
    proc = subprocess.run(
        ["ffmpeg", "-i", "pipe:0", "-f", "wav", "-ar", "24000", "-ac", "1",
         "-acodec", "pcm_s16le", "-sample_fmt", "s16", "pipe:1"],
        input=r.content, capture_output=True,
    )
    wav_bytes = proc.stdout if proc.returncode == 0 else r.content
    await _send_audio(ws, wav_bytes, ref_id, cfg)


async def _tts_oaudio(ws: WebSocket, text: str, ref_id: str, cfg: dict) -> None:
    oaudio_url = "http://localhost:8002"
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{oaudio_url}/speak",
            json={
                "text": text,
                "voice": cfg.get("tts_voice", "af_heart"),
                "model": cfg.get("rvc_model", "GOTHMOMMY.pth"),
            },
        )
        r.raise_for_status()

    audio_b64 = base64.b64encode(r.content).decode("ascii")
    print(f"[companion] oAudio: sending {len(audio_b64)//1024}KB (mp3)")
    await ws.send_text(_msg("tts.audio", {
        "audio_b64": audio_b64,
        "format": "mp3",
        "sample_rate": 24000,
    }, ref_id))


async def _tts_f5(ws: WebSocket, text: str, ref_id: str, cfg: dict) -> None:
    oaudio_url = "http://localhost:8002"
    ref_audio_path = cfg.get("ref_audio", "/mnt/oaio/ref-audio/avatar_voice.wav")
    ref_path = Path(ref_audio_path)
    if not ref_path.exists():
        print(f"[companion] F5 ref audio not found: {ref_audio_path}")
        return

    ref_bytes = ref_path.read_bytes()
    async with httpx.AsyncClient(timeout=180.0) as client:
        r = await client.post(
            f"{oaudio_url}/clone",
            files={"ref_audio": ("ref.wav", ref_bytes, "audio/wav")},
            data={"target_text": text},
        )
        r.raise_for_status()
    await _send_audio(ws, r.content, ref_id, cfg)


async def _handle_stt_audio(ws: WebSocket, msg: dict) -> None:
    payload = msg.get("payload", {})
    msg_id = msg.get("id", "")
    audio_b64 = payload.get("audio_b64", "")

    if not audio_b64:
        return

    stt_url = _get_service_url("faster-whisper")
    audio_bytes = base64.b64decode(audio_b64)

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{stt_url}/transcribe",
                files={"file": ("audio.wav", audio_bytes, "audio/wav")},
                data={"language": "en"},
            )
            r.raise_for_status()
            text = r.json().get("text", "").strip()
    except Exception as e:
        await ws.send_text(_msg("stt.transcript", {
            "text": f"[STT error: {e}]",
        }, msg_id))
        return

    await ws.send_text(_msg("stt.transcript", {"text": text}, msg_id))

    if text and payload.get("auto_chat", True):
        chat_msg = {
            "type": "chat.request",
            "id": msg_id,
            "payload": {
                "text": text,
                "history": payload.get("history", []),
            },
        }
        await _handle_chat_request(ws, chat_msg)


def _build_client_config(cfg: dict) -> dict:
    """Build the config dict sent to clients — single source of truth."""
    return {
        "ollama_model": cfg.get("ollama_model", "qwen2.5:7b"),
        "tts_engine": cfg.get("tts_engine", "kokoro"),
        "tts_voice": cfg.get("tts_voice", "af_heart"),
        "vision_model": cfg.get("vision_model", ""),
        "resource_preset": cfg.get("resource_preset", "optimal"),
        "rag_auto_web_search": cfg.get("rag_auto_web_search", True),
        "persona_enabled": _persona_enabled,
        "persona_priority": _persona_priority,
    }


async def _broadcast_config_update(updated: dict):
    """Push config changes to all connected WebSocket clients."""
    if not _connected_clients:
        return
    msg = _msg("config.sync", {"updated": updated, "config": _build_client_config(_state["config"])})
    stale = []
    for cid, client in _connected_clients.items():
        ws = client.get("ws")
        if ws:
            try:
                await ws.send_text(msg)
            except Exception:
                stale.append(cid)
    for cid in stale:
        _connected_clients.pop(cid, None)


# ── WebSocket endpoint ───────────────────────────────────────────────────────
@router.websocket("/ws")
async def companion_ws(websocket: WebSocket):
    """Bidirectional companion protocol over WebSocket."""
    await websocket.accept()

    client_id = uuid.uuid4().hex[:12]
    client_info = {"platform": "unknown", "name": f"companion-{client_id[:6]}"}
    _connected_clients[client_id] = {
        "ws": websocket,
        "info": client_info,
        "connected_at": time.time(),
    }

    print(f"[companion] client connected: {client_id}")

    cfg = _state["config"]
    await _ensure_persona()
    await websocket.send_text(_msg("state.sync", {
        "hub_status": "online",
        "services": {
            "ollama": _get_service_url("ollama"),
            "kokoro-tts": _get_service_url("kokoro-tts"),
            "faster-whisper": _get_service_url("faster-whisper"),
        },
        "config": _build_client_config(cfg),
        "emotion": _emotion_state.to_dict(),
        "persona": {
            "enabled": _persona_enabled,
            "priority": _persona_priority,
            **(_persona_matrix.get_status() if _persona_matrix._loaded else {}),
        },
    }))

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")

            if msg_type == "chat.request":
                asyncio.create_task(_handle_chat_request(websocket, msg))

            elif msg_type == "stt.audio":
                asyncio.create_task(_handle_stt_audio(websocket, msg))

            elif msg_type == "state.sync":
                payload = msg.get("payload", {})
                client_info.update({
                    "platform": payload.get("platform", "unknown"),
                    "name": payload.get("name", client_info["name"]),
                    "capabilities": payload.get("capabilities", []),
                    "client_type": payload.get("client_type", "desktop"),
                })
                _connected_clients[client_id]["info"] = client_info
                await _register_fleet_node(client_id, client_info)
                print(f"[companion] client identified: {client_info}")

            elif msg_type == "vision.analyze":
                asyncio.create_task(_handle_vision_analyze(websocket, msg))

            elif msg_type == "chat.multi":
                asyncio.create_task(_handle_chat_multi(websocket, msg))

            elif msg_type == "ping":
                await websocket.send_text(_msg("pong", {}))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[companion] client error: {e}")
    finally:
        _connected_clients.pop(client_id, None)
        await _deregister_fleet_node(client_id)
        print(f"[companion] client disconnected: {client_id}")


# ── REST endpoints ───────────────────────────────────────────────────────────
@router.get("/config")
def get_config():
    return dict(_state["config"])


@router.patch("/config")
async def patch_config(body: dict):
    cfg = _state["config"]
    allowed = {
        "ollama_model", "tts_voice", "tts_engine", "tts_compress", "tts_mode",
        "ref_audio", "system_prompt", "vision_model", "webui_api_key",
        # LLM tuning
        "llm_num_ctx", "llm_temperature",
        # RAG tuning
        "rag_chunk_chars", "rag_max_results", "rag_min_priority", "rag_temperature",
        # RAG sources
        "rag_knowledge_enabled", "rag_web_search_enabled", "rag_notes_enabled",
        "rag_episodes_enabled", "rag_vision_memory_enabled",
        "rag_git_enabled", "git_repo_path",
        "rag_auto_web_search",
        # Resource presets
        "resource_preset",
    }
    updated = {}
    for key in allowed:
        if key in body:
            cfg[key] = body[key]
            updated[key] = body[key]
    if updated:
        _save_state(_state)
        await _broadcast_config_update(updated)
    return {"updated": updated, "config": dict(cfg)}


# ── Resource presets ──────────────────────────────────────────────────────
_PRESETS = {
    "max_quality": {
        "label": "Max Quality",
        "description": "Best quality — IndexTTS + vision. High VRAM (~10 GB)",
        "tts_engine": "indextts",
        "vision_enabled": True,
        "vision_model": "llava:7b",
        "ollama_models": ["qwen2.5:7b", "llava:7b"],
    },
    "optimal": {
        "label": "Optimal",
        "description": "Balanced — Kokoro TTS + vision. Moderate VRAM (~6 GB)",
        "tts_engine": "kokoro",
        "vision_enabled": True,
        "vision_model": "llava:7b",
        "ollama_models": ["qwen2.5:7b", "llava:7b"],
    },
    "lite": {
        "label": "Lite",
        "description": "Lightweight — Kokoro TTS, no vision. Low VRAM (~3 GB)",
        "tts_engine": "kokoro",
        "vision_enabled": False,
        "vision_model": "",
        "ollama_models": ["qwen2.5:7b"],
    },
    "gaming": {
        "label": "Gaming",
        "description": "Max GPU — Kira sleeps. Zero VRAM",
        "tts_engine": "off",
        "vision_enabled": False,
        "vision_model": "",
        "ollama_models": [],
    },
}


@router.get("/presets")
def list_presets():
    """List available resource presets."""
    current = _state.get("config", {}).get("resource_preset", "optimal")
    return {
        "current": current,
        "presets": {k: {"label": v["label"], "description": v["description"]} for k, v in _PRESETS.items()},
    }


@router.post("/presets/{preset_name}")
async def apply_preset(preset_name: str):
    """Apply a resource preset — adjusts TTS, vision, and ollama models.

    This changes companion config and manages ollama model loading.
    """
    if preset_name not in _PRESETS:
        return {"error": f"Unknown preset: {preset_name}. Available: {list(_PRESETS.keys())}"}

    preset = _PRESETS[preset_name]
    cfg = _state["config"]
    changes = {}

    # TTS engine
    if preset["tts_engine"] == "off":
        changes["tts_mode"] = "off"
    else:
        cfg["tts_engine"] = preset["tts_engine"]
        changes["tts_engine"] = preset["tts_engine"]
        if cfg.get("tts_mode") == "off":
            cfg["tts_mode"] = "batch"
            changes["tts_mode"] = "batch"

    # Vision model
    cfg["vision_model"] = preset["vision_model"]
    changes["vision_model"] = preset["vision_model"]
    cfg["rag_vision_memory_enabled"] = preset["vision_enabled"]
    changes["rag_vision_memory_enabled"] = preset["vision_enabled"]

    # Save preset name
    cfg["resource_preset"] = preset_name
    changes["resource_preset"] = preset_name
    _save_state(_state)

    # Manage ollama models — unload unused, ensure needed ones are available
    ollama_url = _get_service_url("ollama")
    unloaded = []
    loaded = []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Get currently loaded models
            r = await client.get(f"{ollama_url}/api/ps")
            if r.status_code == 200:
                running = [m["name"] for m in r.json().get("models", [])]

                # Unload models not in preset
                for model_name in running:
                    if model_name not in preset["ollama_models"]:
                        try:
                            await client.post(f"{ollama_url}/api/generate",
                                json={"model": model_name, "keep_alive": 0})
                            unloaded.append(model_name)
                        except Exception:
                            pass

                # Warm up models in preset (light load to get them in VRAM)
                for model_name in preset["ollama_models"]:
                    if model_name not in running:
                        try:
                            await client.post(f"{ollama_url}/api/generate",
                                json={"model": model_name, "prompt": "", "keep_alive": "10m"},
                                timeout=60.0)
                            loaded.append(model_name)
                        except Exception:
                            pass
    except Exception as e:
        print(f"[companion] preset model management failed: {e}")

    print(f"[companion] preset applied: {preset_name} | unloaded={unloaded} loaded={loaded}")
    return {
        "preset": preset_name,
        "changes": changes,
        "models_unloaded": unloaded,
        "models_loaded": loaded,
    }


@router.post("/model/switch")
async def switch_model(body: dict):
    """Switch the chat LLM model. Unloads old, loads new.

    Body: {"model": "qwen2.5:7b"}
    """
    model_name = body.get("model", "").strip()
    if not model_name:
        return {"error": "model is required"}

    cfg = _state["config"]
    old_model = cfg.get("ollama_model", "")
    cfg["ollama_model"] = model_name
    _save_state(_state)

    ollama_url = _get_service_url("ollama")
    unloaded = ""
    loaded = ""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # Unload old model
            if old_model and old_model != model_name:
                try:
                    await client.post(f"{ollama_url}/api/generate",
                        json={"model": old_model, "keep_alive": 0})
                    unloaded = old_model
                except Exception:
                    pass
            # Load new model
            try:
                r = await client.post(f"{ollama_url}/api/generate",
                    json={"model": model_name, "prompt": "", "keep_alive": "10m"},
                    timeout=120.0)
                if r.status_code < 300:
                    loaded = model_name
                else:
                    return {"error": f"Model '{model_name}' not available. Pull it first: docker exec ollama ollama pull {model_name}",
                            "config_updated": True}
            except Exception as e:
                return {"error": f"Failed to load model: {e}", "config_updated": True}
    except Exception as e:
        return {"error": f"Ollama unreachable: {e}"}

    print(f"[companion] model switched: {old_model} -> {model_name} (unloaded={unloaded}, loaded={loaded})")
    return {"model": model_name, "unloaded": unloaded, "loaded": loaded}


@router.get("/clients")
def list_clients():
    return [
        {
            "id": cid,
            "info": data["info"],
            "connected_at": data["connected_at"],
            "uptime_s": round(time.time() - data["connected_at"]),
        }
        for cid, data in _connected_clients.items()
    ]


@router.get("/emotion")
def get_emotion():
    """Current emotion state — useful for debugging and external integrations."""
    return _emotion_state.to_dict()


# ── Multi-LLM / Agent Personas ──────────────────────────────────────────────

def _get_agents() -> list[dict]:
    return _state.get("agents", [])


def _save_agents(agents: list[dict]) -> None:
    _state["agents"] = agents
    _save_state(_state)


@router.get("/agents")
def list_agents():
    """Return configured agent personas."""
    return _get_agents()


@router.post("/agents")
def add_agent(body: dict):
    """Add a new agent persona."""
    name = body.get("name", "").strip()
    if not name:
        return {"error": "name is required"}
    agents = _get_agents()
    if any(a["name"] == name for a in agents):
        return {"error": f"Agent '{name}' already exists"}
    agent = {
        "name": name,
        "model": body.get("model", "qwen2.5:7b"),
        "system_prompt": body.get("system_prompt", f"You are {name}."),
    }
    agents.append(agent)
    _save_agents(agents)
    return {"added": agent}


@router.delete("/agents/{name}")
def remove_agent(name: str):
    """Remove an agent persona."""
    agents = _get_agents()
    agents = [a for a in agents if a["name"] != name]
    _save_agents(agents)
    return {"removed": name}


async def _handle_vision_analyze(ws: WebSocket, msg: dict) -> None:
    """Process vision.analyze: send screenshot to ollama vision model."""
    payload = msg.get("payload", {})
    msg_id = msg.get("id", "")
    image_b64 = payload.get("image_b64", "")
    context = payload.get("context", "")
    prompt = payload.get("prompt", "Describe what you see on screen.")
    model = _state["config"].get("vision_model", "llava:7b")

    if not image_b64:
        return

    cfg = _state["config"]
    ollama_url = _get_service_url("ollama")

    # ── Persona-aware prompt for vision too ──
    await _ensure_persona()
    if _persona_enabled and _persona_matrix._loaded:
        system_prompt = _persona_matrix.build_prompt(
            priority=_persona_priority,
            observation=context,
        )
    else:
        system_prompt = cfg["system_prompt"]
        if context:
            system_prompt += "\n\n--- Current situation ---\n" + context
    system_prompt += "\n\n" + _EMOTION_TAG_INSTRUCTION

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt, "images": [image_b64]},
    ]

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            r = await client.post(
                f"{ollama_url}/api/chat",
                json={"model": model, "messages": messages, "stream": False},
            )
            r.raise_for_status()
            response_text = r.json().get("message", {}).get("content", "")
    except Exception as e:
        await ws.send_text(_msg("chat.response", {
            "text": f"[Vision error: {e}]",
            "done": True,
        }, msg_id))
        return

    emotion_data = _detect_emotion(response_text)
    display_text = _clean_for_display(response_text)
    tts_text = _clean_for_tts(response_text)

    print(f"[companion] vision: {emotion_data['primary']}:{emotion_data['primary_intensity']} | {display_text[:80]}")

    # Persist vision description to vision memory
    if _rag_router and _rag_router.vision and cfg.get("rag_vision_memory_enabled", True):
        _rag_router.vision.add(display_text, activity=context)

    await ws.send_text(_msg("chat.response", {
        "text": display_text,
        "done": True,
        "emotion": emotion_data,
    }, msg_id))

    if tts_text:
        await _generate_and_send_tts(ws, tts_text, msg_id)


async def _handle_chat_multi(ws: WebSocket, msg: dict) -> None:
    """Multi-agent chat — multiple LLM personas respond to the same input.

    payload: {
        text: "user message",
        agents: [{name, model, system_prompt}, ...],  # override or use saved agents
        pattern: "debate" | "collaborate" | "chain",
        history: [...]
    }
    """
    payload = msg.get("payload", {})
    msg_id = msg.get("id", "")
    user_text = payload.get("text", "").strip()
    pattern = payload.get("pattern", "collaborate")
    history = payload.get("history", [])

    # Use provided agents or fall back to saved ones
    agents = payload.get("agents") or _get_agents()
    if not agents or len(agents) < 2:
        await ws.send_text(_msg("chat.response", {
            "text": "[Error: multi-agent requires at least 2 agents]",
            "done": True,
        }, msg_id))
        return

    if not user_text:
        return

    ollama_url = _get_service_url("ollama")

    async def _call_agent(agent: dict, prompt: str, ctx_history: list) -> str:
        messages = [{"role": "system", "content": agent["system_prompt"]}]
        messages.extend(ctx_history)
        messages.append({"role": "user", "content": prompt})
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(
                    f"{ollama_url}/api/chat",
                    json={"model": agent.get("model", "qwen2.5:7b"), "messages": messages, "stream": False},
                )
                r.raise_for_status()
                return r.json().get("message", {}).get("content", "")
        except Exception as e:
            return f"[{agent['name']} error: {e}]"

    responses = []

    if pattern == "debate":
        # Both respond independently, then respond to each other
        tasks = [_call_agent(a, user_text, history) for a in agents[:2]]
        r1, r2 = await asyncio.gather(*tasks)
        responses.append({"agent": agents[0]["name"], "text": r1})
        responses.append({"agent": agents[1]["name"], "text": r2})

        # Round 2: each responds to the other
        r1b = await _call_agent(agents[0], f"{agents[1]['name']} said: {r2}\n\nRespond to their point.", history)
        r2b = await _call_agent(agents[1], f"{agents[0]['name']} said: {r1}\n\nRespond to their point.", history)
        responses.append({"agent": agents[0]["name"], "text": r1b, "round": 2})
        responses.append({"agent": agents[1]["name"], "text": r2b, "round": 2})

    elif pattern == "chain":
        # First agent responds, output becomes input for next
        current_text = user_text
        for agent in agents:
            resp = await _call_agent(agent, current_text, history)
            responses.append({"agent": agent["name"], "text": resp})
            current_text = resp

    else:  # collaborate
        # First agent responds, second agent refines
        r1 = await _call_agent(agents[0], user_text, history)
        responses.append({"agent": agents[0]["name"], "text": r1})
        r2 = await _call_agent(agents[1], f"Previous response by {agents[0]['name']}: {r1}\n\nRefine, correct, or add to this response.", history)
        responses.append({"agent": agents[1]["name"], "text": r2})

    # Send combined response
    combined = "\n\n".join(f"**{r['agent']}:** {r['text']}" for r in responses)
    await ws.send_text(_msg("chat.response", {
        "text": combined,
        "done": True,
        "multi": True,
        "responses": responses,
        "emotion": _detect_emotion(responses[-1]["text"]),
    }, msg_id))


# ── Persona Matrix REST endpoints ───────────────────────────────────────────

@router.get("/persona")
async def get_persona():
    """Current persona state — enabled, identity, mood, narrative stats, priority."""
    await _ensure_persona()
    return {
        "enabled": _persona_enabled,
        "priority": _persona_priority,
        **(_persona_matrix.get_status() if _persona_matrix._loaded else {"loaded": False}),
    }


@router.get("/persona/list")
def list_personas():
    """List all available persona files."""
    return _persona_matrix.list_personas()


@router.post("/persona/enable")
async def enable_persona(body: dict):
    """Enable persona and load it. Body: {"id": "kira"} (optional, defaults to config or kira)"""
    global _persona_matrix, _persona_enabled, _persona_init_done, _rag_router
    persona_id = body.get("id", "").strip() or _state.get("config", {}).get("persona", "kira")

    # Save current state if switching
    if _persona_matrix._loaded and _persona_matrix.persona_id != persona_id:
        _persona_matrix.save_state()

    if not _persona_matrix._loaded or _persona_matrix.persona_id != persona_id:
        new_matrix = PersonaMatrix()
        ok = await new_matrix.load(persona_id)
        if not ok:
            return {"error": f"Failed to load persona: {persona_id}"}
        _persona_matrix = new_matrix

    _persona_enabled = True
    _persona_init_done = True
    _state["config"]["persona_enabled"] = True
    _state["config"]["persona"] = persona_id
    _save_state(_state)

    # Re-init RAG router for new persona
    _rag_router = RagRouter(
        knowledge_client=_knowledge_client,
        config=_state["config"],
        persona_id=persona_id,
    )

    return {"enabled": True, "persona": persona_id, **_persona_matrix.get_status()}


@router.post("/persona/disable")
async def disable_persona():
    """Disable persona — reverts to static system_prompt."""
    global _persona_enabled
    if _persona_matrix._loaded:
        _persona_matrix.save_state()
    _persona_enabled = False
    _state["config"]["persona_enabled"] = False
    _save_state(_state)
    return {"enabled": False}


@router.post("/persona/priority")
async def set_priority(body: dict):
    """Set priority dial. Body: {"priority": 5}

    0-3: Full persona (playful, emotional, personality-first)
    4-6: Balanced (personality + capability)
    7-10: Work mode (minimal persona, max knowledge)
    """
    global _persona_priority
    p = body.get("priority")
    if p is None:
        return {"error": "priority is required (0-10)"}
    _persona_priority = max(0, min(10, int(p)))
    _state["config"]["persona_priority"] = _persona_priority
    _save_state(_state)
    return {"priority": _persona_priority}


@router.post("/persona/create")
def create_persona(body: dict):
    """Create a new persona file. Body: full persona JSON (id, name, identity, etc.)

    Minimal example:
    {"id": "rex", "name": "Rex", "identity": {"core": "You are Rex, a no-nonsense engineer."}}
    """
    persona_id = body.get("id", "").strip()
    if not persona_id:
        return {"error": "id is required"}
    if not re.match(r'^[a-z0-9_-]+$', persona_id):
        return {"error": "id must be lowercase alphanumeric (a-z, 0-9, -, _)"}

    personas_dir = Path(__file__).parent / "personas"
    personas_dir.mkdir(exist_ok=True)
    target = personas_dir / f"{persona_id}.json"

    if target.exists():
        return {"error": f"Persona '{persona_id}' already exists. Use PUT /persona/{{id}} to update."}

    # Ensure minimum structure
    persona = {
        "id": persona_id,
        "name": body.get("name", persona_id.title()),
        "version": 1,
        "identity": body.get("identity", {
            "core": f"You are {body.get('name', persona_id.title())}.",
            "personality": [],
            "speech_style": "",
            "boundaries": [],
            "user_facts": {},
        }),
        "priority_prompts": body.get("priority_prompts", {
            "low": "",
            "mid": "",
            "high": "",
        }),
        "voice": body.get("voice", {}),
    }

    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(persona, indent=2))
    tmp.rename(target)
    return {"created": persona_id, "path": str(target)}


@router.put("/persona/{persona_id}")
def update_persona(persona_id: str, body: dict):
    """Update an existing persona file. Body: partial or full persona JSON.

    Only provided fields are merged — omitted fields keep their current values.
    """
    personas_dir = Path(__file__).parent / "personas"
    target = personas_dir / f"{persona_id}.json"

    if not target.exists():
        return {"error": f"Persona '{persona_id}' not found"}

    try:
        current = json.loads(target.read_text())
    except Exception as e:
        return {"error": f"Failed to read persona: {e}"}

    # Deep merge: update top-level keys, merge dicts one level deep
    for key, value in body.items():
        if key == "id":
            continue  # don't allow id change
        if isinstance(value, dict) and isinstance(current.get(key), dict):
            current[key].update(value)
        else:
            current[key] = value

    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, indent=2))
    tmp.rename(target)

    # If this is the active persona, reload it
    if _persona_matrix._loaded and _persona_matrix.persona_id == persona_id:
        # Reload identity from disk (keep runtime state)
        try:
            data = json.loads(target.read_text())
            _persona_matrix.identity = data.get("identity", {})
            _persona_matrix.priority_prompts = data.get("priority_prompts", {})
            _persona_matrix.voice_config = data.get("voice", {})
        except Exception:
            pass

    return {"updated": persona_id}


@router.delete("/persona/{persona_id}")
def delete_persona(persona_id: str):
    """Delete a persona file. Cannot delete the currently active persona."""
    if _persona_matrix._loaded and _persona_matrix.persona_id == persona_id and _persona_enabled:
        return {"error": f"Cannot delete active persona '{persona_id}'. Disable or switch first."}

    personas_dir = Path(__file__).parent / "personas"
    target = personas_dir / f"{persona_id}.json"
    if not target.exists():
        return {"error": f"Persona '{persona_id}' not found"}

    target.unlink()

    # Also remove state file if it exists
    state_file = Path(__file__).parent / "persona_state" / f"{persona_id}_state.json"
    if state_file.exists():
        state_file.unlink()

    return {"deleted": persona_id}


@router.get("/persona/narrative")
async def get_narrative():
    """Return the narrative buffer — recent exchanges and summaries."""
    if not _persona_matrix._loaded:
        return {"exchanges": [], "summaries": []}
    return _persona_matrix.narrative.to_dict()


@router.get("/persona/mood")
async def get_mood():
    """Return the current mood drift state."""
    if not _persona_matrix._loaded:
        return {"baseline": "content", "valence": 0.0, "energy": 0.5}
    return _persona_matrix.mood.to_dict()


@router.get("/persona/prompt")
async def get_persona_prompt(priority: int | None = None, observation: str = ""):
    """Return the fully-built system prompt for the current persona.

    Used by external integrations (Open WebUI #Kira pipe) to get a
    ready-made prompt without duplicating build_prompt() logic.

    Query params:
        priority: override priority dial (0-10), defaults to current setting
        observation: ephemeral context string to include
    """
    await _ensure_persona()
    if not _persona_enabled or not _persona_matrix._loaded:
        return {"prompt": None, "enabled": False}

    p = priority if priority is not None else _persona_priority
    prompt = _persona_matrix.build_prompt(priority=p, observation=observation)
    prompt += "\n\n" + _EMOTION_TAG_INSTRUCTION

    return {
        "prompt": prompt,
        "enabled": True,
        "persona_id": _persona_matrix.persona_id,
        "priority": p,
        "mood": _persona_matrix.mood.to_dict(),
    }


@router.post("/persona/record")
async def record_exchange(body: dict):
    """Record an exchange in the narrative buffer.

    Used by external integrations (Open WebUI #Kira pipe) to feed
    conversation data back into the persona's narrative memory.

    Body: {"role": "user"|"assistant", "text": "...", "emotion": "happy"}
    """
    if not _persona_enabled or not _persona_matrix._loaded:
        return {"error": "persona not enabled"}

    role = body.get("role", "").strip()
    text = body.get("text", "").strip()
    emotion = body.get("emotion", "")

    if role not in ("user", "assistant") or not text:
        return {"error": "role (user|assistant) and text are required"}

    source = body.get("source", "webui")
    _persona_matrix.record_exchange(role, text, emotion, source=source)
    return {"recorded": True, "narrative_exchanges": len(_persona_matrix.narrative.exchanges)}


@router.post("/persona/save")
async def save_persona_state():
    """Force-save persona state to disk."""
    if _persona_matrix._loaded:
        _persona_matrix.save_state()
        return {"saved": _persona_matrix.persona_id}
    return {"error": "no persona loaded"}


# ── Knowledge REST endpoints ────────────────────────────────────────────────

@router.get("/knowledge")
async def list_knowledge():
    """List available knowledge bases from Open WebUI."""
    await _ensure_persona()
    if not _knowledge_client:
        return {"error": "knowledge client not configured — set webui_api_key in config"}
    return await _knowledge_client.list_knowledge_bases()


@router.post("/knowledge/query")
async def query_knowledge(body: dict):
    """Query the knowledge store. Body: {"query": "...", "n_results": 5}"""
    await _ensure_persona()
    if not _knowledge_client:
        return {"error": "knowledge client not configured — set webui_api_key in config"}

    query = body.get("query", "").strip()
    if not query:
        return {"error": "query is required"}

    results = await _knowledge_client.query(
        text=query,
        collection_name=body.get("collection", ""),
        n_results=body.get("n_results", 5),
    )
    return [{"text": r.text, "source": r.source, "score": r.score} for r in results]


# ── Notes REST endpoints ─────────────────────────────────────────────────

@router.get("/notes")
async def list_notes():
    """List all personal notes."""
    if not _rag_router or not _rag_router.notes:
        return {"error": "notes not available — persona not loaded"}
    return _rag_router.notes.list_all()


@router.post("/notes")
async def add_note(body: dict):
    """Add a personal note. Body: {"key": "...", "value": "..."}"""
    if not _rag_router or not _rag_router.notes:
        return {"error": "notes not available — persona not loaded"}
    key = body.get("key", "").strip()
    value = body.get("value", "").strip()
    if not key or not value:
        return {"error": "key and value are required"}
    _rag_router.notes.add(key, value)
    return {"added": key}


@router.post("/notes/search")
async def search_notes(body: dict):
    """Search personal notes. Body: {"query": "...", "max_results": 5}"""
    if not _rag_router or not _rag_router.notes:
        return {"error": "notes not available — persona not loaded"}
    query = body.get("query", "").strip()
    if not query:
        return {"error": "query is required"}
    return _rag_router.notes.search(query, body.get("max_results", 5))


@router.delete("/notes/{key}")
async def delete_note(key: str):
    """Delete a personal note by key."""
    if not _rag_router or not _rag_router.notes:
        return {"error": "notes not available — persona not loaded"}
    if _rag_router.notes.delete(key):
        return {"deleted": key}
    return {"error": f"note '{key}' not found"}


# ── Episodes REST endpoints ──────────────────────────────────────────────

@router.get("/episodes")
async def list_episodes():
    """List recent episodic memories."""
    if not _rag_router or not _rag_router.episodes:
        return {"error": "episodes not available — persona not loaded"}
    return _rag_router.episodes.list_recent(20)


@router.post("/episodes/search")
async def search_episodes(body: dict):
    """Search episodic memories. Body: {"query": "...", "max_results": 3}"""
    if not _rag_router or not _rag_router.episodes:
        return {"error": "episodes not available — persona not loaded"}
    query = body.get("query", "").strip()
    if not query:
        return {"error": "query is required"}
    return _rag_router.episodes.search(query, body.get("max_results", 3))


# ── Vision Memory REST endpoints ─────────────────────────────────────────

@router.get("/vision/memory")
async def list_vision_memory():
    """List recent vision memory entries."""
    if not _rag_router or not _rag_router.vision:
        return {"error": "vision memory not available — persona not loaded"}
    return _rag_router.vision.list_recent(10)


@router.post("/vision/memory/search")
async def search_vision_memory(body: dict):
    """Search vision memory. Body: {"query": "...", "max_results": 3}"""
    if not _rag_router or not _rag_router.vision:
        return {"error": "vision memory not available — persona not loaded"}
    query = body.get("query", "").strip()
    if not query:
        return {"error": "query is required"}
    return _rag_router.vision.search(query, body.get("max_results", 3))
