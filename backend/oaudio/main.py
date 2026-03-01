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

app = FastAPI(title="oAudio", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

KOKORO_URL  = os.environ.get("KOKORO_URL",  "http://localhost:8000")
RVC_PROXY   = os.environ.get("RVC_PROXY",   "http://localhost:8001")
RVC_GRADIO  = os.environ.get("RVC_GRADIO",  "http://rvc:7865")
F5_TTS_URL  = os.environ.get("F5_TTS_URL",  "http://f5-tts:7860")

OUTPUT_BASE = Path(os.environ.get("OUTPUT_BASE", "/rvc/audio"))
PROXY_OUT   = OUTPUT_BASE / "proxy"
WEBUI_OUT   = OUTPUT_BASE / "webui"
CLONE_OUT   = OUTPUT_BASE / "clone"
TEMP_OUT    = OUTPUT_BASE / "temp"

for d in [PROXY_OUT, WEBUI_OUT, CLONE_OUT, TEMP_OUT]:
    d.mkdir(parents=True, exist_ok=True)


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
            json={"input": req.text, "voice": req.voice}
        )
    r.raise_for_status()
    out = PROXY_OUT / "output.mp3"
    out.write_bytes(r.content)
    return Response(content=r.content, media_type="audio/mpeg")


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
            files={"file": (file.filename, audio_bytes, file.content_type or "audio/wav")},
            data={"transpose": transpose},
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
    """Reference audio + text → F5-TTS voice cloning → WAV."""
    audio_bytes = await ref_audio.read()

    async with httpx.AsyncClient(timeout=300.0) as client:
        # 1. Upload ref audio to F5-TTS
        upload_r = await client.post(
            f"{F5_TTS_URL}/gradio_api/upload",
            files={"files": (ref_audio.filename, audio_bytes,
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
                    payload = json_lib.loads(line[5:].strip())
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
