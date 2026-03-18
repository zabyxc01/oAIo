"""
Mode management routes — CRUD, activate/deactivate, templates, emergency.
"""
import copy
import json
import re
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException

from api.shared import (
    MODES_CFG_FILE, TEMPLATES_DIR, MODES_SNAPSHOT,
    config_lock, atomic_write, services_cfg, modes, write_modes,
    active_modes, persist_active_modes,
    register_manual_stop, projected_vram,
    build_service_urls, route_manager, load_graph,
    get_status, start, stop,
)

router = APIRouter()


@router.get("/modes", tags=["Modes"])
def list_modes():
    return modes()


@router.get("/modes/{name}/check", tags=["Modes"])
def check_mode(name: str):
    m = modes()
    if name not in m:
        return {"error": f"Unknown mode: {name}"}
    return projected_vram(m[name], services_cfg())


@router.post("/modes/{name}/activate", tags=["Modes"])
def activate_mode(name: str, force: bool = False):
    m = modes()
    services = services_cfg()
    if name not in m:
        return {"error": f"Unknown mode: {name}"}
    mode = m[name]

    projection = projected_vram(mode, services)
    if projection["blocked"] and not force:
        return {
            "error":      "VRAM budget exceeded — activation blocked",
            "blocked":    True,
            "projection": projection,
        }

    new_svc_set = set(mode["services"])

    # Displacement: stop old-mode services not needed by the new mode
    old_svc_set: set[str] = set()
    for active_name in list(active_modes):
        old_mode = m.get(active_name)
        if old_mode:
            old_svc_set.update(old_mode.get("services", []))
    displaced = old_svc_set - new_svc_set

    results = []
    for svc_name in displaced:
        svc = services.get(svc_name)
        if not svc:
            continue
        ctr = svc.get("container")
        if not ctr:
            continue
        register_manual_stop(ctr, svc_name, svc.get("priority", 3))
        stop(ctr)
        results.append({"name": ctr, "action": "stopped", "ok": True})

    for svc_name in mode["services"]:
        svc = services.get(svc_name)
        if not svc:
            continue
        ctr = svc.get("container")
        if not ctr:
            continue
        results.append(start(ctr))

    active_modes.clear()
    active_modes.add(name)
    persist_active_modes()

    graph_id = mode.get("graph_state")
    if graph_id:
        gs = load_graph(graph_id)
        if gs:
            svc_urls = build_service_urls(services)
            route_manager.set_active_graph(gs, svc_urls)

    return {
        "mode":       name,
        "results":    results,
        "projection": projection,
        "warning":    projection["warning"],
        "graph_state": graph_id,
    }


@router.post("/modes/{name}/deactivate", tags=["Modes"])
def deactivate_mode(name: str):
    active_modes.discard(name)
    persist_active_modes()
    route_manager.clear()
    return {"deactivated": name, "active_modes": list(active_modes)}


@router.post("/modes/{name}/bind-graph", tags=["Modes"])
async def bind_graph_to_mode(name: str, body: dict):
    graph_id = body.get("graph_id", "").strip()
    if not graph_id:
        raise HTTPException(status_code=400, detail="graph_id required")
    gs = load_graph(graph_id)
    if not gs:
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    async with config_lock:
        cfg = json.loads(MODES_CFG_FILE.read_text())
        if name not in cfg["modes"]:
            raise HTTPException(status_code=404, detail=f"Mode '{name}' not found")
        cfg["modes"][name]["graph_state"] = graph_id
        atomic_write(MODES_CFG_FILE, json.dumps(cfg, indent=2))
    return {"mode": name, "graph_state": graph_id}


@router.post("/emergency/kill", tags=["Enforcement"])
def emergency_kill():
    services = services_cfg()
    active_modes.clear()
    persist_active_modes()
    results = []
    for svc_name, svc in services.items():
        ctr = svc.get("container")
        if not ctr:
            continue
        register_manual_stop(ctr, svc_name, svc.get("priority", 3))
        threading.Thread(target=stop, args=(ctr,), daemon=True).start()
        results.append(ctr)
    return {"killed": results, "active_modes": []}


@router.get("/modes/{name}/allocations", tags=["Modes"])
def get_allocations(name: str):
    m = modes()
    if name not in m:
        return {"error": f"Unknown mode: {name}"}
    return {
        "allocations":    m[name].get("allocations", {}),
        "vram_budget_gb": m[name].get("vram_budget_gb", 0),
    }


@router.post("/modes/{name}/allocations/{service}", tags=["Modes"])
async def set_allocation(name: str, service: str, body: dict):
    async with config_lock:
        cfg = json.loads(MODES_CFG_FILE.read_text())
        if name not in cfg["modes"]:
            return {"error": f"Unknown mode: {name}"}
        gb = body.get("gb")
        if gb is None:
            return {"error": "gb is required"}
        cfg["modes"][name].setdefault("allocations", {})[service] = round(float(gb), 1)
        write_modes(cfg)
        return projected_vram(cfg["modes"][name], services_cfg())


@router.post("/modes/{name}/budget", tags=["Modes"])
async def set_budget(name: str, body: dict):
    async with config_lock:
        cfg = json.loads(MODES_CFG_FILE.read_text())
        if name not in cfg["modes"]:
            return {"error": f"Unknown mode: {name}"}
        gb = body.get("gb")
        if gb is None:
            return {"error": "gb is required"}
        cfg["modes"][name]["vram_budget_gb"] = round(float(gb), 1)
        write_modes(cfg)
        return projected_vram(cfg["modes"][name], services_cfg())


