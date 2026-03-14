FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ffmpeg libsndfile1 build-essential curl espeak-ng \
    && rm -rf /var/lib/apt/lists/*

# PyTorch ROCm 6.2
RUN pip install --no-cache-dir \
    torch torchaudio \
    --index-url https://download.pytorch.org/whl/rocm6.2

# Clone IndexTTS-2
RUN git clone https://github.com/index-tts/index-tts /app/index-tts
WORKDIR /app/index-tts

# Install IndexTTS deps — relax torch pin since ROCm has its own version
RUN sed -i 's/"torch==2\.8\.\*"/"torch"/g' pyproject.toml && \
    sed -i 's/"torchaudio==2\.8\.\*"/"torchaudio"/g' pyproject.toml && \
    pip install --no-cache-dir -e .

# FastAPI wrapper + Gradio UI deps
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn[standard] \
    python-multipart \
    huggingface_hub \
    gradio

COPY docker/indextts/app.py /app/app.py

ENV HSA_OVERRIDE_GFX_VERSION=11.0.0
ENV PYTORCH_ROCM_ARCH=gfx1100
ENV HF_HOME=/hf-cache
ENV INDEXTTS_MODEL_DIR=/models

EXPOSE 8004 7890

HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8004/status || exit 1

WORKDIR /app
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8004"]
