"""
Reactive OOM enforcement loop.

Runs as a background asyncio task inside the FastAPI process.
Polls VRAM and host RAM every POLL_INTERVAL seconds.

Responsibilities:
  1. OOM kill   — stops lowest-priority hard-limit container when VRAM > 95%
  2. RAM kill   — stops lowest-priority hard-limit container when host RAM > 95%
  3. Recovery   — restarts enforcer-killed containers when both VRAM and RAM drop < 85%
  4. Crash watch — detects unexpected container exits and restores them with backoff

Actions by service limit_mode:
  soft  — warn only, never killed by enforcer (but crash-watched and restored)
  hard  — stopped on OOM, restored when pressure drops
"""
import asyncio
import json
import logging
import time
import traceback
from collections import deque
from pathlib import Path
import psutil
from .vram import get_vram_usage
from .vram_realtime import get_per_container_vram
from . import resources

log = logging.getLogger("enforcer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

KILL_LOG_FILE = Path(__file__).parent.parent.parent / "config" / "kill_log.json"

POLL_INTERVAL  = 5   # seconds between enforcement cycles
RECOVERY_DELAY = 30  # seconds after kill before attempting restart
CRASH_DELAY    = 30  # seconds after crash before attempting restart

# Shared state — mutated by activate_mode / deactivate_mode in main.py
active_modes: set[str] = set()

# Master enforcement toggle — when False, OOM kills are disabled (crash watch still runs)
enforcer_enabled: bool = True

# Virtual VRAM ceiling — when set, enforcer pretends GPU has this much total VRAM
vram_virtual_ceiling_gb: float | None = None

# Per-container VRAM snapshot — updated each cycle when enforcement_mode=realtime
per_container_vram: dict = {}

# Kill/crash/restore log — last 50 events, exported via WS + /enforcement/status
kill_log: deque = deque(maxlen=50)


def _load_kill_log():
    """Restore kill log from disk on startup."""
    if KILL_LOG_FILE.exists():
        try:
            entries = json.loads(KILL_LOG_FILE.read_text())
            for entry in entries:
                kill_log.append(entry)
        except Exception:
            pass


def _persist_kill_log():
    """Write kill log to disk atomically so it survives restarts."""
    try:
        tmp = KILL_LOG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(kill_log), indent=2))
        tmp.replace(KILL_LOG_FILE)
    except Exception:
        pass


_load_kill_log()

# Containers killed or crashed: {container_name: {svc_name, priority, killed_at, reason}}
# reason "manual" = user-stopped; never auto-restored
_killed_services: dict = {}

# Last known container status for crash detection: {container_name: str}
_prev_status: dict = {}


def register_manual_stop(container_name: str, svc_name: str, priority: int = 3):
    """Call this when a user manually stops a container so crash detection ignores it."""
    _killed_services[container_name] = {
        "svc_name":  svc_name,
        "priority":  priority,
        "killed_at": time.time(),
        "reason":    "manual",
    }


