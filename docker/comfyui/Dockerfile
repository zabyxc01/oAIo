FROM python:3.10-slim

WORKDIR /ComfyUI

# System deps
# libgl1 + libglib2.0 = OpenCV
# libsm6 + libxext6  = OpenCV headless fallback
# libgomp1           = numpy/scipy BLAS
# build-essential    = some pip packages need gcc
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libgomp1 \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# PyTorch with ROCm 6.2 — same as f5-tts/rvc
RUN pip install --no-cache-dir \
    torch torchvision torchaudio \
    --index-url https://download.pytorch.org/whl/rocm6.2

# ComfyUI at v0.7.0
RUN git clone https://github.com/comfyanonymous/ComfyUI . && \
    git checkout f59f71cf

# ComfyUI core requirements (installs newer transformers/diffusers — these win)
RUN pip install --no-cache-dir -r requirements.txt

# Custom node dependencies — unique packages only, no version overrides
# This avoids downgrading transformers/diffusers that ComfyUI needs
RUN pip install --no-cache-dir \
    # MVAdapter / general 3D
    peft \
    trimesh \
    omegaconf \
    scikit-image \
    scikit-learn \
    # ControlNet aux
    addict \
    yacs \
    albumentations \
    mediapipe \
    matplotlib \
    python-dateutil \
    # Segment Anything / RMBG / grounding
    segment-anything \
    transparent-background \
    onnxruntime \
    protobuf \
    hydra-core \
    iopath \
    decord \
    ftfy \
    # KJNodes
    color-matcher \
    mss \
    # ComfyUI Manager
    GitPython \
    PyGithub \
    typer \
    rich \
    toml \
    uv \
    chardet \
    # General extras
    opencv-contrib-python-headless \
    kornia \
    einops \
    timm \
    accelerate

# fvcore (Facebook — needs special index)
RUN pip install --no-cache-dir fvcore --extra-index-url https://dl.fbaipublicfiles.com/fvcore/ || \
    pip install --no-cache-dir fvcore

# groundingdino — sometimes has build issues, non-fatal if it fails
RUN pip install --no-cache-dir groundingdino-py || echo "groundingdino-py install failed — skipping"

# Create mount points
RUN mkdir -p \
    /ComfyUI/models \
    /ComfyUI/custom_nodes \
    /ComfyUI/output \
    /ComfyUI/user \
    /ComfyUI/input

# ROCm env for GPU detection
ENV HSA_OVERRIDE_GFX_VERSION=11.0.0
ENV PYTORCH_ROCM_ARCH=gfx1100
ENV HF_HOME=/hf-cache

EXPOSE 8188

# --listen = accept connections from all interfaces (not just localhost)
CMD ["python3", "main.py", "--listen", "0.0.0.0", "--port", "8188"]
