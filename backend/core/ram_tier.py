"""
RAM tier — pinned host memory pool management.

When a symlink under /mnt/oaio/ points at /dev/shm/oaio-<name>, the RAM
tier is active for that path. This module auto-detects a safe ceiling based
on total system RAM, manages /dev/shm directories, tracks usage across all
active pools, and emits alerts in the same format as VRAM alerts.

Safety: on startup, recover_dangling() checks every path in paths.json.
If a symlink points to a dead /dev/shm path (wiped by reboot/crash), it
reverts to default_target automatically. No data loss, no manual fix.

Ceiling formula:
  total_ram - max(8 GB, total_ram * 0.25)
  e.g. 62 GB machine → ~46 GB ceiling
       16 GB machine →  ~8 GB ceiling
      256 GB machine → ~192 GB ceiling

SAM (Resizable BAR) note:
  When AMD SAM is enabled, the GPU can DMA from CPU-pinned RAM across the
  full BAR window. The RAM tier becomes a significantly faster staging area
  for model weights — load from /dev/shm → GPU avoids PCIe bouncing through
  the 256 MB legacy BAR aperture.
"""
import json
import os
import shutil
from pathlib import Path

_SHM_ROOT   = Path("/dev/shm")
_SHM_PREFIX = "oaio-"
_CONFIG_DIR = Path(__file__).parent.parent.parent / "config"
_PATHS_FILE = _CONFIG_DIR / "paths.json"

WARN_THRESHOLD = 0.85
HARD_THRESHOLD = 0.95


def detect_ceiling_gb() -> float:
    """
    Auto-detect safe RAM tier ceiling for this machine.
    Reserves max(8 GB, 25% of total) for OS + containers.
    """
    total_bytes = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    total_gb    = total_bytes / (1024 ** 3)
    reserved    = max(8.0, total_gb * 0.25)
    return round(total_gb - reserved, 1)


def _shm_path(name: str) -> Path:
    return _SHM_ROOT / f"{_SHM_PREFIX}{name}"


def activate(name: str) -> dict:
    """
    Create /dev/shm/oaio-<name> and return the path to use as symlink target.
    Idempotent — safe to call if already active.
    """
    p = _shm_path(name)
    p.mkdir(parents=True, exist_ok=True)
    return {
        "ok":          True,
        "path":        str(p),
        "ceiling_gb":  detect_ceiling_gb(),
    }


def deactivate(name: str, move_to: str | None = None) -> dict:
    """
    Remove /dev/shm/oaio-<name>.
    If move_to is provided, moves contents there first (e.g. back to NVMe).
    """
    p = _shm_path(name)
    if not p.exists():
        return {"ok": True, "note": "already inactive"}

    moved = 0
    if move_to:
        dest = Path(move_to)
        dest.mkdir(parents=True, exist_ok=True)
        for item in p.iterdir():
            shutil.move(str(item), str(dest / item.name))
            moved += 1

    shutil.rmtree(p, ignore_errors=True)
    return {"ok": True, "moved_files": moved}


def deactivate_all() -> list[dict]:
    """
    Deactivate ALL active RAM tier pools, reverting symlinks to default_target.
    Called on clean shutdown.
    """
    results = []
    try:
        cfg = json.loads(_PATHS_FILE.read_text())
    except Exception:
        return results

    for name, entry in cfg.items():
        link = Path(entry.get("link", ""))
        default = entry.get("default_target", "")
        if not link.is_symlink() or not default:
            continue
        try:
            target = os.readlink(str(link))
        except OSError:
            continue
        if target.startswith(f"{_SHM_ROOT}/{_SHM_PREFIX}"):
            result = _revert_to_default(name, link, default)
            results.append(result)
    return results


def recover_dangling() -> list[dict]:
    """
    Startup safety — find symlinks pointing to dead /dev/shm paths and
    revert them to their default_target from paths.json.

    Call this once at startup. Handles crash, reboot, force-kill — any
    scenario where /dev/shm was wiped without a clean deactivate.
    """
    recovered = []
    try:
        cfg = json.loads(_PATHS_FILE.read_text())
    except Exception as e:
        print(f"[ram_tier] recover: cannot read paths.json: {e}")
        return recovered

    for name, entry in cfg.items():
        link = Path(entry.get("link", ""))
        default = entry.get("default_target", "")
        if not default:
            continue
        if not link.is_symlink():
            continue
        try:
            target = os.readlink(str(link))
        except OSError:
            continue
        # Only recover links that point to /dev/shm/oaio-* but the dir is gone
        if target.startswith(f"{_SHM_ROOT}/{_SHM_PREFIX}") and not Path(target).exists():
            result = _revert_to_default(name, link, default)
            recovered.append(result)

    return recovered


def _revert_to_default(name: str, link: Path, default: str) -> dict:
    """Repoint a symlink from dead /dev/shm back to its default_target."""
    try:
        old_target = os.readlink(str(link))
        link.unlink()
        link.symlink_to(default)
        print(f"[ram_tier] recovered '{name}': {old_target} → {default}")
        return {"name": name, "old": old_target, "new": default, "ok": True}
    except Exception as e:
        print(f"[ram_tier] recover '{name}' FAILED: {e}")
        return {"name": name, "error": str(e), "ok": False}


def get_usage() -> dict:
    """
    Scan all active oaio-* pools in /dev/shm.
    Returns used_gb, ceiling_gb, free_gb, percent, and per-pool breakdown.
    Returns empty dict if RAM tier is not active (no pools).
    """
    pools: dict[str, float] = {}
    total_bytes = 0

    if _SHM_ROOT.exists():
        for d in _SHM_ROOT.iterdir():
            if d.is_dir() and d.name.startswith(_SHM_PREFIX):
                name       = d.name[len(_SHM_PREFIX):]
                size_bytes = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                pools[name] = round(size_bytes / (1024 ** 3), 2)
                total_bytes += size_bytes

    if not pools:
        return {}

    ceiling_gb = detect_ceiling_gb()
    used_gb    = round(total_bytes / (1024 ** 3), 2)
    return {
        "used_gb":    used_gb,
        "ceiling_gb": ceiling_gb,
        "free_gb":    round(max(ceiling_gb - used_gb, 0), 2),
        "percent":    round(used_gb / ceiling_gb * 100, 1) if ceiling_gb > 0 else 0,
        "pools":      pools,
    }


def check_alerts() -> list[dict]:
    """RAM tier alerts — mirrors the VRAM alert pattern in resources.py."""
    usage = get_usage()
    if not usage:
        return []  # RAM tier not active

    alerts = []
    pct = usage["percent"] / 100

    if pct >= HARD_THRESHOLD:
        alerts.append({
            "level":   "critical",
            "type":    "ram_tier_hard",
            "message": (
                f"RAM tier {usage['used_gb']}/{usage['ceiling_gb']} GB "
                f"({usage['percent']:.0f}%) — ceiling exceeded"
            ),
        })
    elif pct >= WARN_THRESHOLD:
        alerts.append({
            "level":   "warning",
            "type":    "ram_tier_warn",
            "message": (
                f"RAM tier {usage['used_gb']}/{usage['ceiling_gb']} GB "
                f"({usage['percent']:.0f}%) — approaching ceiling"
            ),
        })

    return alerts
