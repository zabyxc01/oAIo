"""
oLLMo API — system orchestration.
Port: 9000
"""
import json
import os
import asyncio
import psutil
import httpx
import docker as docker_sdk
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.vram import get_vram_usage, get_gpu_utilization
from core.docker_control import get_status, start, stop, get_logs, all_status
from core.paths import get_all_paths, repoint, get_storage_stats
from core.resources import projected_vram, check_alerts, get_system_accounting
from core.enforcer import enforcement_loop, active_modes as _active_modes, kill_log as _kill_log
from core import ram_tier
from core.extensions import load_all as _load_extensions, list_all as _list_extensions, \
    set_enabled as _ext_set_enabled, EXTENSIONS_DIR

OLLAMA_URL        = os.environ.get("OLLAMA_URL",        "http://localhost:11434")
COMFYUI_USER_PATH = Path(os.environ.get("COMFYUI_USER_PATH", str(Path.home() / "ComfyUI" / "user")))
RVC_GRADIO        = os.environ.get("RVC_GRADIO", "http://rvc:7865")


def _docker_client():
    return docker_sdk.from_env()

async def _rvc_startup_refresh():
    """On boot, call RVC Gradio /infer_refresh so the model list is populated."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(f"{RVC_GRADIO}/run/infer_refresh", json={"data": []})
    except Exception:
        pass  # RVC may not be up yet — not fatal


def _persist_active_modes():
    """Write current active_modes to disk so it survives restarts."""
    ACTIVE_MODES_FILE.write_text(json.dumps(list(_active_modes)))

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Restore active_modes from last session
    if ACTIVE_MODES_FILE.exists():
        try:
            for m in json.loads(ACTIVE_MODES_FILE.read_text()):
                _active_modes.add(m)
        except Exception:
            pass

    asyncio.create_task(_rvc_startup_refresh())
    task = asyncio.create_task(
        enforcement_loop(_services_cfg, _docker_client)
    )
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

app = FastAPI(title="oLLMo", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"
TEMPLATES_DIR     = Path(__file__).parent.parent.parent / "templates"
PATHS_CFG_FILE    = CONFIG_DIR / "paths.json"
ROUTING_CFG_FILE  = CONFIG_DIR / "routing.json"
MODES_CFG_FILE    = CONFIG_DIR / "modes.json"
NODES_CFG_FILE    = CONFIG_DIR / "nodes.json"
SERVICES_CFG_FILE = CONFIG_DIR / "services.json"
ACTIVE_MODES_FILE = CONFIG_DIR / "active_modes.json"

# Always read fresh — never cache; services/modes are mutable at runtime
def _services_cfg() -> dict:
    return json.loads(SERVICES_CFG_FILE.read_text())["services"]

def _modes() -> dict:
    return json.loads(MODES_CFG_FILE.read_text())["modes"]

# Snapshot of modes at startup — used by reset endpoint
_MODES_SNAPSHOT: dict = json.loads(MODES_CFG_FILE.read_text())["modes"]

# Keep SERVICES name for any remaining internal references (stays in sync via _services_cfg)
SERVICES = _services_cfg()


@app.get("/system/status")
def system_status():
    vram = get_vram_usage()
    gpu  = get_gpu_utilization()
    ram  = psutil.virtual_memory()
    acct = get_system_accounting(_services_cfg())
    return {
        "vram":         vram,
        "gpu":          gpu,
        "ram":          {"used_gb": round(ram.used/1e9,2), "total_gb": round(ram.total/1e9,2), "percent": ram.percent},
        "ram_tier":     ram_tier.get_usage(),
        "accounting":   acct,
        "active_modes": list(_active_modes),
        "services":     all_status(SERVICES),
        "alerts":       check_alerts(),
    }


@app.get("/vram")
def vram_status():
    return get_vram_usage()


@app.get("/services")
def list_services():
    return _services_cfg()


@app.post("/services/{name}/start")
def start_service(name: str):
    services = _services_cfg()
    if name not in services:
        return {"error": f"Unknown service: {name}"}
    return start(services[name]["container"])


@app.post("/services/{name}/stop")
def stop_service(name: str):
    services = _services_cfg()
    if name not in services:
        return {"error": f"Unknown service: {name}"}
    return stop(services[name]["container"])


@app.get("/services/{name}/status")
def service_status(name: str):
    services = _services_cfg()
    if name not in services:
        return {"error": f"Unknown service: {name}"}
    return get_status(services[name]["container"])


@app.get("/services/{name}/logs")
def service_logs(name: str, lines: int = 50):
    services = _services_cfg()
    if name not in services:
        return {"error": f"Unknown service: {name}"}
    return {"logs": get_logs(services[name]["container"], lines)}


@app.get("/modes")
def list_modes():
    return _modes()


@app.get("/modes/{name}/check")
def check_mode(name: str):
    """Pre-flight VRAM check — call before activating to see if it fits."""
    modes = _modes()
    if name not in modes:
        return {"error": f"Unknown mode: {name}"}
    return projected_vram(modes[name], _services_cfg())


@app.post("/modes/{name}/activate")
def activate_mode(name: str, force: bool = False):
    modes    = _modes()
    services = _services_cfg()
    if name not in modes:
        return {"error": f"Unknown mode: {name}"}
    mode = modes[name]

    projection = projected_vram(mode, services)
    if projection["blocked"] and not force:
        return {
            "error":      "VRAM budget exceeded — activation blocked",
            "blocked":    True,
            "projection": projection,
        }

    active  = set(mode["services"])
    results = []
    for svc_name, svc in services.items():
        if not svc.get("container"):
            continue
        if svc_name in active:
            results.append(start(svc["container"]))
        else:
            results.append(stop(svc["container"]))

    _active_modes.add(name)
    _persist_active_modes()

    return {
        "mode":       name,
        "results":    results,
        "projection": projection,
        "warning":    projection["warning"],
    }


@app.post("/modes/{name}/deactivate")
def deactivate_mode(name: str):
    _active_modes.discard(name)
    _persist_active_modes()
    return {"deactivated": name, "active_modes": list(_active_modes)}


@app.get("/modes/{name}/allocations")
def get_allocations(name: str):
    modes = _modes()
    if name not in modes:
        return {"error": f"Unknown mode: {name}"}
    return {
        "allocations":   modes[name].get("allocations", {}),
        "vram_budget_gb": modes[name].get("vram_budget_gb", 0),
    }


@app.post("/modes/{name}/allocations/{service}")
def set_allocation(name: str, service: str, body: dict):
    """body: {"gb": 7.5} — update one service's VRAM allocation within a mode."""
    cfg = json.loads(MODES_CFG_FILE.read_text())
    if name not in cfg["modes"]:
        return {"error": f"Unknown mode: {name}"}
    gb = body.get("gb")
    if gb is None:
        return {"error": "gb is required"}
    cfg["modes"][name].setdefault("allocations", {})[service] = round(float(gb), 1)
    MODES_CFG_FILE.write_text(json.dumps(cfg, indent=2))
    return projected_vram(cfg["modes"][name], SERVICES)


