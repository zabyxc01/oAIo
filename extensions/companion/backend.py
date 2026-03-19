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


# ── Config ───────────────────────────────────────────────────────────────────

_EMOTION_TAG_INSTRUCTION = (
    "IMPORTANT: Begin EVERY response with an emotion tag in this exact format: "
    "[emotion:intensity] where emotion is one of: happy, angry, sad, surprised, "
    "relaxed, neutral, blush, sleepy, thinking, shy, bored, serious, curious, love "
    "and intensity is a decimal from 0.0 to 1.0 indicating strength. "
    "You may optionally add a second tag for blended emotions. "
    "Examples: [happy:0.8] [curious:0.3] Hey that's really cool! | "
    "[sad:0.6] I'm sorry to hear that. | [thinking:0.4] Hmm let me consider that. "
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
            "ollama_model": "gemma3:latest",
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

    # Build messages array with emotion tag instruction
    system_prompt = cfg["system_prompt"] + "\n\n" + _EMOTION_TAG_INSTRUCTION
    # Append ambient context to system prompt (NOT as user message)
    if context:
        system_prompt += "\n\n--- Current situation ---\n" + context
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    # Call ollama
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{ollama_url}/api/chat",
                json={
                    "model": cfg["ollama_model"],
                    "messages": messages,
                    "stream": False,
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

    tts_mode = cfg.get("tts_mode", "batch")

    # Build response with full emotion payload
    chat_payload = {
        "text": display_text,
        "done": True,
        "emotion": emotion_data,  # Full emotion dict instead of just string
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


# ── WebSocket endpoint ───────────────────────────────────────────────────────
@router.websocket("/ws")
async def companion_ws(websocket: WebSocket):
    """Bidirectional companion protocol over WebSocket."""
    await websocket.accept()

    client_id = uuid.uuid4().hex[:12]
    client_info = {"platform": "unknown", "name": f"companion-{client_id[:6]}"}
    _connected_clients[client_id] = {
        "info": client_info,
        "connected_at": time.time(),
    }

    print(f"[companion] client connected: {client_id}")

    cfg = _state["config"]
    await websocket.send_text(_msg("state.sync", {
        "hub_status": "online",
        "services": {
            "ollama": _get_service_url("ollama"),
            "kokoro-tts": _get_service_url("kokoro-tts"),
            "faster-whisper": _get_service_url("faster-whisper"),
        },
        "config": {
            "model": cfg["ollama_model"],
            "voice": cfg["tts_voice"],
        },
        "emotion": _emotion_state.to_dict(),
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
def patch_config(body: dict):
    cfg = _state["config"]
    allowed = {"ollama_model", "tts_voice", "tts_engine", "tts_compress", "tts_mode", "ref_audio", "system_prompt", "vision_model"}
    updated = {}
    for key in allowed:
        if key in body:
            cfg[key] = body[key]
            updated[key] = body[key]
    if updated:
        _save_state(_state)
    return {"updated": updated, "config": dict(cfg)}


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
        "model": body.get("model", "gemma3:latest"),
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
    model = payload.get("model") or _state["config"].get("vision_model", "llama3.2-vision:11b")

    if not image_b64:
        return

    cfg = _state["config"]
    ollama_url = _get_service_url("ollama")

    system_prompt = cfg["system_prompt"] + "\n\n" + _EMOTION_TAG_INSTRUCTION
    if context:
        system_prompt += "\n\n--- Current situation ---\n" + context

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
                    json={"model": agent.get("model", "gemma3:latest"), "messages": messages, "stream": False},
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
