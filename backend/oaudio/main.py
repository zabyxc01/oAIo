"""
oAudio API — unified voice pipeline.
Port: 8002
"""
import io
import os
import uuid
import json as json_lib
import httpx
import soundfile as sf
from pydub import AudioSegment
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from contextlib import asynccontextmanager

# ── Optional API token auth ──────────────────────────────────────────────────
_API_TOKEN = os.environ.get("OAIO_API_TOKEN", "").strip() or None


class TokenAuthMiddleware:
    """ASGI middleware — optional Bearer token auth.

    No-op if OAIO_API_TOKEN is unset.  All oAudio endpoints require auth.
    """
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if _API_TOKEN is None:
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        auth_val = headers.get(b"authorization", b"").decode("utf-8", errors="replace")
        if auth_val == f"Bearer {_API_TOKEN}":
            await self.app(scope, receive, send)
            return

        resp = JSONResponse({"error": "unauthorized"}, status_code=401)
        await resp(scope, receive, send)


_ALLOWED_AUDIO_SUFFIXES = {".wav", ".mp3", ".ogg", ".flac", ".m4a"}


def _safe_suffix(filename: str | None) -> str:
    """Extract and validate audio file extension. Returns '.wav' if invalid or missing."""
    if not filename:
        return ".wav"
    suffix = Path(filename).suffix.lower()
    return suffix if suffix in _ALLOWED_AUDIO_SUFFIXES else ".wav"

KOKORO_URL  = os.environ.get("KOKORO_URL",  "http://localhost:8000")
RVC_PROXY   = os.environ.get("RVC_PROXY",   "http://localhost:8001")
RVC_GRADIO  = os.environ.get("RVC_GRADIO",  "http://rvc:7865")
F5_TTS_URL  = os.environ.get("F5_TTS_URL",  "http://f5-tts:7860")

OUTPUT_BASE = Path(os.environ.get("OUTPUT_BASE", "/rvc/audio"))
PROXY_OUT   = OUTPUT_BASE / "proxy"
WEBUI_OUT   = OUTPUT_BASE / "webui"
CLONE_OUT   = OUTPUT_BASE / "clone"
TEMP_OUT    = OUTPUT_BASE / "temp"
HF_CACHE    = Path(os.environ.get("HF_HOME", "/hf-cache"))

# Lazy-loaded Whisper model for ref_text auto-transcription
_whisper_model = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Pre-load Whisper model at startup so first /clone isn't slow
    global _whisper_model
    try:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel("tiny", device="cpu",
                                      download_root=str(HF_CACHE / "whisper"))
    except Exception as e:
        print(f"[oAudio] Whisper pre-load failed (will lazy-load): {e}")
    yield


app = FastAPI(title="oAudio", version="0.1.0", lifespan=lifespan)

