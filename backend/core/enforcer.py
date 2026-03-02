"""
Reactive OOM enforcement loop.

Runs as a background asyncio task inside the FastAPI process.
Polls VRAM every POLL_INTERVAL seconds.

Responsibilities:
  1. OOM kill   — stops lowest-priority hard-limit container when VRAM > 95%
  2. Recovery   — restarts enforcer-killed containers when VRAM drops < 85%
  3. Crash watch — detects unexpected container exits and restores them with backoff

Actions by service limit_mode:
  soft  — warn only, never killed by enforcer (but crash-watched and restored)
  hard  — stopped on OOM, restored when pressure drops
"""
import asyncio
import logging
import time
from collections import deque
from .vram import get_vram_usage
from .resources import HARD_THRESHOLD, WARN_THRESHOLD

log = logging.getLogger("enforcer")

POLL_INTERVAL  = 5   # seconds between enforcement cycles
RECOVERY_DELAY = 30  # seconds after kill before attempting restart
CRASH_DELAY    = 30  # seconds after crash before attempting restart

# Shared state — mutated by activate_mode / deactivate_mode in main.py
active_modes: set[str] = set()

# Kill/crash/restore log — last 50 events, exported via WS + /enforcement/status
kill_log: deque = deque(maxlen=50)

# Containers killed or crashed: {container_name: {svc_name, priority, killed_at, reason}}
_killed_services: dict = {}

# Last known container status for crash detection: {container_name: str}
_prev_status: dict = {}


async def enforcement_loop(get_services_fn, get_docker_fn):
    """
    get_services_fn() — returns current services config dict (re-read each cycle)
    get_docker_fn()   — returns docker client (from_env)
    """
    log.info("Enforcement loop started (poll every %ds)", POLL_INTERVAL)
    _last_kill = None  # cooldown — don't kill same container twice in a row

    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            vram = get_vram_usage()
            if "error" in vram or vram.get("total_gb", 0) == 0:
                continue

            pct      = vram.get("percent", 0) / 100
            services = get_services_fn()
            client   = get_docker_fn()

            # ── 1. Crash detection (runs regardless of active modes) ──────────
            for svc_name, svc in services.items():
                ctr = svc.get("container")
                if not ctr:
                    continue
                try:
                    status = client.containers.get(ctr).status
                except Exception:
                    status = "unknown"

                prev = _prev_status.get(ctr)
                if prev == "running" and status in ("exited", "dead") and ctr not in _killed_services:
                    log.warning("Crash detected: %s — scheduling restart in %ds", ctr, CRASH_DELAY)
                    _killed_services[ctr] = {
                        "svc_name":  svc_name,
                        "priority":  svc.get("priority", 3),
                        "killed_at": time.time(),
                        "reason":    "crash",
                    }
                    kill_log.appendleft({
                        "event":     "crash",
                        "service":   svc_name,
                        "container": ctr,
                        "vram_used": round(vram.get("used_gb", 0), 2),
                        "ts":        time.time(),
                    })

                _prev_status[ctr] = status

            # ── 2. Recovery — restart killed/crashed containers when safe ─────
            if _killed_services and pct < WARN_THRESHOLD:
                now        = time.time()
                to_restore = [
                    ctr for ctr, info in _killed_services.items()
                    if now - info["killed_at"] >= RECOVERY_DELAY
                ]
                for ctr in to_restore:
                    info = _killed_services.pop(ctr)
                    try:
                        client.containers.get(ctr).start()
                        log.info("Restored %s (was %s)", ctr, info.get("reason", "oom"))
                        kill_log.appendleft({
                            "event":     "restore",
                            "service":   info["svc_name"],
                            "container": ctr,
                            "vram_used": round(vram.get("used_gb", 0), 2),
                            "ts":        time.time(),
                            "reason":    info.get("reason", "oom"),
                        })
                    except Exception as e:
                        log.error("Failed to restore %s: %s", ctr, e)
                        _killed_services[ctr] = info  # retry next cycle

            # ── 3. OOM enforcement — only when a mode is active ───────────────
            if not active_modes:
                continue

            if pct < HARD_THRESHOLD:
                _last_kill = None  # reset cooldown when pressure drops
                continue

            candidates = []
            for svc_name, svc in services.items():
                ctr = svc.get("container")
                if not ctr:
                    continue
                if svc.get("limit_mode", "soft") == "soft":
                    continue
                try:
                    c = client.containers.get(ctr)
                    if c.status != "running":
                        continue
                except Exception:
                    continue
                candidates.append((svc.get("priority", 3), svc_name, ctr))

            if not candidates:
                log.warning("VRAM %.0f%% — no stoppable services (all soft-limit)", pct * 100)
                continue

            candidates.sort(key=lambda x: -x[0])
            priority, target_svc, target_ctr = candidates[0]

            if target_ctr == _last_kill:
                log.warning("Cooldown: %s already stopped, skipping", target_ctr)
                continue

            log.warning(
                "OOM: VRAM %.1f/%.1f GB (%.0f%%) — stopping %s (priority %d)",
                vram["used_gb"], vram["total_gb"], pct * 100, target_svc, priority,
            )
            try:
                client.containers.get(target_ctr).stop(timeout=10)
                _last_kill = target_ctr
                _killed_services[target_ctr] = {
                    "svc_name":  target_svc,
                    "priority":  priority,
                    "killed_at": time.time(),
                    "reason":    "oom",
                }
                kill_log.appendleft({
                    "event":     "kill",
                    "service":   target_svc,
                    "container": target_ctr,
                    "priority":  priority,
                    "vram_used": round(vram.get("used_gb", 0), 2),
                    "ts":        time.time(),
                    "reason":    "oom",
                })
                log.info("Stopped %s", target_ctr)
            except Exception as e:
                log.error("Failed to stop %s: %s", target_ctr, e)

        except Exception as e:
            log.error("Enforcer loop error: %s", e)
