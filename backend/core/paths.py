"""
Symlink management and storage I/O stats for /mnt/oaio/* paths.
"""
import os
import time
from pathlib import Path

SYMLINK_ROOT = Path(os.environ.get("OAIO_SYMLINK_ROOT", "/mnt/oaio"))

_ALLOWED_TARGETS = ["/mnt/storage", "/mnt/windows-sata", "/dev/shm", "/home/oao", "/tmp"]

_TIER_MAP = {
    "/mnt/storage":      "nvme",
    "/mnt/windows-sata": "sata",
    "/dev/shm":          "ram",
}

def _infer_tier(target: str) -> str:
    for prefix, tier in _TIER_MAP.items():
        if target.startswith(prefix):
            return tier
    return "custom"


def heal_dangling(paths_cfg: dict) -> list[str]:
    """Create missing target directories for all symlinks. Returns list of healed paths."""
    healed = []
    for name, cfg in paths_cfg.items():
        link = Path(cfg["link"])
        try:
            target = Path(os.readlink(link))
        except OSError:
            continue
        if not target.exists() and not target.suffix:
            try:
                target.mkdir(parents=True, exist_ok=True)
                healed.append(str(target))
            except Exception:
                pass
    return healed


def get_all_paths(paths_cfg: dict) -> list[dict]:
    """Return list of path entries with current symlink target and inferred tier."""
    result = []
    for name, cfg in paths_cfg.items():
        link = Path(cfg["link"])
        try:
            target = str(os.readlink(link))
        except OSError:
            target = None
        exists = target is not None and Path(target).exists()
        result.append({
            "name":            name,
            "label":           cfg["label"],
            "link":            str(link),
            "target":          target,
            "default_target":  cfg["default_target"],
            "tier":            _infer_tier(target) if target else "missing",
            "exists":          exists,
            "containers":      cfg.get("containers", []),
        })
    return result


def repoint(link_path: str, new_target: str) -> dict:
    """
    Atomically repoint a symlink:
      1. Ensure target directory exists (prevents dangling symlinks that break Docker mounts).
      2. Create a temp symlink next to the original.
      3. os.rename() (atomic on Linux) replaces the original.
    """
    link = Path(link_path)
    target = Path(new_target)

    # Validate link_path is under SYMLINK_ROOT
    try:
        link.resolve().relative_to(SYMLINK_ROOT.resolve())
    except ValueError:
        return {"ok": False, "error": f"link_path must be under {SYMLINK_ROOT}"}

    # Validate new_target is absolute and contains no '..' after resolution
    if not target.is_absolute():
        return {"ok": False, "error": "new_target must be an absolute path"}
    if ".." in Path(new_target).parts:
        return {"ok": False, "error": "new_target must not contain '..' components"}
    # Validate target is under an allowed prefix
    resolved_target = str(target.resolve())
    if not any(resolved_target.startswith(prefix) for prefix in _ALLOWED_TARGETS):
        return {"ok": False, "error": f"new_target must be under one of: {', '.join(_ALLOWED_TARGETS)}"}
    # Auto-create target dir if it looks like a directory path (no file extension)
    if not target.exists() and not target.suffix:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    tmp  = link.parent / (link.name + ".tmp")
    try:
        if tmp.exists() or tmp.is_symlink():
            tmp.unlink()
        os.symlink(new_target, tmp)
        os.rename(tmp, link)
        return {"ok": True, "link": str(link), "target": new_target}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── /proc/diskstats reader ────────────────────────────────────────────────────

_DISKSTATS_DEVS = {"nvme0n1": "nvme", "sda": "sata"}
_prev_stats: dict = {}
_prev_time: float = 0.0

def _read_diskstats() -> dict[str, dict]:
    """Parse /proc/diskstats → {devname: {reads_kb, writes_kb}}."""
    stats = {}
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                fields = line.split()
                if len(fields) < 14:
                    continue
                dev = fields[2]
                if dev in _DISKSTATS_DEVS:
                    # sectors read/written (sector = 512 bytes)
                    stats[dev] = {
                        "sectors_read":    int(fields[5]),
                        "sectors_written": int(fields[9]),
                    }
    except Exception:
        pass
    return stats


def get_storage_stats() -> dict:
    """
    Returns MB/s read and write for nvme and sata by diffing /proc/diskstats.
    Returns zeros on first call (no prior sample).
    """
    global _prev_stats, _prev_time

    now  = time.monotonic()
    curr = _read_diskstats()
    dt   = now - _prev_time if _prev_time else 0.0

    result = {
        "nvme": {"read_mbs": 0.0, "write_mbs": 0.0},
        "sata": {"read_mbs": 0.0, "write_mbs": 0.0},
    }

    if dt > 0 and _prev_stats:
        for dev, tier in _DISKSTATS_DEVS.items():
            if dev in curr and dev in _prev_stats:
                dr = curr[dev]["sectors_read"]    - _prev_stats[dev]["sectors_read"]
                dw = curr[dev]["sectors_written"] - _prev_stats[dev]["sectors_written"]
                # 512 bytes/sector → MB/s
                result[tier]["read_mbs"]  = round(max(dr, 0) * 512 / 1e6 / dt, 2)
                result[tier]["write_mbs"] = round(max(dw, 0) * 512 / 1e6 / dt, 2)

    _prev_stats = curr
    _prev_time  = now
    return result
