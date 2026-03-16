"""
Real-time per-container VRAM tracking via /proc fdinfo.

Reads drm-memory-vram from /proc/[pid]/fdinfo/* for each GPU-using process,
then maps PIDs to Docker containers to get per-service VRAM usage.

Requires /proc mounted as /host-proc inside the oAIo container.
Falls back gracefully if unavailable.
"""
import os
import logging
from pathlib import Path

log = logging.getLogger("vram_realtime")

HOST_PROC = Path(os.environ.get("HOST_PROC", "/host-proc"))


def _get_pid_vram_kib(pid: int) -> int:
    """Read total VRAM usage for a single PID from fdinfo. Returns KiB."""
    fdinfo_dir = HOST_PROC / str(pid) / "fdinfo"
    if not fdinfo_dir.exists():
        return 0

    total_kib = 0
    try:
        for fd in os.listdir(fdinfo_dir):
            try:
                content = (fdinfo_dir / fd).read_text()
                if "drm-memory-vram" not in content:
                    continue
                for line in content.split("\n"):
                    if line.startswith("drm-memory-vram:"):
                        # Format: "drm-memory-vram:\t123456 KiB"
                        val = line.split(":")[1].strip().split()[0]
                        total_kib += int(val)
            except (PermissionError, OSError, ValueError):
                continue
    except (PermissionError, OSError):
        return 0

    return total_kib


def _get_container_pids(docker_client, container_name: str) -> list[int]:
    """Get all PIDs belonging to a container (main + children)."""
    try:
        ctr = docker_client.containers.get(container_name)
        if ctr.status != "running":
            return []
        # top() returns all processes in the container
        top = ctr.top()
        pid_idx = top["Titles"].index("PID")
        return [int(row[pid_idx]) for row in top["Processes"]]
    except Exception:
        return []


def get_per_container_vram(docker_client, container_names: list[str]) -> dict:
    """
    Returns per-container VRAM usage in GB.

    {
        "ollama": 7.31,
        "indextts": 6.82,
        "comfyui": 0,
        ...
        "_untracked_gb": 0.05,  # VRAM used by processes not in our containers
        "_available": True
    }
    """
    if not HOST_PROC.exists() or not (HOST_PROC / "1").exists():
        return {"_available": False, "_error": "host /proc not mounted at /host-proc"}

    result = {}
    tracked_total_kib = 0

    for name in container_names:
        pids = _get_container_pids(docker_client, name)
        container_kib = 0
        for pid in pids:
            container_kib += _get_pid_vram_kib(pid)
        result[name] = round(container_kib / 1024 / 1024, 2)  # KiB → GB
        tracked_total_kib += container_kib

    # Calculate untracked VRAM (other GPU consumers like games, desktop, etc.)
    from .vram import get_vram_usage
    vram = get_vram_usage()
    if "used_bytes" in vram:
        total_used_kib = vram["used_bytes"] / 1024
        untracked_kib = max(0, total_used_kib - tracked_total_kib)
        result["_untracked_gb"] = round(untracked_kib / 1024 / 1024, 2)

    result["_available"] = True
    return result
