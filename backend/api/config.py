"""
Config routes — paths, routing, nodes, storage, boot, services config, profiles.
"""
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

from api.shared import (
    CONTAINER_NAME_RE,
    PATHS_CFG_FILE, ROUTING_CFG_FILE, NODES_CFG_FILE, SERVICES_CFG_FILE,
    MODES_CFG_FILE, PROFILES_CFG_FILE,
    config_lock, atomic_write, services_cfg, modes, enforcement_mode,
    profiles_cfg, save_profiles, apply_profile, deactivate_profile,
    enforcer,
    get_all_paths, repoint, get_storage_stats,
    discover_workflows, export_workflow,
    discover_unregistered,
    set_restart_policy,
)
from core import ram_tier, resources

router = APIRouter()


# ── Merged Config ────────────────────────────────────────────────────────────

_COMPANION_STATE_FILE = Path(__file__).parent.parent.parent / "extensions" / "companion" / "companion.json"


@router.get("/config", tags=["Config"])
async def get_merged_config():
    """Single endpoint returning merged view of all config sections."""
    result = {}

    # Services
    try:
        result["services"] = services_cfg()
    except Exception:
        result["services"] = {}

    # Modes
    try:
        result["modes"] = modes()
    except Exception:
        result["modes"] = {}

    # Paths
    try:
        cfg = json.loads(PATHS_CFG_FILE.read_text())
        result["paths"] = get_all_paths(cfg)
    except Exception:
        result["paths"] = []

    # Companion
    try:
        if _COMPANION_STATE_FILE.exists():
            companion = json.loads(_COMPANION_STATE_FILE.read_text())
            result["companion"] = companion.get("config", {})
        else:
            result["companion"] = {}
    except Exception:
        result["companion"] = {}

    # System
    result["system"] = {
        "enforcement_mode": enforcement_mode(),
        "enforcer_enabled": enforcer.enforcer_enabled,
        "vram_ceiling_gb": enforcer.vram_virtual_ceiling_gb,
        "warn_threshold": resources.WARN_THRESHOLD,
        "hard_threshold": resources.HARD_THRESHOLD,
    }
    try:
        cfg = json.loads(SERVICES_CFG_FILE.read_text())
        result["system"]["boot_with_system"] = cfg.get("boot_with_system", True)
    except Exception:
        result["system"]["boot_with_system"] = True

    return result


@router.patch("/config/{section}", tags=["Config"])
async def patch_config_section(section: str, body: dict):
    """Write to the correct config file based on section."""
    if section == "companion":
        async with config_lock:
            if _COMPANION_STATE_FILE.exists():
                state = json.loads(_COMPANION_STATE_FILE.read_text())
            else:
                state = {"config": {}, "clients": {}}
            allowed = {"ollama_model", "tts_voice", "tts_engine", "tts_compress",
                        "tts_mode", "ref_audio", "system_prompt"}
            for k, v in body.items():
                if k in allowed:
                    state["config"][k] = v
            atomic_write(_COMPANION_STATE_FILE, json.dumps(state, indent=2))
            return {"updated": section, "config": state["config"]}

    elif section == "system":
        import math
        if "vram_ceiling_gb" in body:
            val = body["vram_ceiling_gb"]
            if val is None or val == 0:
                enforcer.vram_virtual_ceiling_gb = None
            else:
                enforcer.vram_virtual_ceiling_gb = float(val)
        if "warn_threshold" in body:
            resources.WARN_THRESHOLD = max(0.0, min(1.0, float(body["warn_threshold"])))
        if "hard_threshold" in body:
            resources.HARD_THRESHOLD = max(0.0, min(1.0, float(body["hard_threshold"])))
        if "enforcer_enabled" in body:
            enforcer.enforcer_enabled = bool(body["enforcer_enabled"])
        if "enforcement_mode" in body:
            async with config_lock:
                cfg = json.loads(SERVICES_CFG_FILE.read_text())
                cfg["enforcement_mode"] = body["enforcement_mode"]
                atomic_write(SERVICES_CFG_FILE, json.dumps(cfg, indent=2))
        return {"updated": section}

    else:
        raise HTTPException(status_code=400, detail=f"Unknown config section: {section}. Use: companion, system")


# ── Paths ────────────────────────────────────────────────────────────────────

@router.get("/config/paths", tags=["Config"])
def config_paths_list():
    cfg = json.loads(PATHS_CFG_FILE.read_text())
    return get_all_paths(cfg)


