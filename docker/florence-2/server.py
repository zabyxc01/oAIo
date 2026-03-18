"""Florence-2 vision-language server for oAIo — Kira can see."""

import base64
import io
from PIL import Image

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Florence-2", description="Vision-language model — describe, caption, detect")

_model = None
_processor = None
_loaded = False


def _load():
    global _model, _processor, _loaded
    if _loaded:
        return
    from transformers import AutoModelForCausalLM, AutoProcessor
    import torch
    _processor = AutoProcessor.from_pretrained("microsoft/Florence-2-base", trust_remote_code=True)
    _model = AutoModelForCausalLM.from_pretrained(
        "microsoft/Florence-2-base", trust_remote_code=True,
        torch_dtype=torch.float32
    ).eval()
    _loaded = True
    print("[florence-2] Model loaded")


class ImageRequest(BaseModel):
    image_b64: str = Field(..., description="Base64-encoded image (PNG/JPG)")
    prompt: str = Field("", description="Optional text prompt for detection/grounding")


def _decode_image(b64: str) -> Image.Image:
    data = base64.b64decode(b64)
    return Image.open(io.BytesIO(data)).convert("RGB")


def _run_task(image: Image.Image, task: str, text_input: str = "") -> str:
    import torch
    _load()
    prompt = task if not text_input else f"{task}{text_input}"
    inputs = _processor(text=prompt, images=image, return_tensors="pt")
    with torch.no_grad():
        generated = _model.generate(
            input_ids=inputs["input_ids"],
            pixel_values=inputs["pixel_values"],
            max_new_tokens=1024,
            num_beams=3,
        )
    result = _processor.batch_decode(generated, skip_special_tokens=False)[0]
    parsed = _processor.post_process_generation(result, task=task, image_size=image.size)
    return parsed


@app.get("/health")
async def health():
    return {"status": "ok", "service": "florence-2", "loaded": _loaded}


@app.post("/describe")
async def describe(req: ImageRequest):
    """Detailed description of the image."""
    try:
        image = _decode_image(req.image_b64)
        result = _run_task(image, "<MORE_DETAILED_CAPTION>")
        return {"description": result.get("<MORE_DETAILED_CAPTION>", str(result))}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/caption")
async def caption(req: ImageRequest):
    """Short caption for the image."""
    try:
        image = _decode_image(req.image_b64)
        result = _run_task(image, "<CAPTION>")
        return {"caption": result.get("<CAPTION>", str(result))}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/detect")
async def detect(req: ImageRequest):
    """Detect objects matching the text prompt."""
    try:
        image = _decode_image(req.image_b64)
        task = "<CAPTION_TO_PHRASE_GROUNDING>"
        result = _run_task(image, task, req.prompt)
        return {"detections": result.get(task, str(result))}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
