"""
Shared state, config helpers, and constants used across all API route modules.
"""
import asyncio
import copy
import json
import math
import os
import re
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import docker as docker_sdk
import httpx
import psutil

from core.vram import get_vram_usage, get_gpu_utilization
from core.docker_control import (
    get_status, start, stop, get_logs, all_status,
    apply_resource_limits, remove_resource_limits,
    discover_unregistered, set_restart_policy,
)
from core.paths import (
    get_all_paths, repoint, get_storage_stats, heal_dangling,
    discover_workflows, export_workflow,
)
from core.resources import projected_vram, check_alerts, get_system_accounting
from core.enforcer import (
    enforcement_loop, active_modes as _active_modes,
    kill_log as _kill_log, register_manual_stop,
)
import core.enforcer as _enforcer
from core import ram_tier
from core.extensions import (
    load_all as _load_extensions, list_all as _list_extensions,
    set_enabled as _ext_set_enabled, EXTENSIONS_DIR,
)
from core.graph import (
    make_graph, make_edge, save_graph, load_graph, list_graphs, delete_graph,
    validate_graph, add_node, remove_node, add_edge, remove_edge,
    graph_to_services_list, get_node_ports, get_edges_for_node,
)
from core.discovery import (
    discover_all, discover_service, discover_ollama_models,
    generate_default_graph, discover_service_dirs,
)
from core.router import route_manager

# ── Re-exports for route modules ─────────────────────────────────────────────
active_modes = _active_modes
kill_log = _kill_log
enforcer = _enforcer
load_extensions = _load_extensions
list_extensions = _list_extensions
ext_set_enabled = _ext_set_enabled

# ── Environment ──────────────────────────────────────────────────────────────
OLLAMA_URL        = os.environ.get("OLLAMA_URL",        "http://localhost:11434")
COMFYUI_USER_PATH = Path(os.environ.get("COMFYUI_USER_PATH", str(Path.home() / "ComfyUI" / "user")))
RVC_GRADIO        = os.environ.get("RVC_GRADIO", "http://rvc:7865")

# ── Config paths ─────────────────────────────────────────────────────────────
CONFIG_DIR         = Path(__file__).parent.parent.parent / "config"
TEMPLATES_DIR      = Path(__file__).parent.parent.parent / "templates"
PATHS_CFG_FILE     = CONFIG_DIR / "paths.json"
ROUTING_CFG_FILE   = CONFIG_DIR / "routing.json"
MODES_CFG_FILE     = CONFIG_DIR / "modes.json"
NODES_CFG_FILE     = CONFIG_DIR / "nodes.json"
SERVICES_CFG_FILE  = CONFIG_DIR / "services.json"
SCANS_CFG_FILE     = CONFIG_DIR / "scans.json"
SERVICE_PORTS_FILE = CONFIG_DIR / "service_ports.json"
ACTIVE_MODES_FILE  = CONFIG_DIR / "active_modes.json"
PROFILES_CFG_FILE  = CONFIG_DIR / "profiles.json"

# Docker container name rules
CONTAINER_NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*$')

# ── Config-file lock (prevents read-modify-write races) ─────────────────────
config_lock = asyncio.Lock()

# ── Benchmark history — rolling 5-minute window at 1Hz ──────────────────────
bench_history: deque = deque(maxlen=300)

# ── Request monitor state ────────────────────────────────────────────────────
req_log: deque = deque(maxlen=500)
req_stats = {"total": 0, "errors": 0, "latency_sum": 0.0, "endpoints": {}}
monitor_ws_clients: list = []


