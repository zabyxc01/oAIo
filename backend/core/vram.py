"""
VRAM monitoring via sysfs (amdgpu driver).
Reads directly from /sys/class/drm/cardN/device/ — no rocm-smi dependency.
"""
from pathlib import Path

_SYS_DRM = Path("/sys/class/drm")


def _amd_dev() -> Path | None:
    """Return the first AMD GPU device sysfs path that exposes VRAM info."""
    for card in sorted(_SYS_DRM.glob("card?")):
        dev = card / "device"
        if (dev / "mem_info_vram_total").exists():
            return dev
    return None


def get_vram_usage() -> dict:
    dev = _amd_dev()
    if not dev:
        return {"error": "No AMD GPU found in /sys/class/drm"}
    try:
        total = int((dev / "mem_info_vram_total").read_text().strip())
        used  = int((dev / "mem_info_vram_used").read_text().strip())
        return {
            "used_bytes":  used,
            "total_bytes": total,
            "used_gb":     round(used  / 1e9, 2),
            "total_gb":    round(total / 1e9, 2),
            "percent":     round((used / total) * 100, 1) if total else 0,
        }
    except Exception as e:
        return {"error": str(e)}


def get_gpu_utilization() -> dict:
    dev = _amd_dev()
    if not dev:
        return {"gpu_use_percent": 0}
    try:
        pct = int((dev / "gpu_busy_percent").read_text().strip())
        return {"gpu_use_percent": pct}
    except Exception as e:
        return {"error": str(e)}
