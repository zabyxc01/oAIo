import io
import os
import re
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
WEIGHTS_DIR = os.environ.get("weight_root", "assets/weights")

print("Initializing RVC...", flush=True)
config = Config()
vc = VC(config)
vc.get_vc(RVC_MODEL)
_current_model = RVC_MODEL
print(f"RVC ready — model: {RVC_MODEL}", flush=True)


def _ensure_model(model_name: str) -> str:
    """Load the requested RVC model if not already loaded.
    Returns the index path to use. Raises ValueError if model not found."""
    global _current_model
    if not model_name or model_name == _current_model:
        return RVC_INDEX if _current_model == RVC_MODEL else ""
    # Validate model name contains only safe characters (prevent path traversal)
    if not re.match(r'^[a-zA-Z0-9_.-]+$', model_name):
        raise ValueError(f"Invalid model name: {model_name}")
    # Validate model exists in weights directory
    model_path = os.path.join(WEIGHTS_DIR, model_name)
    if not os.path.isfile(model_path):
        raise ValueError(f"RVC model not found: {model_name}")
    print(f"[PROXY] Switching RVC model: {_current_model} -> {model_name}", flush=True)
    vc.get_vc(model_name)
    _current_model = model_name
    # Try to find a matching index file
    stem = os.path.splitext(model_name)[0]
    for idx_file in os.listdir("/rvc/assets/indices"):
        if stem.lower() in idx_file.lower() and idx_file.endswith(".index"):
            return f"/rvc/assets/indices/{idx_file}"
    return ""


def _rvc_to_mp3(input_path: str, transpose: int = 0, index_path: str = None) -> bytes:
    """Run vc_single on input_path, return MP3 bytes. Falls back to None on failure."""
    idx = index_path if index_path is not None else RVC_INDEX
    status, result = vc.vc_single(
        0,          # speaker id
        input_path,
        transpose,
        None,       # f0 file
        "rmvpe",
        idx,
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
    rvc_model: str = ""


@app.get("/v1/models")
def list_models():
    return {"object": "list", "data": [{"id": "tts-1", "object": "model"}]}


@app.post("/v1/audio/speech")
async def synthesize(req: SpeechRequest):
    # Switch RVC model if requested
    index_path = None
    if req.rvc_model:
        try:
            index_path = _ensure_model(req.rvc_model)
        except ValueError as e:
            return Response(content=f'{{"error":"{e}"}}'.encode(),
                            media_type="application/json", status_code=400)

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

    mp3_bytes = _rvc_to_mp3(tmp_in, index_path=index_path)
    os.unlink(tmp_in)

    if mp3_bytes is None:
        print(f"[PROXY] RVC failed, falling back to Kokoro output", flush=True)
        return Response(content=r.content, media_type="audio/mpeg")

    print(f"[PROXY] speech voice={req.voice} rvc_model={_current_model} len={len(req.input)} out={len(mp3_bytes)}b", flush=True)
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
    model: str = Form(default=""),
):
    """Audio file → RVC voice conversion → MP3."""
    # Switch RVC model if requested
    index_path = None
    if model:
        try:
            index_path = _ensure_model(model)
        except ValueError as e:
            return Response(content=f'{{"error":"{e}"}}'.encode(),
                            media_type="application/json", status_code=400)

    _allowed_suffixes = {".wav", ".mp3", ".ogg", ".flac", ".m4a"}
    _ext = os.path.splitext(file.filename or "")[1].lower()
    _ext = _ext if _ext in _allowed_suffixes else ".wav"
    tmp_in = f"/tmp/rvc_in_{uuid.uuid4().hex}{_ext}"
    with open(tmp_in, "wb") as f:
        f.write(await file.read())

    try:
        mp3_bytes = _rvc_to_mp3(tmp_in, transpose, index_path=index_path)
    finally:
        os.unlink(tmp_in)

    if mp3_bytes is None:
        return Response(content=b'{"error":"RVC inference returned no output"}',
                        media_type="application/json", status_code=500)

    print(f"[PROXY] convert file={file.filename} model={_current_model} transpose={transpose} out={len(mp3_bytes)}b", flush=True)
    return Response(
        content=mp3_bytes,
        media_type="audio/mpeg",
        headers={"Content-Disposition": "inline; filename=converted.mp3"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