@router.post("/config/paths", tags=["Config"])
async def config_paths_add(body: dict):
    async with config_lock:
        name   = body.get("name", "").strip().lower().replace(" ", "-")
        label  = body.get("label", name)
        target = body.get("target", "")
        ctrs   = body.get("containers", [])
        if not name or not target:
            return {"error": "name and target are required"}
        cfg = json.loads(PATHS_CFG_FILE.read_text())
        if name in cfg:
            return {"error": f"Path '{name}' already exists — use POST /config/paths/{name} to repoint"}
        link = f"/mnt/oaio/{name}"
        cfg[name] = {"label": label, "link": link, "default_target": target, "containers": ctrs}
        atomic_write(PATHS_CFG_FILE, json.dumps(cfg, indent=2))
        result = repoint(link, target)
        return {**result, "name": name, "label": label, "containers": ctrs}


@router.delete("/config/paths/{name}", tags=["Config"])
async def config_paths_delete(name: str):
    async with config_lock:
        cfg = json.loads(PATHS_CFG_FILE.read_text())
        if name not in cfg:
            return {"error": f"Unknown path: {name}"}
        link = Path(cfg[name]["link"])
        if link.is_symlink():
            link.unlink()
        del cfg[name]
        atomic_write(PATHS_CFG_FILE, json.dumps(cfg, indent=2))
        return {"deleted": name}


@router.post("/config/paths/{name}", tags=["Config"])
async def config_paths_set(name: str, body: dict):
    async with config_lock:
        cfg = json.loads(PATHS_CFG_FILE.read_text())
        if name not in cfg:
            return {"error": f"Unknown path: {name}"}

        entry      = cfg[name]
        link       = entry["link"]
        new_target = body.get("target", "").strip()
        if not new_target:
            return {"error": "target is required"}

        try:
            import os as _os
            current_target = _os.readlink(link)
            currently_ram  = current_target.startswith("/dev/shm/oaio-")
        except OSError:
            currently_ram = False

        if new_target == "ram":
            result = ram_tier.activate(name)
            if not result["ok"]:
                return result
            new_target = result["path"]
        elif new_target == "default":
            new_target = entry["default_target"]
            if currently_ram:
                ram_tier.deactivate(name, move_to=new_target)
        elif currently_ram:
            ram_tier.deactivate(name)

        return repoint(link, new_target)


# ── Routing ──────────────────────────────────────────────────────────────────

@router.get("/config/routing", tags=["Config"])
def config_routing_get():
    return json.loads(ROUTING_CFG_FILE.read_text())


@router.post("/config/routing", tags=["Config"])
def config_routing_set(body: dict):
    current = json.loads(ROUTING_CFG_FILE.read_text())
    current.update({k: v for k, v in body.items() if k in current})
    atomic_write(ROUTING_CFG_FILE, json.dumps(current, indent=2))
    return current


# ── Nodes ────────────────────────────────────────────────────────────────────

@router.get("/config/nodes", tags=["Config"])
def config_nodes_get():
    return json.loads(NODES_CFG_FILE.read_text())


@router.post("/config/nodes", tags=["Config"])
def config_nodes_set(body: dict):
    cfg = json.loads(NODES_CFG_FILE.read_text())
    cfg.update(body)
    atomic_write(NODES_CFG_FILE, json.dumps(cfg, indent=2))
    return cfg


# ── Storage ──────────────────────────────────────────────────────────────────

@router.get("/config/storage/stats", tags=["Config"])
def config_storage_stats():
    return get_storage_stats()


# ── Boot config ──────────────────────────────────────────────────────────────

@router.get("/config/boot", tags=["Config"])
def get_boot_config():
    cfg = json.loads(SERVICES_CFG_FILE.read_text())
    return {"enabled": cfg.get("boot_with_system", True)}


@router.post("/config/boot", tags=["Config"])
async def set_boot_config(body: dict):
    enabled = body.get("enabled")
    if enabled is None:
        return {"error": "enabled (bool) is required"}
    enabled = bool(enabled)
    async with config_lock:
        cfg = json.loads(SERVICES_CFG_FILE.read_text())
        cfg["boot_with_system"] = enabled
        atomic_write(SERVICES_CFG_FILE, json.dumps(cfg, indent=2))

    services = cfg.get("services", {})
    results = []
    for svc_name, svc in services.items():
        ctr = svc.get("container")
        if not ctr:
            continue
        if enabled and svc.get("boot_with_system", False):
            policy = "unless-stopped"
        else:
            policy = "no"
        results.append(set_restart_policy(ctr, policy))

    return {"enabled": enabled, "results": results}


# ── Services config ─────────────────────────────────────────────────────────