@app.post("/modes/{name}/budget")
def set_budget(name: str, body: dict):
    """body: {"gb": 11} — update a mode's VRAM ceiling."""
    cfg = json.loads(MODES_CFG_FILE.read_text())
    if name not in cfg["modes"]:
        return {"error": f"Unknown mode: {name}"}
    gb = body.get("gb")
    if gb is None:
        return {"error": "gb is required"}
    cfg["modes"][name]["vram_budget_gb"] = round(float(gb), 1)
    MODES_CFG_FILE.write_text(json.dumps(cfg, indent=2))
    return projected_vram(cfg["modes"][name], SERVICES)


@app.get("/templates")
def list_templates():
    if not TEMPLATES_DIR.exists():
        return []
    return [f.stem for f in TEMPLATES_DIR.glob("*.json")]


@app.post("/templates/save")
def save_template(name: str, description: str = ""):
    template = {
        "name": name,
        "description": description,
        "services": {
            svc: get_status(cfg["container"])
            for svc, cfg in SERVICES.items()
            if cfg.get("container")
        }
    }
    TEMPLATES_DIR.mkdir(exist_ok=True)
    (TEMPLATES_DIR / f"{name}.json").write_text(json.dumps(template, indent=2))
    return {"saved": name}


@app.post("/templates/{name}/load")
def load_template(name: str):
    path = TEMPLATES_DIR / f"{name}.json"
    if not path.exists():
        return {"error": f"Template not found: {name}"}
    template = json.loads(path.read_text())
    results = []
    for svc_name, state in template["services"].items():
        if state.get("status") == "running":
            results.append(start(SERVICES[svc_name]["container"]))
        else:
            results.append(stop(SERVICES[svc_name]["container"]))
    return {"loaded": name, "results": results}


# ── Capability endpoints (Tier 3 sub-nodes) ──────────────────────────────────

@app.get("/services/ollama/models")
async def ollama_models():
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
        models = r.json().get("models", [])
        return [{"name": m["name"], "size_gb": round(m["size"] / 1e9, 1)} for m in models]
    except Exception as e:
        return {"error": str(e)}


@app.post("/services/ollama/models/{name}/load")
async def load_ollama_model(name: str):
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            await client.post(f"{OLLAMA_URL}/api/generate",
                json={"model": name, "prompt": "", "stream": False})
        return {"loaded": name}
    except Exception as e:
        return {"error": str(e)}


