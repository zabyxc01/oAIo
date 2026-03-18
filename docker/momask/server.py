"""MoMask text-to-motion FastAPI server for oAIo."""

import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from bvh_to_vrma import convert_bvh_to_vrma

# Add momask to path
MOMASK_DIR = os.environ.get("MOMASK_DIR", "/app/momask")
sys.path.insert(0, MOMASK_DIR)

app = FastAPI(title="MoMask", description="Text-to-motion generation")

# ---------------------------------------------------------------------------
# Globals — lazy-loaded on first request
# ---------------------------------------------------------------------------
_models = {}
_loaded = False
_device = None
_mean = None
_std = None

FPS = 20
MAX_MOTION_LENGTH = 196  # frames


class GenerateRequest(BaseModel):
    prompt: str = Field(default=None, description="Text description of the motion")
    text: str = Field(default=None, description="Alias for prompt")
    duration: float = Field(4.0, ge=0.5, le=9.8, description="Duration in seconds")
    length: int = Field(default=None, description="Duration in frames (overrides duration)")
    repeat: int = Field(1, ge=1, le=4, description="Number of samples to generate")
    foot_ik: bool = Field(True, description="Apply foot inverse kinematics")

    def get_prompt(self) -> str:
        return self.prompt or self.text or ""

    def get_duration(self) -> float:
        if self.length and self.length > 0:
            return self.length / FPS
        return self.duration


class ConvertRequest(BaseModel):
    bvh: str = Field(..., description="BVH file content as text")


# ── Animation cache ──────────────────────────────────────────────────────────
_cache: dict[str, bytes] = {}  # hash → GLB bytes
_CACHE_MAX = 50

