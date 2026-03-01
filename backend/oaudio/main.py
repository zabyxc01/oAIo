"""
oAudio API — unified voice pipeline.
Port: 8002
"""
import io
import os
import httpx
import soundfile as sf
from pydub import AudioSegment
from pathlib import Path
from fastapi import FastAPI, UploadFile, File
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
    model: str = "TADC_Bubble.pth"


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
    model: str = "TADC_Bubble.pth",
    index: str = ""
):
    """Audio file → RVC → WAV"""
    tmp = TEMP_OUT / file.filename
    tmp.write_bytes(await file.read())
    # TODO: call RVC vc_single directly
    # placeholder until RVC exposes a convert endpoint
    return {"status": "pending", "input": str(tmp), "model": model}


@app.post("/clone")
async def clone(
    ref_audio: UploadFile = File(...),
    ref_text: str = "",
    target_text: str = ""
):
    """Reference audio + text → F5-TTS → WAV"""
    tmp = TEMP_OUT / ref_audio.filename
    tmp.write_bytes(await ref_audio.read())
    # TODO: call F5-TTS API directly
    return {"status": "pending", "ref": str(tmp), "target": target_text}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