@app.get("/services/rvc/models")
def rvc_models():
    try:
        client = docker_sdk.from_env()
        container = client.containers.get("rvc")
        result = container.exec_run("find /rvc/assets/weights -name '*.pth'")
        files = result.output.decode().strip().split("\n")
        return [{"name": Path(f).stem, "file": Path(f).name}
                for f in files if f and f.endswith(".pth")]
    except Exception as e:
        return {"error": str(e)}


@app.post("/services/rvc/models/{name}/activate")
async def activate_rvc_model(name: str):
    """
    Switch RVC active voice model via the Gradio API.
    Calls /infer_refresh (populates list) then /infer_change_voice (selects model).
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Refresh model list
            await client.post(f"{RVC_GRADIO}/run/infer_refresh", json={"data": []})
            # Switch to requested model
            r = await client.post(
                f"{RVC_GRADIO}/run/infer_change_voice",
                json={"data": [f"{name}.pth", 0.33, 0.33]},
            )
        result = r.json()
        # Last item in returns is the auto-detected index path
        index_path = result.get("data", [None, None, None, None])[-1] or ""
        return {"activated": name, "index": index_path}
    except Exception as e:
        return {"error": str(e)}


@app.get("/services/comfyui/workflows")
def comfyui_workflows():
    workflows_path = COMFYUI_USER_PATH / "default" / "workflows"
    if not workflows_path.exists():
        return []
    files = sorted(f for f in workflows_path.glob("*.json") if not f.stem.startswith("."))
    return [{"name": f.stem, "file": f.name} for f in files]


# ── Paths & Routing config endpoints ─────────────────────────────────────────

@app.get("/config/paths")
def config_paths_list():
    cfg = json.loads(PATHS_CFG_FILE.read_text())
    return get_all_paths(cfg)


@app.post("/config/paths")
def config_paths_add(body: dict):
    """body: {"name": "mymodels", "label": "My Models", "target": "/data/models", "containers": ["comfyui"]}"""
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
    PATHS_CFG_FILE.write_text(json.dumps(cfg, indent=2))
    result = repoint(link, target)
    return {**result, "name": name, "label": label, "containers": ctrs}


@app.delete("/config/paths/{name}")
def config_paths_delete(name: str):
    cfg = json.loads(PATHS_CFG_FILE.read_text())
    if name not in cfg:
        return {"error": f"Unknown path: {name}"}
    link = Path(cfg[name]["link"])
    if link.is_symlink():
        link.unlink()
    del cfg[name]
    PATHS_CFG_FILE.write_text(json.dumps(cfg, indent=2))
    return {"deleted": name}


@app.post("/config/paths/{name}")
def config_paths_set(name: str, body: dict):
    """
    body: {"target": "/new/path"} — absolute path, or:
          {"target": "ram"}       — activate RAM tier (/dev/shm/oaio-<name>)
          {"target": "default"}   — revert to default_target from config
    Switching away from "ram" automatically cleans up the /dev/shm directory.
    """
    cfg = json.loads(PATHS_CFG_FILE.read_text())
    if name not in cfg:
        return {"error": f"Unknown path: {name}"}

    entry      = cfg[name]
    link       = entry["link"]
    new_target = body.get("target", "").strip()
    if not new_target:
        return {"error": "target is required"}

    # Detect if currently on RAM tier so we can clean up on flip-away
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
        ram_tier.deactivate(name)  # clean up shm without moving contents

    return repoint(link, new_target)


@app.get("/config/routing")
def config_routing_get():
    return json.loads(ROUTING_CFG_FILE.read_text())


@app.post("/config/routing")
def config_routing_set(body: dict):
    current = json.loads(ROUTING_CFG_FILE.read_text())
    current.update({k: v for k, v in body.items() if k in current})
    ROUTING_CFG_FILE.write_text(json.dumps(current, indent=2))
    return current


@app.get("/config/nodes")
def config_nodes_get():
    return json.loads(NODES_CFG_FILE.read_text())


@app.post("/config/nodes")
def config_nodes_set(body: dict):
    cfg = json.loads(NODES_CFG_FILE.read_text())
    cfg.update(body)
    NODES_CFG_FILE.write_text(json.dumps(cfg, indent=2))
    return cfg


@app.get("/config/storage/stats")
def config_storage_stats():
    return get_storage_stats()


@app.websocket("/ws")
async def ws_status(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            vram = get_vram_usage()
            gpu  = get_gpu_utilization()
            ram  = psutil.virtual_memory()
            svcs = _services_cfg()
            acct = get_system_accounting(svcs)
            payload = {
                "vram":         vram,
                "gpu":          gpu,
                "ram":          {"used_gb": round(ram.used/1e9,2), "total_gb": round(ram.total/1e9,2), "percent": ram.percent},
                "ram_tier":     ram_tier.get_usage(),
                "accounting":   acct,
                "active_modes": list(_active_modes),
                "services":     all_status(svcs),
                "alerts":       check_alerts(),
                "kill_log":     list(_kill_log)[:10],  # last 10 events
            }
            await websocket.send_json(payload)
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass


@app.get("/config/services")
def config_services_get():
    return json.loads(SERVICES_CFG_FILE.read_text())["services"]


@app.post("/config/services")
def config_services_add(body: dict):
    name = body.get("name", "").strip().lower().replace(" ", "-")
    if not name:
        return {"error": "name is required"}
    cfg = json.loads(SERVICES_CFG_FILE.read_text())
    if name in cfg["services"]:
        return {"error": f"Service '{name}' already registered"}
    cfg["services"][name] = {
        "container":    body.get("container", name),
        "port":         body.get("port", 0),
        "vram_est_gb":  body.get("vram_est_gb", 0),
        "ram_est_gb":   body.get("ram_est_gb", 0),
        "priority":     body.get("priority", 3),
        "limit_mode":   body.get("limit_mode", "soft"),
        "group":        body.get("group", "Other"),
        "description":  body.get("description", ""),
        "capabilities": body.get("capabilities", []),
    }
    SERVICES_CFG_FILE.write_text(json.dumps(cfg, indent=2))
    return cfg["services"][name]


@app.get("/enforcement/status")
def enforcement_status():
    """Current enforcement state — thresholds, live VRAM, what would be killed next."""
    vram = get_vram_usage()
    pct  = vram.get("percent", 0) / 100
    services = _services_cfg()

    # Determine kill order (for UI visibility)
    candidates = []
    try:
        client = _docker_client()
        for svc_name, svc in services.items():
            ctr = svc.get("container")
            if not ctr:
                continue
            limit_mode = svc.get("limit_mode", "soft")
            try:
                c      = client.containers.get(ctr)
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

    from core.resources import WARN_THRESHOLD, HARD_THRESHOLD, _FALLBACK_TOTAL
    vram_total = vram.get("total_gb", _FALLBACK_TOTAL)
    return {
        "vram":            vram,
        "pct":             round(pct * 100, 1),
        "warn_threshold":  WARN_THRESHOLD,
        "hard_threshold":  HARD_THRESHOLD,
        "warn_at_gb":      round(vram_total * WARN_THRESHOLD, 1),
        "hard_at_gb":      round(vram_total * HARD_THRESHOLD, 1),
        "enforcing":       pct >= HARD_THRESHOLD and bool(_active_modes),
        "active_modes":    list(_active_modes),
        "paused":          not bool(_active_modes),
        "kill_order":      candidates,
        "kill_log":        list(_kill_log),
    }


@app.post("/modes/{name}/reset")
def reset_mode_allocations(name: str):
    """Restore a mode's allocations and budget to their startup snapshot values."""
    if name not in _MODES_SNAPSHOT:
        return {"error": f"Unknown mode: {name}"}
    cfg = json.loads(MODES_CFG_FILE.read_text())
    if name not in cfg["modes"]:
        return {"error": f"Unknown mode: {name}"}
    snap = _MODES_SNAPSHOT[name]
    cfg["modes"][name]["allocations"]   = snap.get("allocations", {})
    cfg["modes"][name]["vram_budget_gb"] = snap.get("vram_budget_gb", 0)
    MODES_CFG_FILE.write_text(json.dumps(cfg, indent=2))
    return {"reset": name, "allocations": cfg["modes"][name]["allocations"],
            "vram_budget_gb": cfg["modes"][name]["vram_budget_gb"]}


# ── Load extensions (routers mounted before static-file catch-all) ───────────
_load_extensions(app)


# ── Extension API ─────────────────────────────────────────────────────────────

@app.get("/extensions")
def extensions_list():
    return _list_extensions()


@app.post("/extensions/{name}/enable")
def extensions_enable(name: str):
    return _ext_set_enabled(name, True)


@app.post("/extensions/{name}/disable")
def extensions_disable(name: str):
    return _ext_set_enabled(name, False)


# ── Serve extension assets — before main frontend catch-all ───────────────────
if EXTENSIONS_DIR.exists():
    app.mount("/ext", StaticFiles(directory=str(EXTENSIONS_DIR)), name="extensions")

# ── Serve frontend — must be last (catches all unmatched paths) ───────────────
_frontend = Path(__file__).parent.parent.parent / "frontend" / "src"
if _frontend.exists():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000)
