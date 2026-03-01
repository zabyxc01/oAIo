import io
import os
import sys
import uuid
import httpx
import soundfile as sf
from pydub import AudioSegment
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import Response
from pydantic import BaseModel

sys.path.insert(0, '/rvc')
os.chdir('/rvc')

# Ensure RVC env vars point to correct paths
os.environ['weight_root'] = 'assets/weights'
os.environ['weight_uvr5_root'] = 'assets/uvr5_weights'
os.environ['index_root'] = 'assets/indices'
os.environ['rmvpe_root'] = 'assets/rmvpe'

from configs.config import Config
from infer.modules.vc.modules import VC

app = FastAPI()

KOKORO_URL = os.environ.get("KOKORO_URL", "http://host.docker.internal:8000")
RVC_MODEL  = os.environ.get("RVC_MODEL",  "GOTHMOMMY.pth")
RVC_INDEX  = os.environ.get("RVC_INDEX",  "/rvc/assets/indices/added_GOTHMOMMY_v2.index")

print("Initializing RVC...", flush=True)
config = Config()
vc = VC(config)
vc.get_vc(RVC_MODEL)
print(f"RVC ready — model: {RVC_MODEL}", flush=True)


def _rvc_to_mp3(input_path: str, transpose: int = 0) -> bytes:
    """Run vc_single on input_path, return MP3 bytes. Falls back to None on failure."""
    status, result = vc.vc_single(
        0,          # speaker id
        input_path,
        transpose,
        None,       # f0 file
        "rmvpe",
        RVC_INDEX,
        "",
        0.75,       # index rate
        3,          # filter radius
        0,          # resample sr
        0.25,       # rms mix rate
        0.33,       # protect
    )
    if result is None:
        return None
    tgt_sr, audio_opt = result
    tmp_wav = f"/tmp/rvc_{uuid.uuid4().hex}.wav"
    sf.write(tmp_wav, audio_opt, tgt_sr)
    audio = AudioSegment.from_wav(tmp_wav)
    os.unlink(tmp_wav)
    buf = io.BytesIO()
    audio.export(buf, format="mp3")
    return buf.getvalue()


class SpeechRequest(BaseModel):
    model: str = "tts-1"
    input: str
    voice: str = "alloy"
    response_format: str = "mp3"
    speed: float = 1.0


@app.get("/v1/models")
def list_models():
    return {"object": "list", "data": [{"id": "tts-1", "object": "model"}]}


@app.post("/v1/audio/speech")
async def synthesize(req: SpeechRequest):
    # Step 1: Get TTS audio from Kokoro
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            f"{KOKORO_URL}/v1/audio/speech",
            json={"input": req.input, "voice": req.voice, "speed": req.speed},
        )
    r.raise_for_status()

    tmp_in = f"/tmp/proxy_tts_{uuid.uuid4().hex}.mp3"
    with open(tmp_in, "wb") as f:
        f.write(r.content)

    mp3_bytes = _rvc_to_mp3(tmp_in)
    os.unlink(tmp_in)

    if mp3_bytes is None:
        print(f"[PROXY] RVC failed, falling back to Kokoro output", flush=True)
        return Response(content=r.content, media_type="audio/mpeg")

    print(f"[PROXY] speech voice={req.voice} len={len(req.input)} out={len(mp3_bytes)}b", flush=True)
    return Response(
        content=mp3_bytes,
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": "inline; filename=speech.mp3",
            "Content-Length": str(len(mp3_bytes)),
        },
    )


@app.post("/convert")
async def convert(
    file: UploadFile = File(...),
    transpose: int = Form(default=0),
):
    """Audio file → RVC voice conversion → MP3."""
    tmp_in = f"/tmp/rvc_in_{uuid.uuid4().hex}_{file.filename}"
    with open(tmp_in, "wb") as f:
        f.write(await file.read())

    try:
        mp3_bytes = _rvc_to_mp3(tmp_in, transpose)
    finally:
        os.unlink(tmp_in)

    if mp3_bytes is None:
        return Response(content=b'{"error":"RVC inference returned no output"}',
                        media_type="application/json", status_code=500)

    print(f"[PROXY] convert file={file.filename} transpose={transpose} out={len(mp3_bytes)}b", flush=True)
    return Response(
        content=mp3_bytes,
        media_type="audio/mpeg",
        headers={"Content-Disposition": "inline; filename=converted.mp3"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