class SecurityHeadersMiddleware:
    """ASGI middleware — sets security headers on every HTTP response."""
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"content-security-policy",
                    b"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' ws: wss:; frame-ancestors 'none'"))
                headers.append((b"x-frame-options", b"DENY"))
                headers.append((b"x-content-type-options", b"nosniff"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:9000", "http://127.0.0.1:9000", "http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SecurityHeadersMiddleware)
if _API_TOKEN:
    app.add_middleware(TokenAuthMiddleware)

for d in [PROXY_OUT, WEBUI_OUT, CLONE_OUT, TEMP_OUT]:
    d.mkdir(parents=True, exist_ok=True)


def _transcribe(audio_bytes: bytes, filename: str) -> str:
    """Auto-transcribe reference audio using faster-whisper (tiny model, CPU)."""
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel("tiny", device="cpu",
                                      download_root=str(HF_CACHE / "whisper"))
    tmp = TEMP_OUT / f"ref_{uuid.uuid4().hex}{_safe_suffix(filename)}"
    tmp.write_bytes(audio_bytes)
    try:
        segments, _ = _whisper_model.transcribe(str(tmp), beam_size=1)
        return " ".join(s.text.strip() for s in segments)
    finally:
        tmp.unlink(missing_ok=True)


class SpeakRequest(BaseModel):
    text: str
    voice: str = "shimmer"
    model: str = "GOTHMOMMY.pth"


@app.get("/status")
def status():
    return {"service": "oAudio", "version": "0.1.0", "status": "ok"}


@app.get("/voices")
async def list_voices():
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{KOKORO_URL}/v1/audio/voices")
        return r.json()


@app.post("/speak")
async def speak(req: SpeakRequest):
    """Text → Kokoro → RVC → MP3"""
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{RVC_PROXY}/v1/audio/speech",
            json={"input": req.text, "voice": req.voice, "rvc_model": req.model}
        )
    r.raise_for_status()
    out = PROXY_OUT / f"speak_{uuid.uuid4().hex}.mp3"
    out.write_bytes(r.content)
    try:
        return Response(content=r.content, media_type="audio/mpeg")
    finally:
        out.unlink(missing_ok=True)


@app.post("/convert")
async def convert(
    file: UploadFile = File(...),
    model: str = Form(default="GOTHMOMMY.pth"),
    transpose: int = Form(default=0),
):
    """Audio file → RVC proxy /convert → MP3."""
    audio_bytes = await file.read()
    async with httpx.AsyncClient(timeout=180.0) as client:
        r = await client.post(
            f"{RVC_PROXY}/convert",
            files={"file": (f"upload{_safe_suffix(file.filename)}", audio_bytes, file.content_type or "audio/wav")},
            data={"transpose": transpose, "model": model},
        )
    r.raise_for_status()
    return Response(content=r.content, media_type="audio/mpeg")


@app.post("/clone")
async def clone(
    ref_audio: UploadFile = File(...),
    ref_text: str = Form(default=""),
    target_text: str = Form(default=""),
    speed: float = Form(default=1.0),
    remove_silence: bool = Form(default=False),
):
    """Reference audio + text → F5-TTS voice cloning → WAV.
    If ref_text is empty, auto-transcribes the reference audio via Whisper tiny.
    """
    audio_bytes = await ref_audio.read()

    if not ref_text.strip():
        import asyncio
        loop = asyncio.get_event_loop()
        ref_text = await loop.run_in_executor(
            None, _transcribe, audio_bytes, ref_audio.filename or "ref.wav"
        )

    async with httpx.AsyncClient(timeout=300.0) as client:
        # 1. Upload ref audio to F5-TTS
        upload_r = await client.post(
            f"{F5_TTS_URL}/gradio_api/upload",
            files={"files": (f"ref{_safe_suffix(ref_audio.filename)}", audio_bytes,
                             ref_audio.content_type or "audio/wav")},
        )
        upload_r.raise_for_status()
        uploaded = upload_r.json()
        uploaded_path = uploaded[0] if isinstance(uploaded, list) else uploaded

        # 2. Kick off basic_tts job
        call_r = await client.post(
            f"{F5_TTS_URL}/gradio_api/call/basic_tts",
            json={"data": [
                {"path": uploaded_path, "meta": {"_type": "gradio.FileData"}},  # ref_audio_input
                ref_text,                 # ref_text_input
                target_text,              # gen_text_input
                remove_silence,           # remove_silence
                True,                     # randomize_seed
                0,                        # seed_input
                0.15,                     # cross_fade_duration_slider
                32,                       # nfe_slider
                speed,                    # speed_slider
            ]}
        )
        call_r.raise_for_status()
        event_id = call_r.json()["event_id"]

        # 3. Stream SSE result
        output_path = None
        async with client.stream(
            "GET", f"{F5_TTS_URL}/gradio_api/call/basic_tts/{event_id}"
        ) as stream:
            async for line in stream.aiter_lines():
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    try:
                        payload = json_lib.loads(raw)
                    except json_lib.JSONDecodeError:
                        print(f"[clone] SSE non-JSON data: {raw[:200]}")
                        continue
                    print(f"[clone] SSE payload type={type(payload).__name__} len={len(payload) if isinstance(payload, list) else 'n/a'}")
                    if isinstance(payload, list) and payload:
                        item = payload[0]
                        output_path = (
                            item.get("path") or item.get("name")
                            if isinstance(item, dict) else item
                        )
                        break

        if not output_path:
            return {"error": "F5-TTS returned no output path"}

        # 4. Fetch the generated audio from F5-TTS
        audio_r = await client.get(
            f"{F5_TTS_URL}/gradio_api/file={output_path}"
        )
        audio_r.raise_for_status()

    out = CLONE_OUT / f"{uuid.uuid4().hex}.wav"
    out.write_bytes(audio_r.content)
    return Response(content=audio_r.content, media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
