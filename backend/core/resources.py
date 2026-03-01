"""
Resource management — VRAM budgeting and OOM prevention.

Two layers:
  1. Pre-activation check: projected VRAM if a mode were activated
  2. Live alerts: current VRAM vs warn/hard thresholds
"""
from .vram import get_vram_usage

VRAM_TOTAL_GB    = 20.0
WARN_THRESHOLD   = 0.85   # 17.0 GB
HARD_THRESHOLD   = 0.95   # 19.0 GB


def projected_vram(mode: dict, services_cfg: dict) -> dict:
    """
    Project total VRAM if this mode were activated.

    Uses per-mode allocations if defined, falls back to service-level
    vram_est_gb. Returns a dict with:
      projected_gb  — total estimated VRAM for all active services
      budget_gb     — mode ceiling (vram_budget_gb or projected_gb if unset)
      fits          — True if projected < hard threshold
      warning       — True if projected > warn threshold
      blocked       — True if projected >= hard threshold (do not activate)
      details       — {service: alloc_gb} breakdown
    """
    active = set(mode.get("services", []))
    alloc_overrides = mode.get("allocations", {})

    projected = 0.0
    details = {}
    for svc_name, svc in services_cfg.items():
        if svc_name not in active:
            continue
        alloc = alloc_overrides.get(svc_name, svc.get("vram_est_gb", 0))
        details[svc_name] = round(alloc, 1)
        projected += alloc

    projected = round(projected, 1)
    budget    = mode.get("vram_budget_gb", projected)

    warn    = projected > VRAM_TOTAL_GB * WARN_THRESHOLD
    blocked = projected >= VRAM_TOTAL_GB * HARD_THRESHOLD

    return {
        "projected_gb": projected,
        "budget_gb":    round(budget, 1),
        "total_gb":     VRAM_TOTAL_GB,
        "fits":         not blocked,
        "warning":      warn and not blocked,
        "blocked":      blocked,
        "details":      details,
    }


def check_alerts() -> list[dict]:
    """
    Live VRAM alerts. Returns list of active alerts (empty = all clear).
    """
    alerts = []
    vram = get_vram_usage()
    if "error" in vram or vram.get("total_gb", 0) == 0:
        return alerts

    pct  = vram.get("percent", 0) / 100
    used = vram.get("used_gb", 0)
    total = vram.get("total_gb", VRAM_TOTAL_GB)

    if pct >= HARD_THRESHOLD:
        alerts.append({
            "level":   "critical",
            "type":    "vram_hard",
            "message": f"VRAM {used:.1f}/{total}GB ({pct*100:.0f}%) — hard limit exceeded",
        })
    elif pct >= WARN_THRESHOLD:
        alerts.append({
            "level":   "warning",
            "type":    "vram_warn",
            "message": f"VRAM {used:.1f}/{total}GB ({pct*100:.0f}%) — approaching limit",
        })

    return alerts