# ── Config helpers ───────────────────────────────────────────────────────────
def atomic_write(path: Path, data: str):
    """Write JSON atomically via temp file + rename to prevent corruption."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(data)
    tmp.replace(path)


def services_cfg() -> dict:
    """Always read fresh — never cache; services/modes are mutable at runtime."""
    return json.loads(SERVICES_CFG_FILE.read_text())["services"]


def enforcement_mode() -> str:
    cfg = json.loads(SERVICES_CFG_FILE.read_text())
    return cfg.get("enforcement_mode", "estimated")


def modes() -> dict:
    return json.loads(MODES_CFG_FILE.read_text())["modes"]


# Snapshot of modes — updated on every write so reset restores last-saved state
MODES_SNAPSHOT: dict = copy.deepcopy(json.loads(MODES_CFG_FILE.read_text())["modes"])


def write_modes(cfg: dict):
    """Write modes config and update the reset snapshot."""
    global MODES_SNAPSHOT
    atomic_write(MODES_CFG_FILE, json.dumps(cfg, indent=2))
    MODES_SNAPSHOT = copy.deepcopy(cfg["modes"])


def persist_active_modes():
    """Write current active_modes to disk so it survives restarts."""
    atomic_write(ACTIVE_MODES_FILE, json.dumps(list(active_modes)))


def profiles_cfg() -> dict:
    if PROFILES_CFG_FILE.exists():
        return json.loads(PROFILES_CFG_FILE.read_text())
    return {"active": None, "profiles": {}}


def save_profiles(cfg: dict):
    atomic_write(PROFILES_CFG_FILE, json.dumps(cfg, indent=2))


def apply_profile(profile: dict, profile_name: str):
    """Apply a hardware profile: set VRAM ceiling + Docker cgroup limits."""
    enforcer.vram_virtual_ceiling_gb = profile.get("vram_gb") or None

    services = services_cfg()
    running = []
    for svc_name, svc in services.items():
        ctr = svc.get("container")
        if not ctr:
            continue
        try:
            s = get_status(ctr)
            if s.get("status") == "running":
                running.append((svc_name, svc, ctr))
        except Exception:
            pass

    if not running:
        return

    profile_ram = profile.get("ram_gb", 0)
    profile_cpu = profile.get("cpu_cores", 0)

    total_svc_ram = sum(s.get("ram_est_gb", 1.0) for _, s, _ in running) or 1.0
    cpu_per = max(1, profile_cpu // len(running)) if profile_cpu and running else 0

    for svc_name, svc, ctr in running:
        svc_ram = svc.get("ram_est_gb", 1.0)
        mem_gb = round(max(0.25, (svc_ram / total_svc_ram) * profile_ram), 2) if profile_ram else 0
        apply_resource_limits(ctr, mem_gb, cpu_per)


def deactivate_profile():
    """Clear VRAM ceiling + remove all Docker cgroup limits."""
    enforcer.vram_virtual_ceiling_gb = None

    services = services_cfg()
    for svc_name, svc in services.items():
        ctr = svc.get("container")
        if not ctr:
            continue
        try:
            s = get_status(ctr)
            if s.get("status") == "running":
                remove_resource_limits(ctr)
        except Exception:
            pass


def docker_client():
    return docker_sdk.from_env()


async def get_ollama_loaded() -> list[dict]:
    """Return list of models currently loaded in Ollama VRAM."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{OLLAMA_URL}/api/ps")
            return r.json().get("models", [])
    except Exception:
        return []


def build_service_urls(services: dict) -> dict:
    """Build service URL map for the router from env vars + service ports."""
    urls = {
        "ollama": os.environ.get("OLLAMA_URL", "http://ollama:11434"),
        "kokoro-tts": os.environ.get("KOKORO_URL", "http://kokoro-tts:8000"),
        "rvc": os.environ.get("RVC_PROXY", "http://rvc:8001"),
        "f5-tts": os.environ.get("F5_TTS_URL", "http://f5-tts:7860"),
        "faster-whisper": os.environ.get("STT_URL", "http://faster-whisper:8003"),
    }
    for svc_name, svc in services.items():
        if svc_name not in urls:
            ctr = svc.get("container", svc_name)
            port = svc.get("port", 8000)
            urls[svc_name] = f"http://{ctr}:{port}"
    return urls


def build_kill_order(svcs: dict) -> list:
    """Build kill-order list from services config + live container status."""
    candidates = []
    try:
        dc = docker_client()
        for svc_name, svc in svcs.items():
            ctr = svc.get("container")
            if not ctr:
                continue
            limit_mode = svc.get("limit_mode", "soft")
            try:
                c = dc.containers.get(ctr)
                status = c.status
            except Exception:
                status = "unknown"
            candidates.append({
                "service":    svc_name,
                "container":  ctr,
                "priority":   svc.get("priority", 3),
                "limit_mode": limit_mode,
                "status":     status,
                "would_kill": limit_mode == "hard" and status == "running",
            })
        candidates.sort(key=lambda x: -x["priority"])
    except Exception:
        pass
    return candidates


def load_scans() -> dict:
    """Load cached scan results from disk."""
    if SCANS_CFG_FILE.exists():
        try:
            return json.loads(SCANS_CFG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}