def _cache_key(prompt: str, duration: float) -> str:
    return hashlib.md5(f"{prompt}:{duration}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------
def _load_models():
    global _models, _loaded, _device, _mean, _std

    if _loaded:
        return

    from options.eval_option import EvalT2MOptions
    from utils.get_opt import get_opt
    from models.vq.model import RVQVAE, LengthEstimator
    from models.mask_transformer.transformer import MaskTransformer, ResidualTransformer

    _device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Parse default options (we override key paths)
    parser = EvalT2MOptions()
    # Minimal argv to avoid parsing issues
    sys.argv = ["server", "--gpu_id", "0" if torch.cuda.is_available() else "-1"]
    opt = parser.parse()
    opt.device = _device

    # Load sub-model configs
    checkpoints = os.path.join(MOMASK_DIR, "checkpoints", "t2m")
    opt.checkpoints_dir = os.path.join(MOMASK_DIR, "checkpoints")

    # Dataset info for t2m
    dataset_opt_path = os.path.join(checkpoints, opt.vq_name, "opt.txt")
    vq_opt = get_opt(dataset_opt_path, _device)
    vq_opt.dim_pose = 263
    vq_opt.joints_num = 22

    # VQ model
    vq_model = RVQVAE(
        vq_opt,
        vq_opt.dim_pose,
        vq_opt.nb_code,
        vq_opt.code_dim,
        vq_opt.output_emb_width,
        vq_opt.down_t,
        vq_opt.stride_t,
        vq_opt.width,
        vq_opt.depth,
        vq_opt.dilation_growth_rate,
        vq_opt.vq_act,
        vq_opt.vq_norm,
    )
    vq_ckpt = os.path.join(checkpoints, opt.vq_name, "model", "net_best_fid.tar")
    ckpt = torch.load(vq_ckpt, map_location=_device)
    vq_model.load_state_dict(ckpt["vq_model"], strict=True)
    vq_model.to(_device).eval()

    # Masked transformer
    model_opt_path = os.path.join(checkpoints, opt.name, "opt.txt")
    model_opt = get_opt(model_opt_path, _device)
    model_opt.num_tokens = vq_opt.nb_code
    model_opt.num_quantizers = vq_opt.num_quantizers
    model_opt.code_dim = vq_opt.code_dim

    t2m_transformer = MaskTransformer(
        code_dim=model_opt.code_dim,
        cond_mode="text",
        latent_dim=model_opt.latent_dim,
        ff_size=model_opt.ff_size,
        num_layers=model_opt.num_layers,
        num_heads=model_opt.n_head,
        dropout=model_opt.dropout,
        clip_dim=512,
        cond_drop_prob=model_opt.cond_drop_prob,
        clip_version="ViT-B/32",
        opt=model_opt,
    )
    trans_ckpt = os.path.join(checkpoints, opt.name, "model", "net_best_fid.tar")
    ckpt = torch.load(trans_ckpt, map_location=_device)
    missing, unexpected = t2m_transformer.load_state_dict(ckpt["trans"], strict=False)
    t2m_transformer.to(_device).eval()

    # Residual transformer
    res_opt_path = os.path.join(checkpoints, opt.res_name, "opt.txt")
    res_opt = get_opt(res_opt_path, _device)
    res_opt.num_tokens = vq_opt.nb_code
    res_opt.num_quantizers = vq_opt.num_quantizers
    res_opt.code_dim = vq_opt.code_dim

    res_model = ResidualTransformer(
        code_dim=res_opt.code_dim,
        cond_mode="text",
        latent_dim=res_opt.latent_dim,
        ff_size=res_opt.ff_size,
        num_layers=res_opt.num_layers,
        num_heads=res_opt.n_head,
        dropout=res_opt.dropout,
        clip_dim=512,
        cond_drop_prob=res_opt.cond_drop_prob,
        clip_version="ViT-B/32",
        opt=res_opt,
    )
    res_ckpt = os.path.join(checkpoints, opt.res_name, "model", "net_best_fid.tar")
    ckpt = torch.load(res_ckpt, map_location=_device)
    missing, unexpected = res_model.load_state_dict(ckpt["res_transformer"], strict=False)
    res_model.to(_device).eval()

    # Length estimator
    length_estimator = LengthEstimator(512, 50)
    le_ckpt = os.path.join(checkpoints, "length_estimator", "model", "finest.tar")
    ckpt = torch.load(le_ckpt, map_location=_device)
    length_estimator.load_state_dict(ckpt["estimator"])
    length_estimator.to(_device).eval()

    # Load mean/std for denormalization
    meta_dir = os.path.join(MOMASK_DIR, "checkpoints", "t2m", opt.vq_name, "meta")
    _mean = np.load(os.path.join(meta_dir, "mean.npy"))
    _std = np.load(os.path.join(meta_dir, "std.npy"))

    _models = {
        "vq": vq_model,
        "transformer": t2m_transformer,
        "residual": res_model,
        "length_estimator": length_estimator,
        "opt": opt,
        "model_opt": model_opt,
    }
    _loaded = True


# ---------------------------------------------------------------------------
# Generation logic
# ---------------------------------------------------------------------------
@torch.no_grad()
def _generate_motion(prompt: str, duration: float, foot_ik: bool = True):
    """Generate motion from text, return joints array (N, 22, 3)."""
    _load_models()

    from utils.motion_process import recover_from_ric
    from torch.distributions.categorical import Categorical

    vq_model = _models["vq"]
    t2m_transformer = _models["transformer"]
    res_model = _models["residual"]
    length_estimator = _models["length_estimator"]
    opt = _models["opt"]

    # Determine motion length in frames
    m_length = int(duration * FPS)
    m_length = min(m_length, MAX_MOTION_LENGTH)
    # Round to unit_length
    unit_length = 4
    m_length = (m_length // unit_length) * unit_length
    m_length = max(m_length, unit_length)

    m_token_length = m_length // unit_length

    # Generate tokens
    causal_tokens, _ = t2m_transformer.generate(
        [prompt],
        torch.LongTensor([m_token_length]).to(_device),
        timesteps=18,
        cond_scale=4.0,
        temperature=1.0,
        topk_filter_thres=0.9,
    )

    # Residual refinement
    codes = res_model.generate(
        causal_tokens,
        [prompt],
        torch.LongTensor([m_token_length]).to(_device),
        temperature=1.0,
        cond_scale=2.0,
    )

    # Decode to motion
    motion = vq_model.forward_decoder(codes)

    # Denormalize
    motion_np = motion[0].cpu().numpy()
    motion_np = motion_np * _std + _mean

    # Recover joint positions (N, 22, 3)
    motion_tensor = torch.from_numpy(motion_np).unsqueeze(0).float()
    joints = recover_from_ric(motion_tensor, 22)
    joints = joints[0].numpy()

    return joints[:m_length]


def _joints_to_bvh(joints: np.ndarray, output_path: str, foot_ik: bool = True):
    """Convert joints array to BVH file using MoMask's converter."""
    from visualization.joints2bvh import Joint2BVHConvertor

    converter = Joint2BVHConvertor()
    _, result_joints = converter.convert(
        joints, filename=output_path, iterations=100, foot_ik=foot_ik
    )
    return output_path


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "service": "momask", "loaded": _loaded}


@app.get("/models")
async def list_models():
    return {
        "models": [
            {
                "id": "humanml3d",
                "name": "HumanML3D",
                "description": "MoMask text-to-motion (HumanML3D dataset)",
                "fps": FPS,
                "max_frames": MAX_MOTION_LENGTH,
                "max_duration_s": MAX_MOTION_LENGTH / FPS,
                "joints": 22,
            }
        ]
    }


@app.post("/generate")
async def generate(req: GenerateRequest):
    prompt = req.get_prompt()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt or text is required")
    duration = req.get_duration()
    try:
        results = []
        for i in range(req.repeat):
            t0 = time.time()
            joints = _generate_motion(prompt, duration, req.foot_ik)
            gen_time = time.time() - t0

            # Write BVH to temp file
            tmp = tempfile.NamedTemporaryFile(
                suffix=".bvh", delete=False, dir="/tmp"
            )
            tmp.close()
            _joints_to_bvh(joints, tmp.name, foot_ik=req.foot_ik)

            with open(tmp.name, "r") as f:
                bvh_data = f.read()
            os.unlink(tmp.name)

            results.append(
                {
                    "index": i,
                    "frames": int(joints.shape[0]),
                    "duration_s": round(joints.shape[0] / FPS, 2),
                    "generation_time_s": round(gen_time, 2),
                    "bvh": bvh_data,
                }
            )

        return {
            "prompt": prompt,
            "results": results,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/generate/file")
async def generate_file(req: GenerateRequest):
    """Generate motion and return as downloadable BVH file (single sample)."""
    prompt = req.get_prompt()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt or text is required")
    try:
        joints = _generate_motion(prompt, req.get_duration(), req.foot_ik)

        tmp = tempfile.NamedTemporaryFile(suffix=".bvh", delete=False, dir="/tmp")
        tmp.close()
        _joints_to_bvh(joints, tmp.name, foot_ik=req.foot_ik)

        return FileResponse(
            tmp.name,
            media_type="application/octet-stream",
            filename="momask_motion.bvh",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/convert")
async def convert_bvh(req: ConvertRequest):
    """Convert BVH text to VRMA GLB binary."""
    try:
        glb = convert_bvh_to_vrma(req.bvh)
        return Response(content=glb, media_type="model/gltf-binary",
                        headers={"Content-Disposition": "attachment; filename=animation.glb"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/generate_and_convert")
async def generate_and_convert(req: GenerateRequest):
    """Text → BVH → VRMA in one shot. Returns GLB binary. Cached."""
    prompt = req.get_prompt()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt or text is required")
    duration = req.get_duration()

    key = _cache_key(prompt, duration)
    if key in _cache:
        return Response(content=_cache[key], media_type="model/gltf-binary",
                        headers={"Content-Disposition": "attachment; filename=animation.glb"})

    try:
        joints = _generate_motion(prompt, duration, req.foot_ik)

        tmp = tempfile.NamedTemporaryFile(suffix=".bvh", delete=False, dir="/tmp")
        tmp.close()
        _joints_to_bvh(joints, tmp.name, foot_ik=req.foot_ik)

        with open(tmp.name, "r") as f:
            bvh_text = f.read()
        os.unlink(tmp.name)

        glb = convert_bvh_to_vrma(bvh_text)

        # Cache result
        if len(_cache) >= _CACHE_MAX:
            oldest = next(iter(_cache))
            del _cache[oldest]
        _cache[key] = glb

        return Response(content=glb, media_type="model/gltf-binary",
                        headers={"Content-Disposition": "attachment; filename=animation.glb"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
