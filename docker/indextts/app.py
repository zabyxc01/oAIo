"""
IndexTTS-2 API + Gradio UI — zero-shot voice cloning TTS with emotion control.
API: port 8004
Gradio UI: port 7890
"""
import os
import sys
import uuid
import asyncio
import threading
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import Response, JSONResponse

MODEL_DIR = Path(os.environ.get("INDEXTTS_MODEL_DIR", "/models"))
HF_CACHE = Path(os.environ.get("HF_HOME", "/hf-cache"))
TEMP_DIR = Path("/tmp/indextts")
TEMP_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR = Path("/output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

os.environ["HF_HUB_CACHE"] = str(HF_CACHE)

sys.path.insert(0, "/app/index-tts")

_tts = None


def _load_model():
    global _tts
    if _tts is not None:
        return _tts

    from indextts.infer_v2 import IndexTTS2

    cfg_path = MODEL_DIR / "config.yaml"
    print(f"[IndexTTS] Loading v2 model from {MODEL_DIR} (cfg={cfg_path})")
    _tts = IndexTTS2(
        cfg_path=str(cfg_path),
        model_dir=str(MODEL_DIR),
        use_fp16=False,
        use_cuda_kernel=False,
    )
    print("[IndexTTS] Model loaded successfully")
    return _tts


def _do_synthesize(ref_path: str, text: str, out_path: str, emo_alpha: float):
    model = _load_model()
    model.infer(
        spk_audio_prompt=ref_path,
        text=text,
        output_path=out_path,
        emo_alpha=emo_alpha,
    )


# ── FastAPI ──────────────────────────────────────────────────────────────────
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    if not any(MODEL_DIR.glob("*.yaml")):
        print(f"[IndexTTS] No config.yaml in {MODEL_DIR}, downloading models...")
        try:
            from huggingface_hub import snapshot_download
            snapshot_download(
                "IndexTeam/IndexTTS-2",
                local_dir=str(MODEL_DIR),
                cache_dir=str(HF_CACHE),
            )
            print("[IndexTTS] Models downloaded")
        except Exception as e:
            print(f"[IndexTTS] Model download failed: {e}")

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _load_model)
    except Exception as e:
        print(f"[IndexTTS] Model pre-load failed (will lazy-load): {e}")

    threading.Thread(target=_start_gradio, daemon=True).start()
    yield


app = FastAPI(title="IndexTTS-2", version="0.1.0", lifespan=lifespan)


@app.get("/status")
def status():
    return {
        "service": "IndexTTS-2",
        "status": "ok" if _tts is not None else "loading",
        "model_dir": str(MODEL_DIR),
    }


@app.post("/synthesize")
async def synthesize(
    ref_audio: UploadFile = File(...),
    text: str = Form(...),
    emo_alpha: float = Form(default=1.0),
):
    audio_bytes = await ref_audio.read()
    suffix = Path(ref_audio.filename or "ref.wav").suffix or ".wav"
    ref_path = TEMP_DIR / f"ref_{uuid.uuid4().hex}{suffix}"
    ref_path.write_bytes(audio_bytes)

    out_path = OUTPUT_DIR / f"{uuid.uuid4().hex}.wav"

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, _do_synthesize, str(ref_path), text, str(out_path), emo_alpha
        )
        wav_bytes = out_path.read_bytes()
        return Response(content=wav_bytes, media_type="audio/wav")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        ref_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)


# ── Gradio UI ────────────────────────────────────────────────────────────────
def _start_gradio():
    import gradio as gr

    def synthesize_ui(ref_audio, text, emo_alpha):
        if not ref_audio:
            return None, "No reference audio provided"
        if not text or not text.strip():
            return None, "No text provided"

        out_path = str(OUTPUT_DIR / f"ui_{uuid.uuid4().hex}.wav")
        try:
            _do_synthesize(ref_audio, text, out_path, emo_alpha)
            return out_path, "Done"
        except Exception as e:
            return None, f"Error: {e}"

    with gr.Blocks(title="IndexTTS-2") as demo:
        gr.Markdown("## IndexTTS-2 — Voice Cloning")
        with gr.Row():
            with gr.Column():
                ref_audio = gr.Audio(type="filepath", label="Reference Audio (5-15s)")
                text_in = gr.Textbox(label="Text to Synthesize", lines=3,
                                     placeholder="Type what you want the cloned voice to say...")
                emo_slider = gr.Slider(0.0, 1.0, value=1.0, step=0.1,
                                       label="Emotion Strength (0=neutral, 1=full)")
                btn = gr.Button("Synthesize", variant="primary")
            with gr.Column():
                audio_out = gr.Audio(label="Output", type="filepath")
                status_out = gr.Textbox(label="Status")
        btn.click(synthesize_ui, inputs=[ref_audio, text_in, emo_slider],
                  outputs=[audio_out, status_out])

    demo.launch(server_name="0.0.0.0", server_port=7890, share=False, quiet=True)