async def enforcement_loop(get_services_fn, get_docker_fn, get_enforcement_mode_fn=None):
    """
    get_services_fn()          — returns current services config dict (re-read each cycle)
    get_docker_fn()            — returns docker client (from_env)
    get_enforcement_mode_fn()  — returns "estimated" or "realtime" (optional, defaults to estimated)
    """
    log.info("Enforcement loop started (poll every %ds)", POLL_INTERVAL)
    _last_kill = None  # cooldown — don't kill same container twice in a row

    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            vram = get_vram_usage()
            if "error" in vram or vram.get("total_gb", 0) == 0:
                continue

            # Apply virtual VRAM ceiling if set (bottleneck simulator)
            if vram_virtual_ceiling_gb is not None and vram_virtual_ceiling_gb > 0:
                vram = dict(vram)  # don't mutate original
                vram["total_gb"] = vram_virtual_ceiling_gb
                used = vram.get("used_gb", 0)
                vram["percent"] = round((used / vram_virtual_ceiling_gb) * 100, 1) if vram_virtual_ceiling_gb else 0

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
                    _persist_kill_log()

                _prev_status[ctr] = status

            # ── 2. Recovery — restart killed/crashed containers when safe ─────
            ram = psutil.virtual_memory()
            ram_pct = ram.percent / 100
            ram_safe = ram_pct < resources.WARN_THRESHOLD
            if _killed_services and pct < resources.WARN_THRESHOLD and ram_safe:
                now        = time.time()
                to_restore = [
                    ctr for ctr, info in _killed_services.items()
                    if now - info["killed_at"] >= RECOVERY_DELAY
                ]
                for ctr in to_restore:
                    info = _killed_services.pop(ctr)
                    if info.get("reason") == "manual":
                        log.info("Skipping restore of %s (manual stop)", ctr)
                        continue
                    svc_cfg = services.get(info["svc_name"], {})
                    if not svc_cfg.get("auto_restore", True):
                        log.info("Skipping restore of %s (auto_restore=false)", ctr)
                        continue
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
                        _persist_kill_log()
                    except Exception as e:
                        log.error("Failed to restore %s: %s", ctr, e)
                        _killed_services[ctr] = info  # retry next cycle

            # ── 3. VRAM OOM enforcement — only when enabled + mode active ──
            _enforcement_mode = get_enforcement_mode_fn() if get_enforcement_mode_fn else "estimated"

            # Update per-container VRAM snapshot when in realtime mode
            if _enforcement_mode == "realtime":
                container_names = [s.get("container") for s in services.values() if s.get("container")]
                per_container_vram.update(get_per_container_vram(client, container_names))

            if enforcer_enabled and active_modes:
                if pct < resources.HARD_THRESHOLD:
                    _last_kill = None  # reset cooldown when pressure drops
                else:
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
                        # In realtime mode, use actual VRAM; in estimated, use priority
                        actual_vram = per_container_vram.get(ctr, 0) if _enforcement_mode == "realtime" else 0
                        candidates.append((svc.get("priority", 3), actual_vram, svc_name, ctr))

                    if not candidates:
                        log.warning("VRAM %.0f%% — no stoppable services (all soft-limit)", pct * 100)
                    else:
                        if _enforcement_mode == "realtime":
                            # Kill the container using the most VRAM (among hard-limit ones)
                            candidates.sort(key=lambda x: -x[1])
                        else:
                            # Kill by priority (highest number = least important)
                            candidates.sort(key=lambda x: -x[0])
                        priority, actual_vram, target_svc, target_ctr = candidates[0]

                        if target_ctr == _last_kill:
                            log.warning("Cooldown: %s already stopped, skipping", target_ctr)
                        else:
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
                                _persist_kill_log()
                                log.info("Stopped %s", target_ctr)
                            except Exception as e:
                                log.error("Failed to stop %s: %s", target_ctr, e)

            # ── 4. Host RAM OOM enforcement ───────────────────────────────
            if enforcer_enabled and active_modes and ram_pct >= resources.HARD_THRESHOLD:
                ram_candidates = []
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
                    ram_candidates.append((svc.get("priority", 3), svc_name, ctr))

                if not ram_candidates:
                    log.warning("Host RAM %.0f%% — no stoppable services (all soft-limit)", ram_pct * 100)
                else:
                    ram_candidates.sort(key=lambda x: -x[0])
                    ram_pri, ram_target_svc, ram_target_ctr = ram_candidates[0]

                    ram_used_gb = round(ram.used / 1e9, 2)
                    ram_total_gb = round(ram.total / 1e9, 2)

                    log.warning(
                        "RAM OOM: Host RAM %.1f/%.1f GB (%.0f%%) — stopping %s (priority %d)",
                        ram_used_gb, ram_total_gb, ram_pct * 100, ram_target_svc, ram_pri,
                    )
                    try:
                        client.containers.get(ram_target_ctr).stop(timeout=10)
                        _killed_services[ram_target_ctr] = {
                            "svc_name":  ram_target_svc,
                            "priority":  ram_pri,
                            "killed_at": time.time(),
                            "reason":    "ram_oom",
                        }
                        kill_log.appendleft({
                            "event":     "kill",
                            "service":   ram_target_svc,
                            "container": ram_target_ctr,
                            "priority":  ram_pri,
                            "ram_used":  ram_used_gb,
                            "ts":        time.time(),
                            "reason":    "ram_oom",
                        })
                        _persist_kill_log()
                        log.info("Stopped %s (RAM OOM)", ram_target_ctr)
                    except Exception as e:
                        log.error("Failed to stop %s: %s", ram_target_ctr, e)

        except Exception as e:
            log.error("Enforcer loop error: %s\n%s", e, traceback.format_exc())
