"""
Companion extension — WebSocket relay for desktop + phone AI companion clients.
Mounted at /extensions/companion by the extension loader.

Endpoints:
  WS  /ws             — bidirectional companion protocol
  GET  /config        — companion configuration (model, voice, system prompt)
  PATCH /config       — update configuration
  GET  /clients       — list connected clients
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
    """Resolve a service URL from oAIo's services config.
    Uses Docker container names since we're running inside the Docker network."""
    try:
        cfg = json.loads(_SERVICES_FILE.read_text())
        svc = cfg.get("services", {}).get(service_name, {})
        container = svc.get("container", service_name)
        port = svc.get("port")
        if container and port:
            return f"http://{container}:{port}"
    except Exception:
        pass
    # Fallback defaults (Docker container names)
    defaults = {
        "ollama": "http://ollama:11434",
        "kokoro-tts": "http://kokoro-tts:8000",
        "faster-whisper": "http://faster-whisper:8003",
    }
    return defaults.get(service_name, f"http://{service_name}:8000")


# ── Fleet integration ────────────────────────────────────────────────────────
async def _register_fleet_node(client_id: str, client_info: dict) -> None:
    """Register a companion client as a thin-client in fleet via its REST API."""
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
    """Mark companion client as unreachable in fleet."""
    try:
        # Find node by URL pattern
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


# ── Emotion detection ────────────────────────────────────────────────────────
# Simple keyword-based emotion detection. Maps to VRM expression presets:
# happy, angry, sad, surprised, relaxed, neutral

_EMOTION_KEYWORDS = {
    "happy": [
        "haha", "lol", "😄", "😊", "😁", "🤣", "glad", "awesome", "great",
        "love", "yay", "nice", "wonderful", "fantastic", "excited", "fun",
        "enjoy", "happy", "laugh", "hehe", "sweet", "cool", "amazing",
    ],
    "angry": [
        "angry", "furious", "mad", "annoyed", "frustrated", "ugh",
        "hate", "pissed", "irritated", "damn", "hell",
    ],
    "sad": [
        "sad", "sorry", "unfortunately", "miss", "lonely", "cry",
        "disappointing", "sigh", "😢", "😞", "😔", "heartbreaking",
    ],
    "surprised": [
        "wow", "whoa", "oh!", "really?", "seriously?", "no way",
        "unexpected", "surprised", "😮", "😲", "shocking", "what?!",
    ],
    "relaxed": [
        "chill", "relax", "calm", "peaceful", "cozy", "comfy",
        "easy", "mellow", "gentle", "soft", "quiet",
    ],
}


def _detect_emotion(text: str) -> str:
    """Detect emotion from response text.

    Parses gemma's natural stage directions like (smiling), (puzzled look),
    (laughs), bracket tags like [happy], and falls back to keyword matching.
    """
    text_stripped = text.strip()

    # Try parenthetical stage directions: (smiling), (puzzled look on face), etc.
    paren_match = re.match(r'^\(([^)]+)\)', text_stripped)
    if paren_match:
        direction = paren_match.group(1).lower()
        # Map common stage directions to VRM expressions
        for keyword, emotion in _STAGE_DIRECTION_MAP.items():
            if keyword in direction:
                return emotion

    # Try bracket tags: [happy], [sad], etc.
    bracket_match = re.match(r'^\[(\w+)\]', text_stripped)
    if bracket_match:
        tag = bracket_match.group(1).lower()
        if tag in _BRACKET_TAG_MAP:
            return _BRACKET_TAG_MAP[tag]

    # Fallback: keyword matching on full text
    text_lower = text.lower()
    scores = {emotion: 0 for emotion in _EMOTION_KEYWORDS}
    for emotion, keywords in _EMOTION_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                scores[emotion] += 1
    best = max(scores, key=scores.get)
    if scores[best] > 0:
        return best
    return "neutral"


# Stage direction keywords → VRM expression mapping
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
    "wide eye": "surprised", "blink": "surprised", "stunned": "surprised",
    "puzzl": "surprised", "confused": "surprised", "tilt": "surprised",
    "calm": "relaxed", "relax": "relaxed", "soft": "relaxed",
    "gentle": "relaxed", "peaceful": "relaxed", "warm": "relaxed",
    "nod": "relaxed", "thoughtful": "relaxed", "ponder": "relaxed",
}

_BRACKET_TAG_MAP = {
    "happy": "happy", "excited": "happy", "playful": "happy",
    "sad": "sad", "worried": "sad",
    "angry": "angry",
    "surprised": "surprised",
    "relaxed": "relaxed", "thoughtful": "relaxed",
    "neutral": "neutral",
}


def _extract_stage_directions(text: str) -> list[str]:
    """Extract all parenthetical stage directions from the text."""
    return re.findall(r'\(([^)]+)\)', text)


def _strip_stage_directions(text: str) -> str:
    """Remove parenthetical stage directions from text for TTS (so she doesn't say them aloud)."""
    return re.sub(r'\([^)]+\)\s*', '', text).strip()