@router.post("/modes", tags=["Modes"])
async def create_mode(body: dict):
    name = body.get("name", "").strip()
    if not name:
        return {"error": "name is required"}
    key = name.lower().replace(" ", "-")
    async with config_lock:
        cfg = json.loads(MODES_CFG_FILE.read_text())
        if key in cfg["modes"]:
            return {"error": f"Mode '{key}' already exists"}
        max_id = max((m.get("id", 0) for m in cfg["modes"].values()), default=0)
        svcs = body.get("services", [])
        known = set(services_cfg().keys())
        unknown = set(svcs) - known
        if unknown:
            return {"error": f"Unknown services: {', '.join(sorted(unknown))}"}
        budget = round(float(body.get("vram_budget_gb", 10)), 1)
        cfg["modes"][key] = {
            "id": max_id + 1,
            "name": name,
            "description": body.get("description", ""),
            "services": svcs,
            "vram_budget_gb": budget,
            "allocations": {s: 0 for s in svcs},
            "boot_image": None,
        }
        write_modes(cfg)
        return {"created": key, "mode": cfg["modes"][key]}


@router.delete("/modes/{name}", tags=["Modes"])
async def delete_mode(name: str):
    async with config_lock:
        cfg = json.loads(MODES_CFG_FILE.read_text())
        if name not in cfg["modes"]:
            return {"error": f"Unknown mode: {name}"}
        active_modes.discard(name)
        persist_active_modes()
        del cfg["modes"][name]
        write_modes(cfg)
        return {"deleted": name}


@router.patch("/modes/{name}", tags=["Modes"])
async def patch_mode(name: str, body: dict):
    async with config_lock:
        cfg = json.loads(MODES_CFG_FILE.read_text())
        if name not in cfg["modes"]:
            return {"error": f"Unknown mode: {name}"}
        mode = cfg["modes"][name]
        if "name" in body:
            mode["name"] = body["name"]
        if "description" in body:
            mode["description"] = body["description"]
        if "services" in body:
            known = set(services_cfg().keys())
            unknown = set(body["services"]) - known
            if unknown:
                return {"error": f"Unknown services: {', '.join(sorted(unknown))}"}
            mode["services"] = body["services"]
            for s in body["services"]:
                mode.setdefault("allocations", {}).setdefault(s, 0)
        if "vram_budget_gb" in body:
            mode["vram_budget_gb"] = round(float(body["vram_budget_gb"]), 1)
        write_modes(cfg)
        return {"updated": name, "mode": mode}


@router.post("/modes/{name}/reset", tags=["Modes"])
async def reset_mode_allocations(name: str):
    if name not in MODES_SNAPSHOT:
        return {"error": f"Unknown mode: {name}"}
    async with config_lock:
        cfg = json.loads(MODES_CFG_FILE.read_text())
        if name not in cfg["modes"]:
            return {"error": f"Unknown mode: {name}"}
        snap = copy.deepcopy(MODES_SNAPSHOT[name])
        cfg["modes"][name]["allocations"]   = snap.get("allocations", {})
        cfg["modes"][name]["vram_budget_gb"] = snap.get("vram_budget_gb", 0)
        write_modes(cfg)
        return {"reset": name, "allocations": cfg["modes"][name]["allocations"],
                "vram_budget_gb": cfg["modes"][name]["vram_budget_gb"]}


# ── Templates ────────────────────────────────────────────────────────────────

@router.get("/templates", tags=["Templates"])
def list_templates():
    if not TEMPLATES_DIR.exists():
        return []
    return [f.stem for f in TEMPLATES_DIR.glob("*.json")]


@router.post("/templates/save", tags=["Templates"])
def save_template(name: str, description: str = ""):
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise HTTPException(status_code=400, detail="Invalid template name: only alphanumeric, dash, underscore allowed")
    TEMPLATES_DIR.mkdir(exist_ok=True)
    path = (TEMPLATES_DIR / f"{name}.json").resolve()
    if not str(path).startswith(str(TEMPLATES_DIR.resolve())):
        return {"error": "Invalid template name"}
    template = {
        "name": name,
        "description": description,
        "services": {
            svc: get_status(cfg["container"])
            for svc, cfg in services_cfg().items()
            if cfg.get("container")
        }
    }
    atomic_write(path, json.dumps(template, indent=2))
    return {"saved": name}


@router.post("/templates/{name}/load", tags=["Templates"])
def load_template(name: str):
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise HTTPException(status_code=400, detail="Invalid template name: only alphanumeric, dash, underscore allowed")
    path = (TEMPLATES_DIR / f"{name}.json").resolve()
    if not str(path).startswith(str(TEMPLATES_DIR.resolve())):
        return {"error": "Invalid template name"}
    if not path.exists():
        return {"error": f"Template not found: {name}"}
    template = json.loads(path.read_text())
    services = services_cfg()
    results = []
    for svc_name, state in template["services"].items():
        if svc_name not in services:
            continue
        if state.get("status") == "running":
            results.append(start(services[svc_name]["container"]))
        else:
            results.append(stop(services[svc_name]["container"]))
    return {"loaded": name, "results": results}
