FROM python:3.10-slim

WORKDIR /app

# System deps — espeak-ng required by styletts2 phonemizer
RUN apt-get update && apt-get install -y --no-install-recommends \
    espeak-ng \
    libsndfile1 \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# PyTorch with ROCm 6.2
RUN pip install --no-cache-dir \
    torch torchaudio \
    --index-url https://download.pytorch.org/whl/rocm6.2

# StyleTTS2 + Gradio
RUN pip install --no-cache-dir \
    styletts2 \
    "gradio==4.19.2" \
    numpy \
    scipy

# NLTK data required by styletts2 tokenizer
RUN python3 -c "import nltk; nltk.download('punkt'); nltk.download('punkt_tab')"

COPY docker/styletts2/app.py .

# ROCm env
ENV HSA_OVERRIDE_GFX_VERSION=11.0.0
ENV PYTORCH_ROCM_ARCH=gfx1100
ENV HF_HOME=/root/.cache/huggingface
ENV GRADIO_SERVER_NAME=0.0.0.0

EXPOSE 7870

CMD ["python3", "app.py"]
