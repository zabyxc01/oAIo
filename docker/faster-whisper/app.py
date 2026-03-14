"""
faster-whisper STT API + Gradio UI.
API: port 8003
Gradio UI: port 7880
"""
import os
import uuid
import asyncio
import threading
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "medium")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "auto")
HF_CACHE = Path(os.environ.get("HF_HOME", "/hf-cache"))
TEMP_DIR = Path("/tmp/whisper-uploads")
TEMP_DIR.mkdir(parents=True, exist_ok=True)

_model = None


def _get_device():
    if WHISPER_DEVICE != "auto":
        return WHISPER_DEVICE
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except ImportError:
        pass
    return "cpu"


def _load_model():
    global _model
    if _model is not None:
        return _model
    from faster_whisper import WhisperModel
    device = _get_device()
    compute_type = "float16" if device == "cuda" else "int8"
    print(f"[faster-whisper] Loading model={WHISPER_MODEL} device={device} compute={compute_type}")
    _model = WhisperModel(
        WHISPER_MODEL,
        device=device,
        compute_type=compute_type,
        download_root=str(HF_CACHE / "whisper"),
    )
    print(f"[faster-whisper] Model loaded successfully")
    return _model


def _do_transcribe(filepath: str, language: str) -> dict:
    model = _load_model()
    segments, info = model.transcribe(filepath, language=language, beam_size=5)
    segment_list = []
    full_text = []
    for seg in segments:
        segment_list.append({
            "start": round(seg.start, 2),
            "end": round(seg.end, 2),
            "text": seg.text.strip(),
        })
        full_text.append(seg.text.strip())

    return {
        "text": " ".join(full_text),
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
        "segments": segment_list,
    }


# ── FastAPI ──────────────────────────────────────────────────────────────────
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _load_model)
    # Start Gradio in background thread
    threading.Thread(target=_start_gradio, daemon=True).start()
    yield

app = FastAPI(title="faster-whisper STT", version="0.1.0", lifespan=lifespan)


@app.get("/status")
def status():
    device = _get_device()
    return {
        "service": "faster-whisper",
        "model": WHISPER_MODEL,
        "device": device,
        "status": "ok" if _model is not None else "loading",
    }


@app.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    language: str = Form(default="en"),
):
    audio_bytes = await file.read()
    suffix = Path(file.filename or "audio.wav").suffix or ".wav"
    tmp = TEMP_DIR / f"{uuid.uuid4().hex}{suffix}"
    tmp.write_bytes(audio_bytes)
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _do_transcribe, str(tmp), language)
        return result
    finally:
        tmp.unlink(missing_ok=True)


# ── Gradio UI ────────────────────────────────────────────────────────────────
def _start_gradio():
    import gradio as gr

    def transcribe_ui(audio_path, language):
        if not audio_path:
            return "No audio provided"
        result = _do_transcribe(audio_path, language)
        text = result["text"]
        lang = result["language"]
        prob = result["language_probability"]
        segments = "\n".join(
            f"[{s['start']:.1f}s - {s['end']:.1f}s] {s['text']}"
            for s in result["segments"]
        )
        return f"{text}\n\n--- Details ---\nLanguage: {lang} ({prob})\n\n{segments}"

    with gr.Blocks(title="faster-whisper STT") as demo:
        gr.Markdown("## faster-whisper STT")
        gr.Markdown(f"Model: **{WHISPER_MODEL}** | Device: **{_get_device()}**")
        with gr.Row():
            audio_in = gr.Audio(type="filepath", label="Upload or Record Audio")
            lang = gr.Dropdown(
                choices=["en", "ja", "zh", "ko", "es", "fr", "de", "auto"],
                value="en", label="Language"
            )
        btn = gr.Button("Transcribe")
        output = gr.Textbox(label="Transcription", lines=8)
        btn.click(transcribe_ui, inputs=[audio_in, lang], outputs=output)

    demo.launch(server_name="0.0.0.0", server_port=7880, share=False, quiet=True)
