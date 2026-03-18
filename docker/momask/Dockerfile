FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential curl wget unzip \
    && rm -rf /var/lib/apt/lists/*

# PyTorch ROCm 6.2 (matches indextts pattern)
RUN pip install --no-cache-dir \
    torch torchvision \
    --index-url https://download.pytorch.org/whl/rocm6.2

# Clone MoMask
RUN git clone https://github.com/EricGuo5513/momask-codes /app/momask
WORKDIR /app/momask

# Install MoMask dependencies — relax version pins for ROCm compatibility
RUN sed -i 's/^--extra-index-url.*//g' requirements.txt && \
    sed -i 's/torch==1\.12\.0/torch/g' requirements.txt && \
    sed -i 's/torchaudio//g' requirements.txt && \
    sed -i 's/torchvision//g' requirements.txt && \
    sed -i 's/numpy==1\.21\.5/numpy/g' requirements.txt && \
    sed -i 's/matplotlib==3\.1\.3/matplotlib/g' requirements.txt && \
    sed -i 's/Pillow==9\.2\.0/Pillow/g' requirements.txt && \
    sed -i 's/grpcio==1\.54\.2/grpcio/g' requirements.txt && \
    sed -i 's/PyYAML==6\.0/PyYAML/g' requirements.txt && \
    sed -i 's/importlib-metadata==5\.0\.0/importlib-metadata/g' requirements.txt && \
    sed -i 's/importlib-resources==5\.12\.0/importlib-resources/g' requirements.txt && \
    pip install --no-cache-dir -r requirements.txt

# FastAPI server deps
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn[standard] \
    gdown

# Download pretrained models (HumanML3D only — KIT not needed for inference)
RUN mkdir -p checkpoints/t2m && \
    gdown --fuzzy "https://drive.google.com/file/d/1vXS7SHJBgWPt59wupQ5UUzhFObrnGkQ0/view?usp=sharing" -O /tmp/humanml3d_models.zip && \
    unzip /tmp/humanml3d_models.zip -d checkpoints/t2m/ && \
    rm /tmp/humanml3d_models.zip

# Download GloVe (needed for text encoding)
RUN bash prepare/download_glove.sh || true

COPY docker/momask/server.py /app/server.py
COPY docker/momask/bvh_to_vrma.py /app/bvh_to_vrma.py

ENV HSA_OVERRIDE_GFX_VERSION=11.0.0
ENV PYTORCH_ROCM_ARCH=gfx1100
ENV MOMASK_DIR=/app/momask
ENV MOMASK_MODELS=/models

EXPOSE 8005

HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8005/health || exit 1

WORKDIR /app
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8005"]