# ── Pipeline handlers ────────────────────────────────────────────────────────
async def _handle_chat_request(ws: WebSocket, msg: dict) -> None:
    """Process chat.request: call ollama, then TTS, send both back."""
    payload = msg.get("payload", {})
    msg_id = msg.get("id", "")
    user_text = payload.get("text", "").strip()
    history = payload.get("history", [])

    if not user_text:
        return

    cfg = _state["config"]
    ollama_url = _get_service_url("ollama")

    # Build messages array
    messages = [{"role": "system", "content": cfg["system_prompt"]}]
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

    # Detect emotion from response text
    emotion = _detect_emotion(response_text)

    # Strip stage directions for TTS (don't say them aloud)
    # but keep full text in chat response so user sees the performance
    tts_text = _strip_stage_directions(response_text)
    directions = _extract_stage_directions(response_text)
    if directions:
        print(f"[companion] stage directions: {directions}")

    tts_mode = cfg.get("tts_mode", "batch")

    if tts_mode == "stream" and tts_text:
        await ws.send_text(_msg("chat.response", {
            "text": response_text,
            "done": True,
            "emotion": emotion,
        }, msg_id))
        await _stream_tts_sentences(ws, tts_text, msg_id, cfg)
    else:
        await ws.send_text(_msg("chat.response", {
            "text": response_text,
            "done": True,
            "emotion": emotion,
        }, msg_id))
        if tts_text:
            await _generate_and_send_tts(ws, tts_text, msg_id)


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences for streaming TTS."""
    # Split on sentence-ending punctuation followed by space or end
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    # Merge very short fragments (under 10 chars) with the previous sentence
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
    """Split text into sentences and TTS each one, sending audio chunks as they're ready."""
    sentences = _split_sentences(text)
    print(f"[companion] Streaming {len(sentences)} sentences")

    for i, sentence in enumerate(sentences):
        try:
            tts_engine = cfg.get("tts_engine", "kokoro")
            if tts_engine == "indextts":
                audio = await _generate_audio_bytes(sentence, cfg, "indextts")
            elif tts_engine == "oaudio":
                audio = await _generate_audio_bytes(sentence, cfg, "oaudio")
            elif tts_engine == "f5":
                audio = await _generate_audio_bytes(sentence, cfg, "f5")
            else:
                audio = await _generate_audio_bytes(sentence, cfg, "kokoro")

            if audio:
                await _send_audio(ws, audio, f"{ref_id}_chunk{i}", cfg)
        except Exception as e:
            print(f"[companion] Stream TTS error on sentence {i}: {e}")


async def _generate_audio_bytes(text: str, cfg: dict, engine: str) -> bytes | None:
    """Generate TTS audio bytes without sending — for streaming use."""
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
            from pathlib import Path
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
            from pathlib import Path
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
    """Generate TTS audio and send to client. Supports kokoro and indextts engines."""
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
    """Send audio to client, compressing if needed based on tts_compress setting."""
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
    """Kokoro TTS — fast, no voice cloning."""
    tts_url = _get_service_url("kokoro-tts")
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{tts_url}/v1/audio/speech",
            json={
                "input": text,
                "voice": cfg.get("tts_voice", "af_heart"),
                "response_format": "wav",
            },
        )
        r.raise_for_status()

    await _send_audio(ws, r.content, ref_id, cfg)


async def _compress_wav_to_mp3(wav_bytes: bytes) -> bytes:
    """Compress WAV to MP3 using ffmpeg — ~10x smaller for WebSocket transfer."""
    import subprocess
    proc = subprocess.run(
        ["ffmpeg", "-i", "pipe:0", "-f", "mp3", "-ab", "48k", "-ar", "24000", "-ac", "1", "pipe:1"],
        input=wav_bytes, capture_output=True,
    )
    if proc.returncode != 0:
        print(f"[companion] ffmpeg compress failed: {proc.stderr[:200]}")
        return wav_bytes  # fallback to raw WAV
    return proc.stdout


async def _tts_indextts(ws: WebSocket, text: str, ref_id: str, cfg: dict) -> None:
    """IndexTTS — voice cloning with reference audio."""
    tts_url = _get_service_url("indextts")
    ref_audio_path = cfg.get("ref_audio", "/mnt/oaio/ref-audio/avatar_voice.wav")

    from pathlib import Path
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

    # Compress to MP3 then convert back to WAV for Godot playback
    # This shrinks the WebSocket payload dramatically
    import subprocess
    proc = subprocess.run(
        ["ffmpeg", "-i", "pipe:0", "-f", "wav", "-ar", "24000", "-ac", "1",
         "-acodec", "pcm_s16le", "-sample_fmt", "s16", "pipe:1"],
        input=r.content, capture_output=True,
    )
    if proc.returncode == 0:
        wav_bytes = proc.stdout
    else:
        wav_bytes = r.content

    await _send_audio(ws, wav_bytes, ref_id, cfg)


async def _tts_oaudio(ws: WebSocket, text: str, ref_id: str, cfg: dict) -> None:
    """oAudio pipeline — kokoro → RVC voice conversion → output."""
    import io
    import subprocess

    oaudio_url = "http://localhost:8002"  # oAudio runs in the same container
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

    # oAudio returns MP3 — send directly as MP3 (Godot handles it)
    audio_b64 = base64.b64encode(r.content).decode("ascii")
    print(f"[companion] oAudio: sending {len(audio_b64)//1024}KB (mp3)")
    await ws.send_text(_msg("tts.audio", {
        "audio_b64": audio_b64,
        "format": "mp3",
        "sample_rate": 24000,
    }, ref_id))


async def _tts_f5(ws: WebSocket, text: str, ref_id: str, cfg: dict) -> None:
    """F5-TTS via oAudio /clone — voice cloning with auto-transcription."""
    from pathlib import Path

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
    """Process stt.audio: forward to faster-whisper, return transcript."""
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

    # Auto-chain: if we got a transcript, run it through chat
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

    # Send initial state sync
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
                # Client reporting its capabilities
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
    allowed = {"ollama_model", "tts_voice", "tts_engine", "tts_compress", "tts_mode", "ref_audio", "system_prompt"}
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
