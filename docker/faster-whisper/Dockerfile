FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libsndfile1 curl \
    && rm -rf /var/lib/apt/lists/*

# CTranslate2 (faster-whisper backend) doesn't support ROCm natively.
# CPU inference with medium model is fast enough on Ryzen 9 3900X (24T).
RUN pip install --no-cache-dir \
    faster-whisper \
    fastapi \
    uvicorn[standard] \
    python-multipart \
    gradio

WORKDIR /app
COPY docker/faster-whisper/app.py .

ENV WHISPER_MODEL=medium
ENV WHISPER_DEVICE=cpu
ENV HF_HOME=/hf-cache

EXPOSE 8003 7880

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8003/status || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8003"]
