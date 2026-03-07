"""
Resource management — VRAM budgeting and OOM prevention.

Two layers:
  1. Pre-activation check: projected VRAM if a mode were activated
  2. Live alerts: current VRAM vs warn/hard thresholds

System-aware: projection checks against actual headroom (total - currently used),
so external GPU consumers (games, scripts) are factored in before activation.
"""
from .vram import get_vram_usage
from . import ram_tier

WARN_THRESHOLD   = 0.85
HARD_THRESHOLD   = 0.95
_FALLBACK_TOTAL  = 20.0  # used only if sysfs is unavailable


def _vram_total() -> float:
    """Read true VRAM total from sysfs. Falls back to _FALLBACK_TOTAL."""
    v = get_vram_usage()
    return v.get("total_gb", _FALLBACK_TOTAL) or _FALLBACK_TOTAL


def get_effective_vram_total() -> float:
    """Return virtual ceiling if set, else true VRAM total."""
    from . import enforcer
    if enforcer.vram_virtual_ceiling_gb is not None and enforcer.vram_virtual_ceiling_gb > 0:
        return enforcer.vram_virtual_ceiling_gb
    return _vram_total()


def get_system_accounting(services_cfg: dict) -> dict:
    """
    Break down VRAM and RAM into attributed (running containers) vs external (gaming/other).

      vram_total      — GPU total from sysfs
      vram_used       — total GPU used (all processes, from sysfs)
      vram_attributed — Σ vram_est_gb for containers currently running
      vram_external   — vram_used - vram_attributed  (gaming / other processes)
      vram_headroom   — vram_total - vram_used        (actually free right now)

      ram_* — same breakdown using psutil + ram_est_gb
    """
    import psutil
    from .docker_control import get_status

    vram = get_vram_usage()
    vram_total = get_effective_vram_total()
    vram_used  = vram.get("used_gb", 0.0)

    ram = psutil.virtual_memory()
    ram_total = round(ram.total / 1e9, 2)
    ram_used  = round(ram.used  / 1e9, 2)

    vram_attr = 0.0
    ram_attr  = 0.0
    for svc_name, svc in services_cfg.items():
        ctr = svc.get("container")
        if not ctr:
            continue
        try:
            s = get_status(ctr)
            if s.get("status") == "running":
                vram_attr += svc.get("vram_est_gb", 0.0)
                ram_attr  += svc.get("ram_est_gb",  0.0)
        except Exception:
            pass

    vram_attr = round(vram_attr, 2)
    ram_attr  = round(ram_attr,  2)

    return {
        "vram_total":      round(vram_total, 2),
        "vram_used":       round(vram_used,  2),
        "vram_attributed": vram_attr,
        "vram_external":   round(max(vram_used - vram_attr, 0.0), 2),
        "vram_headroom":   round(max(vram_total - vram_used, 0.0), 2),
        "ram_total":       ram_total,
        "ram_used":        ram_used,
        "ram_attributed":  ram_attr,
        "ram_external":    round(max(ram_used - ram_attr, 0.0), 2),
        "ram_headroom":    round(max(ram_total - ram_used, 0.0), 2),
    }


def projected_vram(mode: dict, services_cfg: dict) -> dict:
    """
    Project total VRAM if this mode were activated.

    Checks against actual headroom (vram_total - vram_currently_used) so external
    GPU load (gaming etc.) is factored in. Uses per-mode allocations if defined,
    falls back to service-level vram_est_gb. Returns a dict with:
      projected_gb  — total estimated VRAM for all active services
      budget_gb     — mode ceiling (vram_budget_gb or projected_gb if unset)
      headroom_gb   — actual free VRAM right now
      external_gb   — VRAM used by non-container processes
      fits          — True if projected <= headroom
      warning       — True if projected would push usage > warn threshold
      blocked       — True if projected would exceed headroom (do not activate)
      details       — {service: alloc_gb} breakdown
    """
    acct = get_system_accounting(services_cfg)
    vram_total    = get_effective_vram_total()
    vram_headroom = round(max(vram_total - acct["vram_used"], 0.0), 2)

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

    # Project what total usage would be after activation
    projected_total = round(acct["vram_used"] + projected - acct["vram_attributed"], 1)
    warn    = projected_total > vram_total * WARN_THRESHOLD
    blocked = projected > vram_headroom

    return {
        "projected_gb":  projected,
        "budget_gb":     round(budget, 1),
        "total_gb":      vram_total,
        "headroom_gb":   round(vram_headroom, 2),
        "external_gb":   acct["vram_external"],
        "fits":          not blocked,
        "warning":       warn and not blocked,
        "blocked":       blocked,
        "details":       details,
    }


def check_alerts() -> list[dict]:
    """
    Live VRAM alerts. Returns list of active alerts (empty = all clear).
    """
    alerts = []
    vram = get_vram_usage()
    if "error" in vram or vram.get("total_gb", 0) == 0:
        return alerts

    pct   = vram.get("percent", 0) / 100
    used  = vram.get("used_gb", 0)
    total = vram.get("total_gb", _FALLBACK_TOTAL)

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

    alerts.extend(ram_tier.check_alerts())
    return alerts
