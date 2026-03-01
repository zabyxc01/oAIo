"""
Reactive OOM enforcement loop.

Runs as a background asyncio task inside the FastAPI process.
Polls VRAM every POLL_INTERVAL seconds. When hard threshold is hit,
kills the lowest-priority running service (highest priority number = lowest priority).

Actions by service limit_mode:
  soft  — warn only, never kill
  hard  — stop container on OOM
"""
import asyncio
import logging
from .vram import get_vram_usage
from .resources import HARD_THRESHOLD, WARN_THRESHOLD

log = logging.getLogger("enforcer")

POLL_INTERVAL = 5  # seconds

# Shared state — set by activate_mode / deactivate_mode in main.py
active_modes: set[str] = set()


async def enforcement_loop(get_services_fn, get_docker_fn):
    """
    get_services_fn() — returns current services config dict (re-read each cycle)
    get_docker_fn()   — returns docker client (from_env)
    """
    log.info("Reactive enforcement loop started (poll every %ds)", POLL_INTERVAL)
    _last_action = None  # cooldown: don't kill same service twice in a row

    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            # Only enforce when at least one mode is active — ignore gaming/other GPU usage
            if not active_modes:
                continue

            vram = get_vram_usage()
            if "error" in vram or vram.get("total_gb", 0) == 0:
                continue

            pct = vram.get("percent", 0) / 100
            if pct < HARD_THRESHOLD:
                _last_action = None  # reset cooldown when pressure drops
                continue

            # Hard threshold exceeded — find lowest-priority running service to stop
            services = get_services_fn()
            client   = get_docker_fn()

            candidates = []
            for svc_name, svc in services.items():
                container_name = svc.get("container")
                if not container_name:
                    continue
                if svc.get("limit_mode", "soft") == "soft":
                    continue  # soft mode — never auto-kill
                try:
                    c = client.containers.get(container_name)
                    if c.status != "running":
                        continue
                except Exception:
                    continue

                priority = svc.get("priority", 3)
                candidates.append((priority, svc_name, container_name))

            if not candidates:
                log.warning("VRAM at %.0f%% but no stoppable services (all soft-limit)", pct * 100)
                continue

            # Sort by priority descending (highest number = lowest priority = first to go)
            candidates.sort(key=lambda x: -x[0])
            _, target_svc, target_container = candidates[0]

            if target_container == _last_action:
                log.warning("Cooldown: already stopped %s, skipping duplicate action", target_container)
                continue

            log.warning(
                "OOM enforcement: VRAM %.1f/%.1f GB (%.0f%%) — stopping %s (priority %d)",
                vram["used_gb"], vram["total_gb"], pct * 100,
                target_svc, candidates[0][0]
            )

            try:
                c = client.containers.get(target_container)
                c.stop(timeout=10)
                _last_action = target_container
                log.info("Stopped %s", target_container)
            except Exception as e:
                log.error("Failed to stop %s: %s", target_container, e)

        except Exception as e:
            log.error("Enforcer loop error: %s", e)