@router.get("/config/services", tags=["Config"])
def config_services_get():
    return json.loads(SERVICES_CFG_FILE.read_text())["services"]


@router.post("/config/services", tags=["Config"])
async def config_services_add(body: dict):
    name = body.get("name", "").strip().lower().replace(" ", "-")
    if not name:
        return {"error": "name is required"}
    if not CONTAINER_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"Invalid service name: must match {CONTAINER_NAME_RE.pattern}")
    container = body.get("container", name)
    if not CONTAINER_NAME_RE.match(container):
        raise HTTPException(status_code=400, detail=f"Invalid container name: must match {CONTAINER_NAME_RE.pattern}")
    async with config_lock:
        cfg = json.loads(SERVICES_CFG_FILE.read_text())
        if name in cfg["services"]:
            return {"error": f"Service '{name}' already registered"}
        cfg["services"][name] = {
            "container":    container,
            "port":         body.get("port", 0),
            "vram_est_gb":  body.get("vram_est_gb", 0),
            "ram_est_gb":   body.get("ram_est_gb", 0),
            "priority":     body.get("priority", 3),
            "limit_mode":   body.get("limit_mode", "soft"),
            "group":        body.get("group", "Other"),
            "description":  body.get("description", ""),
            "capabilities": body.get("capabilities", []),
        }
        atomic_write(SERVICES_CFG_FILE, json.dumps(cfg, indent=2))
        return cfg["services"][name]


@router.patch("/config/services/{name}", tags=["Config"])
async def config_services_patch(name: str, body: dict):
    async with config_lock:
        cfg = json.loads(SERVICES_CFG_FILE.read_text())
        if name not in cfg["services"]:
            raise HTTPException(status_code=404, detail=f"Service '{name}' not found")
        ALLOWED = {"priority", "limit_mode", "vram_est_gb", "ram_est_gb", "auto_restore",
                   "description", "group", "boot_with_system", "memory_mode", "bus_preference"}
        for k, v in body.items():
            if k in ALLOWED:
                cfg["services"][name][k] = v
        atomic_write(SERVICES_CFG_FILE, json.dumps(cfg, indent=2))
        return cfg["services"][name]


# ── Docker discovery ─────────────────────────────────────────────────────────

@router.get("/docker/discover", tags=["Docker"])
def docker_discover():
    registered = set(s.get("container", k) for k, s in services_cfg().items())
    return discover_unregistered(registered)


# ── Workflows ────────────────────────────────────────────────────────────────

@router.get("/workflows", tags=["Workflows"])
def workflows_list():
    return discover_workflows()


@router.get("/workflows/export", tags=["Workflows"])
def workflows_export(path: str):
    if not path or ".." in path:
        raise HTTPException(status_code=400, detail="Invalid path")
    result = export_workflow(path)
    if result is None:
        raise HTTPException(status_code=404, detail="Workflow not found or not readable")
    return result


# ── Profiles ─────────────────────────────────────────────────────────────────

@router.get("/config/profiles", tags=["Config"])
def config_profiles_get():
    return profiles_cfg()


@router.post("/config/profiles", tags=["Config"])
def config_profiles_add(body: dict):
    name = body.get("name", "").strip()
    if not name:
        return {"error": "name is required"}
    key = name.lower().replace(" ", "-")
    cfg = profiles_cfg()
    if key in cfg.get("profiles", {}):
        return {"error": f"Profile '{key}' already exists"}
    cfg.setdefault("profiles", {})[key] = {
        "name":        name,
        "vram_gb":     body.get("vram_gb", 0),
        "ram_gb":      body.get("ram_gb", 0),
        "cpu_cores":   body.get("cpu_cores", 0),
        "io_mbps":     body.get("io_mbps", 0),
        "description": body.get("description", ""),
    }
    save_profiles(cfg)
    return {"created": key, "profile": cfg["profiles"][key]}


@router.post("/config/profiles/{name}/activate", tags=["Config"])
def config_profiles_activate(name: str):
    cfg = profiles_cfg()
    if name not in cfg.get("profiles", {}):
        return {"error": f"Unknown profile: {name}"}
    profile = cfg["profiles"][name]
    apply_profile(profile, name)
    cfg["active"] = name
    save_profiles(cfg)
    return {"activated": name, "profile": profile, "vram_ceiling_gb": enforcer.vram_virtual_ceiling_gb}


@router.post("/config/profiles/deactivate", tags=["Config"])
def config_profiles_deactivate():
    deactivate_profile()
    cfg = profiles_cfg()
    cfg["active"] = None
    save_profiles(cfg)
    return {"deactivated": True, "vram_ceiling_gb": None}
