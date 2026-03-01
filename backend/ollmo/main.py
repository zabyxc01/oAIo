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
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.vram import get_vram_usage, get_gpu_utilization
from core.docker_control import get_status, start, stop, get_logs, all_status
from core.paths import get_all_paths, repoint, get_storage_stats
from core.resources import projected_vram, check_alerts
from core.enforcer import enforcement_loop, active_modes as _active_modes

OLLAMA_URL        = os.environ.get("OLLAMA_URL",        "http://localhost:11434")
COMFYUI_USER_PATH = Path(os.environ.get("COMFYUI_USER_PATH", str(Path.home() / "ComfyUI" / "user")))

def _services_live():
    return json.loads((CONFIG_DIR / "services.json").read_text())["services"]

def _docker_client():
    return docker_sdk.from_env()

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(
        enforcement_loop(_services_live, _docker_client)
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
SERVICES       = json.loads((CONFIG_DIR / "services.json").read_text())["services"]
TEMPLATES_DIR  = Path(__file__).parent.parent.parent / "templates"
PATHS_CFG_FILE   = CONFIG_DIR / "paths.json"
ROUTING_CFG_FILE = CONFIG_DIR / "routing.json"
MODES_CFG_FILE   = CONFIG_DIR / "modes.json"
NODES_CFG_FILE   = CONFIG_DIR / "nodes.json"

def _modes() -> dict:
    """Always read fresh — modes.json is mutable at runtime."""
    return json.loads(MODES_CFG_FILE.read_text())["modes"]

MODES = _modes()  # cached reference for startup validation


@app.get("/system/status")
def system_status():
    vram = get_vram_usage()
    gpu = get_gpu_utilization()
    ram = psutil.virtual_memory()
    return {
        "vram": vram,
        "gpu": gpu,
        "ram": {
            "used_gb": round(ram.used / 1e9, 2),
            "total_gb": round(ram.total / 1e9, 2),
            "percent": ram.percent
        },
        "services": all_status(SERVICES),
        "alerts": check_alerts(),
    }


@app.get("/vram")
def vram_status():
    return get_vram_usage()


@app.get("/services")
def list_services():
    return SERVICES


@app.post("/services/{name}/start")
def start_service(name: str):
    if name not in SERVICES:
        return {"error": f"Unknown service: {name}"}
    return start(SERVICES[name]["container"])


@app.post("/services/{name}/stop")
def stop_service(name: str):
    if name not in SERVICES:
        return {"error": f"Unknown service: {name}"}
    return stop(SERVICES[name]["container"])


@app.get("/services/{name}/status")
def service_status(name: str):
    if name not in SERVICES:
        return {"error": f"Unknown service: {name}"}
    return get_status(SERVICES[name]["container"])


@app.get("/services/{name}/logs")
def service_logs(name: str, lines: int = 50):
    if name not in SERVICES:
        return {"error": f"Unknown service: {name}"}
    return {"logs": get_logs(SERVICES[name]["container"], lines)}


@app.get("/modes")
def list_modes():
    return _modes()


@app.get("/modes/{name}/check")
def check_mode(name: str):
    """Pre-flight VRAM check — call before activating to see if it fits."""
    modes = _modes()
    if name not in modes:
        return {"error": f"Unknown mode: {name}"}
    return projected_vram(modes[name], SERVICES)


@app.post("/modes/{name}/activate")
def activate_mode(name: str, force: bool = False):
    modes = _modes()
    if name not in modes:
        return {"error": f"Unknown mode: {name}"}
    mode = modes[name]

    projection = projected_vram(mode, SERVICES)
    if projection["blocked"] and not force:
        return {
            "error":      "VRAM budget exceeded — activation blocked",
            "blocked":    True,
            "projection": projection,
        }

    active = set(mode["services"])
    results = []
    for svc_name, svc in SERVICES.items():
        if not svc.get("container"):
            continue
        if svc_name in active:
            results.append(start(svc["container"]))
        else:
            results.append(stop(svc["container"]))

    _active_modes.add(name)

    return {
        "mode":       name,
        "results":    results,
        "projection": projection,
        "warning":    projection["warning"],
    }


@app.post("/modes/{name}/deactivate")
def deactivate_mode(name: str):
    _active_modes.discard(name)
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
def activate_rvc_model(name: str):
    try:
        client = docker_sdk.from_env()
        container = client.containers.get("rvc")
        # Find the index file for this model
        idx_result = container.exec_run(
            f"find /rvc/assets/indices -name '*{name}*' -name '*.index'"
        )
        index_path = idx_result.output.decode().strip().split("\n")[0] or ""
        # Restart proxy with new model env vars
        container.exec_run(
            f"bash -c 'pkill -f rvc_proxy.py; "
            f"RVC_MODEL={name}.pth RVC_INDEX={index_path} "
            f"python3 /rvc/rvc_proxy.py > /tmp/proxy.log 2>&1 &'"
        )
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
    """body: {"target": "/new/path"}"""
    cfg = json.loads(PATHS_CFG_FILE.read_text())
    if name not in cfg:
        return {"error": f"Unknown path: {name}"}
    link = cfg[name]["link"]
    new_target = body.get("target", "")
    if not new_target:
        return {"error": "target is required"}
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


@app.get("/enforcement/status")
def enforcement_status():
    """Current enforcement state — thresholds, live VRAM, what would be killed next."""
    vram = get_vram_usage()
    pct  = vram.get("percent", 0) / 100
    services = _services_live()

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

    from core.resources import WARN_THRESHOLD, HARD_THRESHOLD, VRAM_TOTAL_GB
    return {
        "vram":            vram,
        "pct":             round(pct * 100, 1),
        "warn_threshold":  WARN_THRESHOLD,
        "hard_threshold":  HARD_THRESHOLD,
        "warn_at_gb":      round(VRAM_TOTAL_GB * WARN_THRESHOLD, 1),
        "hard_at_gb":      round(VRAM_TOTAL_GB * HARD_THRESHOLD, 1),
        "enforcing":       pct >= HARD_THRESHOLD and bool(_active_modes),
        "active_modes":    list(_active_modes),
        "paused":          not bool(_active_modes),
        "kill_order":      candidates,
    }


# ── Serve frontend — must be last (catches all unmatched paths) ───────────────
_frontend = Path(__file__).parent.parent.parent / "frontend" / "src"
if _frontend.exists():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000)
