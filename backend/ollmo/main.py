"""
oLLMo API — system orchestration.
Port: 9000
"""
import copy
import json
import math
import os
import re
import asyncio
import threading
import time
import psutil
import httpx
import docker as docker_sdk
from functools import partial
from collections import deque
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

# Docker container name rules: start with alnum, then alnum + underscore/period/hyphen
_CONTAINER_NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*$')
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse, JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from urllib.parse import parse_qs

# ── Optional API token auth ──────────────────────────────────────────────────
_API_TOKEN = os.environ.get("OAIO_API_TOKEN", "").strip() or None

_AUTH_SKIP_PREFIXES = ("/static", "/litegraph", "/style", "/app", "/nodes", "/ext", "/panels",
                       "/extensions-loader", "/favicon")
_AUTH_SKIP_EXTENSIONS = frozenset((".js", ".css", ".html", ".ico", ".svg", ".png",
                                   ".woff", ".woff2", ".ttf", ".map"))


class TokenAuthMiddleware:
    """ASGI middleware — optional Bearer token auth.

    No-op if OAIO_API_TOKEN is unset.  Static assets and root path always pass.
    WebSocket auth uses ?token=<token> query param.
    """
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if _API_TOKEN is None:
            await self.app(scope, receive, send)
            return
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # Allow root (frontend)
        if path == "/":
            await self.app(scope, receive, send)
            return
        # Allow static assets by prefix
        if any(path.startswith(p) for p in _AUTH_SKIP_PREFIXES):
            await self.app(scope, receive, send)
            return
        # Allow static assets by file extension
        last_seg = path.rsplit("/", 1)[-1]
        if "." in last_seg:
            ext = "." + last_seg.rsplit(".", 1)[-1].lower()
            if ext in _AUTH_SKIP_EXTENSIONS:
                await self.app(scope, receive, send)
                return

        # WebSocket: token in query string
        if scope["type"] == "websocket":
            qs = scope.get("query_string", b"").decode("utf-8", errors="replace")
            params = parse_qs(qs)
            token = params.get("token", [None])[0]
            if token != _API_TOKEN:
                await send({"type": "websocket.close", "code": 4001})
                return
            await self.app(scope, receive, send)
            return

        # HTTP: Authorization: Bearer <token>
        headers = dict(scope.get("headers", []))
        auth_val = headers.get(b"authorization", b"").decode("utf-8", errors="replace")
        if auth_val == f"Bearer {_API_TOKEN}":
            await self.app(scope, receive, send)
            return

        resp = JSONResponse({"error": "unauthorized"}, status_code=401)
        await resp(scope, receive, send)


class _NoCacheStatic(StaticFiles):
    """StaticFiles that disables browser caching — keeps frontend changes instant."""
    async def get_response(self, path: str, scope):
        resp = await super().get_response(path, scope)
        if isinstance(resp, FileResponse):
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return resp

# ── Request monitor (middleware data) ──────────────────────────────────────────
_req_log: deque = deque(maxlen=500)
_req_stats = {"total": 0, "errors": 0, "latency_sum": 0.0, "endpoints": {}}
_monitor_ws_clients: list = []

# Paths to skip logging (static assets + the monitor endpoints themselves)
_SKIP_PREFIXES = ("/api/monitor/", "/ext/", "/style.css", "/app.js", "/litegraph",
                  "/panels/", "/nodes/", "/extensions-loader", "/favicon")


class RequestLogMiddleware:
    """ASGI middleware — logs every API request with timing."""
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if any(path.startswith(p) for p in _SKIP_PREFIXES) or "." in path.split("/")[-1]:
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "WS" if scope["type"] == "websocket" else "?")
        t0 = time.monotonic()
        status_code = 0

        async def send_wrapper(message):
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = message.get("status", 0)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            latency_ms = round((time.monotonic() - t0) * 1000, 1)
            entry = {
                "ts": datetime.now().strftime("%H:%M:%S"),
                "method": method,
                "path": path,
                "status": status_code,
                "latency_ms": latency_ms,
            }
            _req_log.append(entry)
            _req_stats["total"] += 1
            _req_stats["latency_sum"] += latency_ms
            if status_code >= 400:
                _req_stats["errors"] += 1
            _req_stats["endpoints"][path] = _req_stats["endpoints"].get(path, 0) + 1

            # Push to all monitor WS clients
            for ws in list(_monitor_ws_clients):
                try:
                    await ws.send_json(entry)
                except Exception:
                    try:
                        _monitor_ws_clients.remove(ws)
                    except ValueError:
                        pass


import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.vram import get_vram_usage, get_gpu_utilization
from core.docker_control import get_status, start, stop, get_logs, all_status, apply_resource_limits, remove_resource_limits, discover_unregistered, set_restart_policy
from core.paths import get_all_paths, repoint, get_storage_stats, heal_dangling, discover_workflows, export_workflow
from core.resources import projected_vram, check_alerts, get_system_accounting
from core.enforcer import enforcement_loop, active_modes as _active_modes, kill_log as _kill_log, register_manual_stop
import core.enforcer as _enforcer
from core import ram_tier
from core.extensions import load_all as _load_extensions, list_all as _list_extensions, \
    set_enabled as _ext_set_enabled, EXTENSIONS_DIR

OLLAMA_URL        = os.environ.get("OLLAMA_URL",        "http://localhost:11434")
COMFYUI_USER_PATH = Path(os.environ.get("COMFYUI_USER_PATH", str(Path.home() / "ComfyUI" / "user")))
RVC_GRADIO        = os.environ.get("RVC_GRADIO", "http://rvc:7865")

# Benchmark history — rolling 5-minute window at 1Hz
_bench_history: deque = deque(maxlen=300)


async def _get_ollama_loaded() -> list[dict]:
    """Return list of models currently loaded in Ollama VRAM."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{OLLAMA_URL}/api/ps")
            return r.json().get("models", [])
    except Exception:
        return []


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
    _atomic_write(ACTIVE_MODES_FILE, json.dumps(list(_active_modes)))


def _profiles_cfg() -> dict:
    if PROFILES_CFG_FILE.exists():
        return json.loads(PROFILES_CFG_FILE.read_text())
    return {"active": None, "profiles": {}}


def _save_profiles(cfg: dict):
    _atomic_write(PROFILES_CFG_FILE, json.dumps(cfg, indent=2))


def _apply_profile(profile: dict, profile_name: str):
    """Apply a hardware profile: set VRAM ceiling + Docker cgroup limits."""
    _enforcer.vram_virtual_ceiling_gb = profile.get("vram_gb") or None

    services = _services_cfg()
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

    # Proportional RAM per service
    total_svc_ram = sum(s.get("ram_est_gb", 1.0) for _, s, _ in running) or 1.0
    cpu_per = max(1, profile_cpu // len(running)) if profile_cpu and running else 0

    for svc_name, svc, ctr in running:
        svc_ram = svc.get("ram_est_gb", 1.0)
        mem_gb = round(max(0.25, (svc_ram / total_svc_ram) * profile_ram), 2) if profile_ram else 0
        apply_resource_limits(ctr, mem_gb, cpu_per)


def _deactivate_profile():
    """Clear VRAM ceiling + remove all Docker cgroup limits."""
    _enforcer.vram_virtual_ceiling_gb = None

    services = _services_cfg()
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Restore active_modes from last session
    if ACTIVE_MODES_FILE.exists():
        try:
            for m in json.loads(ACTIVE_MODES_FILE.read_text()):
                _active_modes.add(m)
        except Exception:
            pass

    # Restore active profile from last session
    try:
        pcfg = _profiles_cfg()
        active_name = pcfg.get("active")
        if active_name and active_name in pcfg.get("profiles", {}):
            _apply_profile(pcfg["profiles"][active_name], active_name)
    except Exception:
        pass

    # Heal dangling symlinks (create missing target dirs so Docker mounts don't fail)
    try:
        paths_cfg = json.loads(PATHS_CFG_FILE.read_text())
        healed = heal_dangling(paths_cfg)
        if healed:
            print(f"[oAIo] Healed {len(healed)} dangling symlink targets: {healed}")
    except Exception:
        pass

    # Enable enforcement on startup
    _enforcer.enforcer_enabled = True

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
    allow_origins=["http://localhost:9000", "http://127.0.0.1:9000", "http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)
class SecurityHeadersMiddleware:
    """ASGI middleware — sets security headers on every HTTP response."""
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"content-security-policy",
                    b"default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' ws: wss:; frame-ancestors 'none'"))
                headers.append((b"x-frame-options", b"DENY"))
                headers.append((b"x-content-type-options", b"nosniff"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestLogMiddleware)
if _API_TOKEN:
    app.add_middleware(TokenAuthMiddleware)

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"
TEMPLATES_DIR     = Path(__file__).parent.parent.parent / "templates"
PATHS_CFG_FILE    = CONFIG_DIR / "paths.json"
ROUTING_CFG_FILE  = CONFIG_DIR / "routing.json"
MODES_CFG_FILE    = CONFIG_DIR / "modes.json"
NODES_CFG_FILE    = CONFIG_DIR / "nodes.json"
SERVICES_CFG_FILE = CONFIG_DIR / "services.json"
SCANS_CFG_FILE    = CONFIG_DIR / "scans.json"
SERVICE_PORTS_FILE = CONFIG_DIR / "service_ports.json"
ACTIVE_MODES_FILE = CONFIG_DIR / "active_modes.json"
PROFILES_CFG_FILE = CONFIG_DIR / "profiles.json"

def _atomic_write(path: Path, data: str):
    """Write JSON atomically via temp file + rename to prevent corruption."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(data)
    tmp.replace(path)


# Always read fresh — never cache; services/modes are mutable at runtime
def _services_cfg() -> dict:
    return json.loads(SERVICES_CFG_FILE.read_text())["services"]

def _modes() -> dict:
    return json.loads(MODES_CFG_FILE.read_text())["modes"]

# Snapshot of modes — updated on every write so reset restores last-saved state
_MODES_SNAPSHOT: dict = copy.deepcopy(json.loads(MODES_CFG_FILE.read_text())["modes"])


def _write_modes(cfg: dict):
    """Write modes config and update the reset snapshot."""
    global _MODES_SNAPSHOT
    _atomic_write(MODES_CFG_FILE, json.dumps(cfg, indent=2))
    _MODES_SNAPSHOT = copy.deepcopy(cfg["modes"])

# ── Config-file lock (prevents read-modify-write races) ──────────────────────
_config_lock = asyncio.Lock()


@app.get("/system/status", tags=["System"])
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
        "services":     all_status(_services_cfg()),
        "alerts":       check_alerts(),
    }


@app.get("/vram", tags=["System"])
def vram_status():
    return get_vram_usage()


@app.get("/services", tags=["Services"])
def list_services():
    return _services_cfg()


@app.post("/services/{name}/start", tags=["Services"])
def start_service(name: str):
    services = _services_cfg()
    if name not in services:
        return {"error": f"Unknown service: {name}"}
    return start(services[name]["container"])


@app.post("/services/{name}/stop", tags=["Services"])
def stop_service(name: str):
    services = _services_cfg()
    if name not in services:
        return {"error": f"Unknown service: {name}"}
    svc = services[name]
    ctr = svc["container"]
    register_manual_stop(ctr, name, svc.get("priority", 3))
    threading.Thread(target=stop, args=(ctr,), daemon=True).start()
    return {"name": ctr, "action": "stopping", "ok": True}


@app.get("/services/{name}/status", tags=["Services"])
def service_status(name: str):
    services = _services_cfg()
    if name not in services:
        return {"error": f"Unknown service: {name}"}
    return get_status(services[name]["container"])


@app.get("/services/{name}/logs", tags=["Services"])
def service_logs(name: str, lines: int = 50):
    services = _services_cfg()
    if name not in services:
        return {"error": f"Unknown service: {name}"}
    return {"logs": get_logs(services[name]["container"], lines)}


# ── API Scanner ───────────────────────────────────────────────────────────────

def _load_scans() -> dict:
    """Load cached scan results from disk."""
    if SCANS_CFG_FILE.exists():
        try:
            return json.loads(SCANS_CFG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


# I/O suggestion mapping per capability
_IO_MAP = {
    "chat":             {"inputs": [["text", "string"]], "outputs": [["response", "string"]]},
    "tts":              {"inputs": [["text", "string"]], "outputs": [["audio", "audio"]]},
    "embeddings":       {"inputs": [["text", "string"]], "outputs": [["embedding", "array"]]},
    "voice_conversion": {"inputs": [["audio", "audio"]], "outputs": [["audio", "audio"]]},
    "image_gen":        {"inputs": [["prompt", "string"]], "outputs": [["image", "image"]]},
    "text_gen":         {"inputs": [["text", "string"]], "outputs": [["response", "string"]]},
    "gradio_app":       {"inputs": [], "outputs": []},
}


def _derive_capabilities(endpoints: list[dict]) -> list[str]:
    """Derive capability tags from discovered endpoint paths."""
    caps = set()
    paths = {ep["path"].lower() for ep in endpoints}
    for p in paths:
        if "/v1/chat/completions" in p or "/api/chat" in p:
            caps.add("chat")
        if "/v1/audio/speech" in p:
            caps.add("tts")
        if "/v1/embeddings" in p or "/api/embed" in p:
            caps.add("embeddings")
        if "/convert" in p:
            caps.add("voice_conversion")
        if "/prompt" in p:
            caps.add("image_gen")
        if "/api/generate" in p:
            caps.add("text_gen")
    return sorted(caps)


def _derive_io(capabilities: list[str], gradio_endpoints: list[dict]) -> dict:
    """Build suggested I/O from capabilities + Gradio param types."""
    inputs_set: list[list[str]] = []
    outputs_set: list[list[str]] = []
    seen_in: set[tuple] = set()
    seen_out: set[tuple] = set()
    for cap in capabilities:
        io = _IO_MAP.get(cap)
        if io:
            for pair in io["inputs"]:
                key = tuple(pair)
                if key not in seen_in:
                    seen_in.add(key)
                    inputs_set.append(pair)
            for pair in io["outputs"]:
                key = tuple(pair)
                if key not in seen_out:
                    seen_out.add(key)
                    outputs_set.append(pair)
    # Derive from Gradio param info if available
    for ep in gradio_endpoints:
        for param in ep.get("parameters", []):
            pname = param.get("name", "input")
            ptype = param.get("type", "string")
            key = (pname, ptype)
            if key not in seen_in:
                seen_in.add(key)
                inputs_set.append([pname, ptype])
    return {"inputs": inputs_set, "outputs": outputs_set}


async def _probe_get(client: httpx.AsyncClient, url: str) -> tuple[bool, int, dict | str | None]:
    """Probe a URL with GET. Returns (reachable, status_code, parsed_body_or_None)."""
    try:
        r = await client.get(url)
        try:
            body = r.json()
        except Exception:
            body = r.text[:500] if r.text else None
        return True, r.status_code, body
    except Exception:
        return False, 0, None


async def _probe_head(client: httpx.AsyncClient, url: str) -> tuple[bool, int]:
    """Probe a URL with HEAD. Returns (reachable, status_code)."""
    try:
        r = await client.head(url)
        return True, r.status_code
    except Exception:
        return False, 0


async def _probe_post_empty(client: httpx.AsyncClient, url: str) -> tuple[bool, int]:
    """Probe a URL with empty POST to check if it responds. Returns (reachable, status_code)."""
    try:
        r = await client.post(url, json={})
        return True, r.status_code
    except Exception:
        return False, 0


async def _scan_service(base_url: str, service_name: str) -> dict:
    """Run full API scan against a service. Returns structured scan result."""
    scan_start = datetime.utcnow()
    result = {
        "service": service_name,
        "url": base_url,
        "scan_time": scan_start.isoformat() + "Z",
        "reachable": False,
        "api_type": "unknown",
        "openapi_spec": None,
        "endpoints": [],
        "capabilities": [],
        "suggested_io": {"inputs": [], "outputs": []},
    }

    async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
        # ── Liveness check ────────────────────────────────────────────────
        alive, status, _ = await _probe_get(client, f"{base_url}/")
        if not alive:
            # Try /health as fallback
            alive, status, _ = await _probe_get(client, f"{base_url}/health")
        result["reachable"] = alive
        if not alive:
            print(f"[Scanner] {service_name}: unreachable at {base_url}")
            return result

        print(f"[Scanner] {service_name}: reachable at {base_url}")
        endpoints: list[dict] = []

        # ── OpenAPI detection ─────────────────────────────────────────────
        openapi_found = False
        ok, code, body = await _probe_get(client, f"{base_url}/openapi.json")
        if ok and code == 200 and isinstance(body, dict) and "paths" in body:
            print(f"[Scanner] {service_name}: OpenAPI spec found")
            result["api_type"] = "openapi"
            result["openapi_spec"] = body
            openapi_found = True
            # Extract endpoints from OpenAPI
            for path, methods in body.get("paths", {}).items():
                for method, detail in methods.items():
                    if method.lower() in ("get", "post", "put", "patch", "delete", "head", "options"):
                        ep = {
                            "method": method.upper(),
                            "path": path,
                            "summary": detail.get("summary", ""),
                            "parameters": [],
                            "tags": detail.get("tags", []),
                            "source": "openapi",
                        }
                        # Collect path/query parameters
                        for p in detail.get("parameters", []):
                            ep["parameters"].append({
                                "name": p.get("name", ""),
                                "in": p.get("in", ""),
                                "type": p.get("schema", {}).get("type", "string"),
                                "required": p.get("required", False),
                            })
                        # Collect request body schema fields
                        req_body = detail.get("requestBody", {})
                        content = req_body.get("content", {})
                        json_schema = content.get("application/json", {}).get("schema", {})
                        for prop_name, prop_def in json_schema.get("properties", {}).items():
                            ep["parameters"].append({
                                "name": prop_name,
                                "in": "body",
                                "type": prop_def.get("type", "string"),
                                "required": prop_name in json_schema.get("required", []),
                            })
                        endpoints.append(ep)

        # Check for Swagger UI (just note existence)
        ok_docs, code_docs, _ = await _probe_get(client, f"{base_url}/docs")
        if ok_docs and code_docs == 200:
            print(f"[Scanner] {service_name}: Swagger UI (/docs) available")

        # ── Gradio detection ──────────────────────────────────────────────
        gradio_found = False
        gradio_endpoints: list[dict] = []

        # Gradio v6
        ok_g6, code_g6, body_g6 = await _probe_get(client, f"{base_url}/gradio_api/info")
        if ok_g6 and code_g6 == 200 and isinstance(body_g6, dict):
            print(f"[Scanner] {service_name}: Gradio v6 API detected")
            gradio_found = True
            if not openapi_found:
                result["api_type"] = "gradio_v6"
            # Parse Gradio v6 endpoints
            for ep_name, ep_info in body_g6.get("named_endpoints", {}).items():
                ep = {
                    "method": "POST",
                    "path": f"/gradio_api/call{ep_name}",
                    "summary": ep_info.get("description", f"Gradio endpoint {ep_name}"),
                    "parameters": [],
                    "tags": ["gradio"],
                    "source": "gradio_v6",
                }
                for param in ep_info.get("parameters", []):
                    ep["parameters"].append({
                        "name": param.get("parameter_name", param.get("label", "")),
                        "type": param.get("python_type", {}).get("type", "string") if isinstance(param.get("python_type"), dict) else str(param.get("python_type", "string")),
                        "component": param.get("component", ""),
                    })
                gradio_endpoints.append(ep)
                endpoints.append(ep)

        # Gradio v4 — /info
        if not gradio_found:
            ok_g4, code_g4, body_g4 = await _probe_get(client, f"{base_url}/info")
            if ok_g4 and code_g4 == 200 and isinstance(body_g4, dict) and ("named_endpoints" in body_g4 or "unnamed_endpoints" in body_g4):
                print(f"[Scanner] {service_name}: Gradio v4 API detected (/info)")
                gradio_found = True
                if not openapi_found:
                    result["api_type"] = "gradio_v4"
                for ep_name, ep_info in body_g4.get("named_endpoints", {}).items():
                    ep = {
                        "method": "POST",
                        "path": f"/run{ep_name}" if ep_name.startswith("/") else f"/run/{ep_name}",
                        "summary": ep_info.get("description", f"Gradio endpoint {ep_name}"),
                        "parameters": [],
                        "tags": ["gradio"],
                        "source": "gradio_v4",
                    }
                    for param in ep_info.get("parameters", []):
                        ep["parameters"].append({
                            "name": param.get("parameter_name", param.get("label", "")),
                            "type": param.get("python_type", {}).get("type", "string") if isinstance(param.get("python_type"), dict) else str(param.get("python_type", "string")),
                            "component": param.get("component", ""),
                        })
                    gradio_endpoints.append(ep)
                    endpoints.append(ep)

            # Gradio v4 — /api/
            if not gradio_found:
                ok_g4b, code_g4b, body_g4b = await _probe_get(client, f"{base_url}/api/")
                if ok_g4b and code_g4b == 200 and isinstance(body_g4b, dict) and ("named_endpoints" in body_g4b or "unnamed_endpoints" in body_g4b):
                    print(f"[Scanner] {service_name}: Gradio v4 API detected (/api/)")
                    gradio_found = True
                    if not openapi_found:
                        result["api_type"] = "gradio_v4"
                    for ep_name, ep_info in body_g4b.get("named_endpoints", {}).items():
                        ep = {
                            "method": "POST",
                            "path": f"/run{ep_name}" if ep_name.startswith("/") else f"/run/{ep_name}",
                            "summary": ep_info.get("description", f"Gradio endpoint {ep_name}"),
                            "parameters": [],
                            "tags": ["gradio"],
                            "source": "gradio_v4",
                        }
                        for param in ep_info.get("parameters", []):
                            ep["parameters"].append({
                                "name": param.get("parameter_name", param.get("label", "")),
                                "type": param.get("python_type", {}).get("type", "string") if isinstance(param.get("python_type"), dict) else str(param.get("python_type", "string")),
                                "component": param.get("component", ""),
                            })
                        gradio_endpoints.append(ep)
                        endpoints.append(ep)

        # ── OpenAI-compatible detection (skip if OpenAPI already found full spec)
        openai_compat = False
        if not openapi_found:
            ok_models, code_models, body_models = await _probe_get(client, f"{base_url}/v1/models")
            if ok_models and code_models == 200 and isinstance(body_models, dict) and body_models.get("object") == "list":
                print(f"[Scanner] {service_name}: OpenAI-compatible API detected")
                openai_compat = True
                if not gradio_found:
                    result["api_type"] = "openai_compat"

                # Probe known OpenAI endpoints — only add if they return 2xx or 422 (valid schema error)
                openai_probes = [
                    ("/v1/chat/completions", "POST", "Chat completions"),
                    ("/v1/audio/speech", "POST", "Text-to-speech"),
                    ("/v1/embeddings", "POST", "Embeddings"),
                    ("/v1/completions", "POST", "Text completions"),
                ]
                for path, method, summary in openai_probes:
                    # Skip if already discovered
                    if any(ep["path"] == path for ep in endpoints):
                        continue
                    reachable, scode = await _probe_post_empty(client, f"{base_url}{path}")
                    if reachable and (scode < 300 or scode in (400, 422)):
                        print(f"[Scanner] {service_name}:   {path} responds ({scode})")
                        endpoints.append({
                            "method": method,
                            "path": path,
                            "summary": summary,
                            "parameters": [],
                            "tags": ["openai"],
                            "source": "openai_compat",
                        })
                # Also add /v1/models as GET
                if not any(ep["path"] == "/v1/models" for ep in endpoints):
                    endpoints.append({
                        "method": "GET",
                        "path": "/v1/models",
                        "summary": "List models",
                        "parameters": [],
                        "tags": ["openai"],
                        "source": "openai_compat",
                    })

        # ── Generic probing ───────────────────────────────────────────────
        generic_probes = [
            ("/health", "Health check"),
            ("/api/version", "API version"),
            ("/version", "Version"),
            ("/api/generate", "Generate"),
            ("/api/chat", "Chat"),
            ("/api/embed", "Embed"),
            ("/convert", "Convert"),
            ("/prompt", "Prompt"),
        ]
        for path, summary in generic_probes:
            # Skip if already discovered from OpenAPI or other methods
            if any(ep["path"] == path for ep in endpoints):
                continue
            reachable, gcode, _ = await _probe_get(client, f"{base_url}{path}")
            if reachable and 200 <= gcode < 300:
                print(f"[Scanner] {service_name}:   {path} responds ({gcode})")
                endpoints.append({
                    "method": "GET",
                    "path": path,
                    "summary": summary,
                    "parameters": [],
                    "tags": ["generic"],
                    "source": "probe",
                })

        # ── Finalize ──────────────────────────────────────────────────────
        result["endpoints"] = endpoints
        capabilities = _derive_capabilities(endpoints)
        if gradio_found and "gradio_app" not in capabilities:
            capabilities.append("gradio_app")
            capabilities.sort()
        result["capabilities"] = capabilities
        result["suggested_io"] = _derive_io(capabilities, gradio_endpoints)

    elapsed = (datetime.utcnow() - scan_start).total_seconds()
    print(f"[Scanner] {service_name}: scan complete in {elapsed:.1f}s — "
          f"type={result['api_type']}, {len(endpoints)} endpoints, caps={capabilities}")
    return result


@app.post("/services/{name}/scan", tags=["Services"])
async def scan_service(name: str):
    """Probe a registered service's API and return a structured capability map."""
    if not _CONTAINER_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"Invalid service name: must match {_CONTAINER_NAME_RE.pattern}")
    services = _services_cfg()
    if name not in services:
        raise HTTPException(status_code=404, detail=f"Service '{name}' not found")

    svc = services[name]
    container = svc.get("container", name)
    port = svc.get("port", 0)
    if not port:
        raise HTTPException(status_code=400, detail=f"Service '{name}' has no port configured")

    base_url = f"http://{container}:{port}"
    print(f"[Scanner] Starting scan of {name} at {base_url}")

    scan_result = await _scan_service(base_url, name)

    # Persist to scans.json
    async with _config_lock:
        scans = _load_scans()
        scans[name] = scan_result
        _atomic_write(SCANS_CFG_FILE, json.dumps(scans, indent=2))

    return scan_result


@app.get("/services/{name}/scan", tags=["Services"])
async def get_scan_result(name: str):
    """Retrieve the last cached scan result for a service."""
    if not _CONTAINER_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"Invalid service name: must match {_CONTAINER_NAME_RE.pattern}")
    services = _services_cfg()
    if name not in services:
        raise HTTPException(status_code=404, detail=f"Service '{name}' not found")
    scans = _load_scans()
    if name not in scans:
        raise HTTPException(status_code=404, detail=f"No scan result for '{name}' — run POST /services/{name}/scan first")
    return scans[name]


# ── Hardcoded fallback port definitions (mirrors SERVICE_PORTS in services.js)
_DEFAULT_SERVICE_PORTS = {
    "ollama":       {"in": [["llm_req", "request"]],     "out": [["llm_resp", "response"]]},
    "open-webui":   {"in": [["llm_resp", "response"], ["tts_audio", "audio"], ["image", "image"]],
                     "out": [["llm_req", "request"], ["tts_req", "request"], ["imggen_req", "request"]]},
    "kokoro-tts":   {"in": [["tts_req", "request"]],     "out": [["raw_audio", "audio"]]},
    "rvc":          {"in": [["raw_audio", "audio"], ["clone_audio", "audio"]],
                     "out": [["tts_audio", "audio"]]},
    "f5-tts":       {"in": [["clone_req", "request"]],   "out": [["clone_audio", "audio"]]},
    "comfyui":      {"in": [["imggen_req", "request"]],   "out": [["image", "image"]]},
    "styletts2":    {"in": [["tts_req", "request"]],      "out": [["raw_audio", "audio"]]},
}

# ── Capability -> port mapping for autowire ──────────────────────────────────
_CAPABILITY_PORTS = {
    "chat":             {"in": [["prompt", "string"]],  "out": [["response", "string"]]},
    "tts":              {"in": [["text", "string"]],    "out": [["audio", "audio"]]},
    "embeddings":       {"in": [["text", "string"]],    "out": [["embedding", "array"]]},
    "voice_conversion": {"in": [["audio", "audio"]],    "out": [["audio", "audio"]]},
    "image_gen":        {"in": [["prompt", "string"]],  "out": [["image", "image"]]},
    "text_gen":         {"in": [["prompt", "string"]],  "out": [["text", "string"]]},
}

# Gradio type -> LiteGraph type mapping
_GRADIO_TYPE_MAP = {
    "string":    "string",
    "str":       "string",
    "text":      "string",
    "textbox":   "string",
    "number":    "number",
    "int":       "number",
    "float":     "number",
    "slider":    "number",
    "audio":     "audio",
    "image":     "image",
    "video":     "video",
    "file":      "file",
    "checkbox":  "boolean",
    "bool":      "boolean",
    "dropdown":  "string",
    "radio":     "string",
    "json":      "object",
    "dataframe": "array",
}


def _gradio_type_to_litegraph(gradio_type: str) -> str:
    """Map a Gradio component/type name to a LiteGraph type."""
    return _GRADIO_TYPE_MAP.get(gradio_type.lower().strip(), "any")


def _generate_ports_from_scan(scan: dict) -> dict:
    """Generate I/O port definitions from a scan result's capabilities and endpoints."""
    capabilities = scan.get("capabilities", [])
    endpoints = scan.get("endpoints", [])

    in_ports = []
    out_ports = []
    seen_in = set()
    seen_out = set()

    # Process known capabilities
    for cap in capabilities:
        cap_name = cap if isinstance(cap, str) else cap.get("type", "")
        if cap_name in _CAPABILITY_PORTS:
            for port in _CAPABILITY_PORTS[cap_name]["in"]:
                if port[0] not in seen_in:
                    in_ports.append(port)
                    seen_in.add(port[0])
            for port in _CAPABILITY_PORTS[cap_name]["out"]:
                if port[0] not in seen_out:
                    out_ports.append(port)
                    seen_out.add(port[0])

    # Process gradio_app capability — derive ports from endpoint parameters
    for cap in capabilities:
        cap_name = cap if isinstance(cap, str) else cap.get("type", "")
        if cap_name == "gradio_app":
            for ep in endpoints:
                params = ep.get("parameters", [])
                returns = ep.get("returns", [])
                for p in params:
                    pname = p.get("name", "input")
                    ptype = _gradio_type_to_litegraph(p.get("type", "any"))
                    if pname not in seen_in:
                        in_ports.append([pname, ptype])
                        seen_in.add(pname)
                for r in returns:
                    rname = r.get("name", "output")
                    rtype = _gradio_type_to_litegraph(r.get("type", "any"))
                    if rname not in seen_out:
                        out_ports.append([rname, rtype])
                        seen_out.add(rname)

    # Fallback: if no ports generated, use generic input/output
    if not in_ports:
        in_ports = [["input", "any"]]
    if not out_ports:
        out_ports = [["output", "any"]]

    return {"in": in_ports, "out": out_ports}


def _read_service_ports() -> dict:
    """Read service_ports.json; return empty dict if missing."""
    if SERVICE_PORTS_FILE.exists():
        return json.loads(SERVICE_PORTS_FILE.read_text())
    return {}


@app.post("/services/{name}/autowire", tags=["Services"])
async def autowire_service(name: str):
    """Generate LiteGraph I/O port definitions from the last scan result."""
    if not _CONTAINER_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"Invalid service name: must match {_CONTAINER_NAME_RE.pattern}")

    services = _services_cfg()
    if name not in services:
        raise HTTPException(status_code=404, detail=f"Service '{name}' not registered")

    # Read last scan result
    if not SCANS_CFG_FILE.exists():
        raise HTTPException(status_code=404, detail=f"No scans found. Run POST /services/{name}/scan first.")

    scans = json.loads(SCANS_CFG_FILE.read_text())
    if name not in scans:
        raise HTTPException(status_code=404, detail=f"No scan result for '{name}'. Run POST /services/{name}/scan first.")

    scan = scans[name]

    # Warn if scan is older than 24 hours
    warning = None
    scanned_at = scan.get("scanned_at", "")
    if scanned_at:
        try:
            scan_time = datetime.fromisoformat(scanned_at)
            age_hours = (datetime.now() - scan_time).total_seconds() / 3600
            if age_hours > 24:
                warning = f"Scan is {age_hours:.1f} hours old (>24h). Consider re-scanning for fresh results."
        except (ValueError, TypeError):
            pass

    # Generate ports from scan capabilities
    ports = _generate_ports_from_scan(scan)

    # Build the entry
    entry = {
        "in":  ports["in"],
        "out": ports["out"],
        "auto_wired": True,
        "wired_at": datetime.now().isoformat(),
        "source_capabilities": [
            (c if isinstance(c, str) else c.get("type", ""))
            for c in scan.get("capabilities", [])
        ],
    }

    # Save to service_ports.json
    async with _config_lock:
        all_ports = _read_service_ports()
        all_ports[name] = entry
        _atomic_write(SERVICE_PORTS_FILE, json.dumps(all_ports, indent=2))

    result = {"service": name, "ports": entry}
    if warning:
        result["warning"] = warning
    return result


@app.get("/services/{name}/ports", tags=["Services"])
def get_service_ports(name: str):
    """Return current port definitions — auto-wired if available, else hardcoded defaults."""
    if not _CONTAINER_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"Invalid service name: must match {_CONTAINER_NAME_RE.pattern}")

    services = _services_cfg()
    if name not in services:
        raise HTTPException(status_code=404, detail=f"Service '{name}' not registered")

    # Check auto-wired ports first
    all_ports = _read_service_ports()
    if name in all_ports:
        return {"service": name, "source": "auto_wired", "ports": all_ports[name]}

    # Fall back to hardcoded defaults
    if name in _DEFAULT_SERVICE_PORTS:
        return {"service": name, "source": "default", "ports": {
            "in":  _DEFAULT_SERVICE_PORTS[name]["in"],
            "out": _DEFAULT_SERVICE_PORTS[name]["out"],
            "auto_wired": False,
        }}

    # Unknown service with no ports defined
    return {"service": name, "source": "generic", "ports": {
        "in":  [["input", "any"]],
        "out": [["output", "any"]],
        "auto_wired": False,
    }}


@app.delete("/services/{name}/autowire", tags=["Services"])
async def delete_autowire(name: str):
    """Remove auto-wired port definitions, reverting to defaults."""
    if not _CONTAINER_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"Invalid service name: must match {_CONTAINER_NAME_RE.pattern}")

    services = _services_cfg()
    if name not in services:
        raise HTTPException(status_code=404, detail=f"Service '{name}' not registered")

    async with _config_lock:
        all_ports = _read_service_ports()
        if name not in all_ports:
            return {"service": name, "deleted": False, "detail": "No auto-wired ports to remove"}
        del all_ports[name]
        _atomic_write(SERVICE_PORTS_FILE, json.dumps(all_ports, indent=2))

    return {"service": name, "deleted": True, "reverted_to": "default"}


@app.get("/modes", tags=["Modes"])
def list_modes():
    return _modes()


@app.get("/modes/{name}/check", tags=["Modes"])
def check_mode(name: str):
    """Pre-flight VRAM check — call before activating to see if it fits."""
    modes = _modes()
    if name not in modes:
        return {"error": f"Unknown mode: {name}"}
    return projected_vram(modes[name], _services_cfg())


@app.post("/modes/{name}/activate", tags=["Modes"])
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

    new_svc_set = set(mode["services"])

    # ── Displacement: stop old-mode services not needed by the new mode ──────
    old_svc_set: set[str] = set()
    for active_name in list(_active_modes):
        old_mode = modes.get(active_name)
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

    # ── Start new mode's services ────────────────────────────────────────────
    for svc_name in mode["services"]:
        svc = services.get(svc_name)
        if not svc:
            continue
        ctr = svc.get("container")
        if not ctr:
            continue
        results.append(start(ctr))

    _active_modes.clear()
    _active_modes.add(name)
    _persist_active_modes()

    return {
        "mode":       name,
        "results":    results,
        "projection": projection,
        "warning":    projection["warning"],
    }


@app.post("/modes/{name}/deactivate", tags=["Modes"])
def deactivate_mode(name: str):
    _active_modes.discard(name)
    _persist_active_modes()
    return {"deactivated": name, "active_modes": list(_active_modes)}


@app.post("/emergency/kill", tags=["Enforcement"])
def emergency_kill():
    """Stop all managed containers immediately, clear all active modes."""
    services = _services_cfg()
    _active_modes.clear()
    _persist_active_modes()
    results = []
    for svc_name, svc in services.items():
        ctr = svc.get("container")
        if not ctr:
            continue
        register_manual_stop(ctr, svc_name, svc.get("priority", 3))
        threading.Thread(target=stop, args=(ctr,), daemon=True).start()
        results.append(ctr)
    return {"killed": results, "active_modes": []}


@app.get("/modes/{name}/allocations", tags=["Modes"])
def get_allocations(name: str):
    modes = _modes()
    if name not in modes:
        return {"error": f"Unknown mode: {name}"}
    return {
        "allocations":   modes[name].get("allocations", {}),
        "vram_budget_gb": modes[name].get("vram_budget_gb", 0),
    }


@app.post("/modes/{name}/allocations/{service}", tags=["Modes"])
async def set_allocation(name: str, service: str, body: dict):
    """body: {"gb": 7.5} — update one service's VRAM allocation within a mode."""
    async with _config_lock:
        cfg = json.loads(MODES_CFG_FILE.read_text())
        if name not in cfg["modes"]:
            return {"error": f"Unknown mode: {name}"}
        gb = body.get("gb")
        if gb is None:
            return {"error": "gb is required"}
        cfg["modes"][name].setdefault("allocations", {})[service] = round(float(gb), 1)
        _write_modes(cfg)
        return projected_vram(cfg["modes"][name], _services_cfg())


@app.post("/modes/{name}/budget", tags=["Modes"])
async def set_budget(name: str, body: dict):
    """body: {"gb": 11} — update a mode's VRAM ceiling."""
    async with _config_lock:
        cfg = json.loads(MODES_CFG_FILE.read_text())
        if name not in cfg["modes"]:
            return {"error": f"Unknown mode: {name}"}
        gb = body.get("gb")
        if gb is None:
            return {"error": "gb is required"}
        cfg["modes"][name]["vram_budget_gb"] = round(float(gb), 1)
        _write_modes(cfg)
        return projected_vram(cfg["modes"][name], _services_cfg())


@app.post("/modes", tags=["Modes"])
async def create_mode(body: dict):
    """Create a new mode. body: {name, description?, services[], vram_budget_gb?}"""
    name = body.get("name", "").strip()
    if not name:
        return {"error": "name is required"}
    key = name.lower().replace(" ", "-")
    async with _config_lock:
        cfg = json.loads(MODES_CFG_FILE.read_text())
        if key in cfg["modes"]:
            return {"error": f"Mode '{key}' already exists"}
        max_id = max((m.get("id", 0) for m in cfg["modes"].values()), default=0)
        services = body.get("services", [])
        known = set(_services_cfg().keys())
        unknown = set(services) - known
        if unknown:
            return {"error": f"Unknown services: {', '.join(sorted(unknown))}"}
        budget = round(float(body.get("vram_budget_gb", 10)), 1)
        cfg["modes"][key] = {
            "id": max_id + 1,
            "name": name,
            "description": body.get("description", ""),
            "services": services,
            "vram_budget_gb": budget,
            "allocations": {s: 0 for s in services},
            "boot_image": None,
        }
        _write_modes(cfg)
        return {"created": key, "mode": cfg["modes"][key]}


@app.delete("/modes/{name}", tags=["Modes"])
async def delete_mode(name: str):
    """Remove a mode from modes.json."""
    async with _config_lock:
        cfg = json.loads(MODES_CFG_FILE.read_text())
        if name not in cfg["modes"]:
            return {"error": f"Unknown mode: {name}"}
        _active_modes.discard(name)
        _persist_active_modes()
        del cfg["modes"][name]
        _write_modes(cfg)
        return {"deleted": name}


@app.patch("/modes/{name}", tags=["Modes"])
async def patch_mode(name: str, body: dict):
    """Update mode fields: name, description, services, vram_budget_gb."""
    async with _config_lock:
        cfg = json.loads(MODES_CFG_FILE.read_text())
        if name not in cfg["modes"]:
            return {"error": f"Unknown mode: {name}"}
        mode = cfg["modes"][name]
        if "name" in body:
            mode["name"] = body["name"]
        if "description" in body:
            mode["description"] = body["description"]
        if "services" in body:
            known = set(_services_cfg().keys())
            unknown = set(body["services"]) - known
            if unknown:
                return {"error": f"Unknown services: {', '.join(sorted(unknown))}"}
            mode["services"] = body["services"]
            # ensure allocations has entries for all services
            for s in body["services"]:
                mode.setdefault("allocations", {}).setdefault(s, 0)
        if "vram_budget_gb" in body:
            mode["vram_budget_gb"] = round(float(body["vram_budget_gb"]), 1)
        _write_modes(cfg)
        return {"updated": name, "mode": mode}


@app.get("/templates", tags=["Templates"])
def list_templates():
    if not TEMPLATES_DIR.exists():
        return []
    return [f.stem for f in TEMPLATES_DIR.glob("*.json")]


@app.post("/templates/save", tags=["Templates"])
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
            for svc, cfg in _services_cfg().items()
            if cfg.get("container")
        }
    }
    _atomic_write(path, json.dumps(template, indent=2))
    return {"saved": name}


@app.post("/templates/{name}/load", tags=["Templates"])
def load_template(name: str):
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise HTTPException(status_code=400, detail="Invalid template name: only alphanumeric, dash, underscore allowed")
    path = (TEMPLATES_DIR / f"{name}.json").resolve()
    if not str(path).startswith(str(TEMPLATES_DIR.resolve())):
        return {"error": "Invalid template name"}
    if not path.exists():
        return {"error": f"Template not found: {name}"}
    template = json.loads(path.read_text())
    services = _services_cfg()
    results  = []
    for svc_name, state in template["services"].items():
        if svc_name not in services:
            continue
        if state.get("status") == "running":
            results.append(start(services[svc_name]["container"]))
        else:
            results.append(stop(services[svc_name]["container"]))
    return {"loaded": name, "results": results}


# ── Capability endpoints (Tier 3 sub-nodes) ──────────────────────────────────

@app.get("/services/ollama/models", tags=["Ollama"])
async def ollama_models():
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
        models = r.json().get("models", [])
        return [{"name": m["name"], "size_gb": round(m["size"] / 1e9, 1)} for m in models]
    except Exception as e:
        print(f"[oLLMo] ollama_models error: {e}")
        return {"error": "Failed to list Ollama models"}


@app.post("/services/ollama/models/{name}/load", tags=["Ollama"])
async def load_ollama_model(name: str):
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            await client.post(f"{OLLAMA_URL}/api/generate",
                json={"model": name, "prompt": "", "stream": False})
        return {"loaded": name}
    except Exception as e:
        print(f"[oLLMo] load_ollama_model error: {e}")
        return {"error": "Failed to load model"}


@app.post("/services/ollama/models/pull", tags=["Ollama"])
async def pull_ollama_model(body: dict):
    name = body.get("name", "").strip()
    if not name:
        return {"error": "name is required"}
    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            r = await client.post(f"{OLLAMA_URL}/api/pull",
                json={"name": name, "stream": False})
            data = r.json()
            if data.get("error"):
                print(f"[oLLMo] pull_ollama_model upstream error: {data['error']}")
                return {"error": "Failed to pull model"}
            return {"pulled": name, "status": data.get("status", "success")}
    except Exception as e:
        print(f"[oLLMo] pull_ollama_model error: {e}")
        return {"error": "Failed to pull model"}


@app.delete("/services/ollama/models/{name}", tags=["Ollama"])
async def delete_ollama_model(name: str):
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.delete(f"{OLLAMA_URL}/api/delete",
                json={"name": name})
            if r.status_code == 200:
                return {"deleted": name}
            print(f"[oLLMo] delete_ollama_model failed: {r.text}")
            return {"error": "Failed to delete model"}
    except Exception as e:
        print(f"[oLLMo] delete_ollama_model error: {e}")
        return {"error": "Failed to delete model"}


@app.get("/services/rvc/models", tags=["RVC"])
def rvc_models():
    try:
        client = docker_sdk.from_env()
        container = client.containers.get("rvc")
        result = container.exec_run(["find", "/rvc/assets/weights", "-name", "*.pth"])
        files = result.output.decode().strip().split("\n")
        return [{"name": Path(f).stem, "file": Path(f).name}
                for f in files if f and f.endswith(".pth")]
    except Exception as e:
        print(f"[oLLMo] rvc_models error: {e}")
        return {"error": "Failed to list RVC models"}


@app.post("/services/rvc/models/{name}/activate", tags=["RVC"])
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
        print(f"[oLLMo] activate_rvc_model error: {e}")
        return {"error": "Failed to activate RVC model"}


@app.get("/services/comfyui/workflows", tags=["ComfyUI"])
def comfyui_workflows():
    workflows_path = COMFYUI_USER_PATH / "default" / "workflows"
    if not workflows_path.exists():
        return []
    files = sorted(f for f in workflows_path.glob("*.json") if not f.stem.startswith("."))
    return [{"name": f.stem, "file": f.name} for f in files]


# ── Workflow discovery & export ───────────────────────────────────────────────

@app.get("/workflows", tags=["Workflows"])
def workflows_list():
    """Discover workflow JSON files across all known storage tiers."""
    return discover_workflows()


@app.get("/workflows/export", tags=["Workflows"])
def workflows_export(path: str):
    """Export a workflow with oAIo storage tier metadata. ?path=/mnt/oaio/workflows/my.json"""
    if not path or ".." in path:
        raise HTTPException(status_code=400, detail="Invalid path")
    result = export_workflow(path)
    if result is None:
        raise HTTPException(status_code=404, detail="Workflow not found or not readable")
    return result


# ── Paths & Routing config endpoints ─────────────────────────────────────────

@app.get("/config/paths", tags=["Config"])
def config_paths_list():
    cfg = json.loads(PATHS_CFG_FILE.read_text())
    return get_all_paths(cfg)


@app.post("/config/paths", tags=["Config"])
async def config_paths_add(body: dict):
    """body: {"name": "mymodels", "label": "My Models", "target": "/data/models", "containers": ["comfyui"]}"""
    async with _config_lock:
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
        _atomic_write(PATHS_CFG_FILE, json.dumps(cfg, indent=2))
        result = repoint(link, target)
        return {**result, "name": name, "label": label, "containers": ctrs}


@app.delete("/config/paths/{name}", tags=["Config"])
async def config_paths_delete(name: str):
    async with _config_lock:
        cfg = json.loads(PATHS_CFG_FILE.read_text())
        if name not in cfg:
            return {"error": f"Unknown path: {name}"}
        link = Path(cfg[name]["link"])
        if link.is_symlink():
            link.unlink()
        del cfg[name]
        _atomic_write(PATHS_CFG_FILE, json.dumps(cfg, indent=2))
        return {"deleted": name}


@app.post("/config/paths/{name}", tags=["Config"])
async def config_paths_set(name: str, body: dict):
    """
    body: {"target": "/new/path"} — absolute path, or:
          {"target": "ram"}       — activate RAM tier (/dev/shm/oaio-<name>)
          {"target": "default"}   — revert to default_target from config
    Switching away from "ram" automatically cleans up the /dev/shm directory.
    """
    async with _config_lock:
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


@app.get("/config/routing", tags=["Config"])
def config_routing_get():
    return json.loads(ROUTING_CFG_FILE.read_text())


@app.post("/config/routing", tags=["Config"])
def config_routing_set(body: dict):
    current = json.loads(ROUTING_CFG_FILE.read_text())
    current.update({k: v for k, v in body.items() if k in current})
    _atomic_write(ROUTING_CFG_FILE, json.dumps(current, indent=2))
    return current


@app.get("/config/nodes", tags=["Config"])
def config_nodes_get():
    return json.loads(NODES_CFG_FILE.read_text())


@app.post("/config/nodes", tags=["Config"])
def config_nodes_set(body: dict):
    cfg = json.loads(NODES_CFG_FILE.read_text())
    cfg.update(body)
    _atomic_write(NODES_CFG_FILE, json.dumps(cfg, indent=2))
    return cfg


@app.get("/config/storage/stats", tags=["Config"])
def config_storage_stats():
    return get_storage_stats()


@app.get("/config/boot", tags=["Config"])
def get_boot_config():
    """Get boot-with-system status."""
    cfg = json.loads(SERVICES_CFG_FILE.read_text())
    return {"enabled": cfg.get("boot_with_system", True)}


@app.post("/config/boot", tags=["Config"])
async def set_boot_config(body: dict):
    """Toggle boot-with-system. Sets Docker restart policies on all service containers."""
    enabled = body.get("enabled")
    if enabled is None:
        return {"error": "enabled (bool) is required"}
    enabled = bool(enabled)
    async with _config_lock:
        cfg = json.loads(SERVICES_CFG_FILE.read_text())
        cfg["boot_with_system"] = enabled
        _atomic_write(SERVICES_CFG_FILE, json.dumps(cfg, indent=2))

    # Update Docker restart policies
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


@app.get("/benchmark/history", tags=["System"])
def benchmark_history():
    return list(_bench_history)


def _build_kill_order(svcs: dict) -> list:
    """Build kill-order list from services config + live container status."""
    candidates = []
    try:
        dc = _docker_client()
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


@app.websocket("/ws")
async def ws_status(websocket: WebSocket):
    await websocket.accept()
    loop = asyncio.get_event_loop()
    try:
        while True:
            vram    = get_vram_usage()
            gpu     = get_gpu_utilization()
            ram     = psutil.virtual_memory()
            svcs    = _services_cfg()
            acct    = get_system_accounting(svcs)
            loaded  = await _get_ollama_loaded()

            # Append to benchmark history
            _bench_history.append({
                "ts":        datetime.now().strftime("%H:%M:%S"),
                "vram_used": round(vram.get("used_gb", 0), 2),
                "gpu_pct":   gpu.get("gpu_use_percent", 0),
                "models":    [m["name"] for m in loaded],
            })

            from core.resources import WARN_THRESHOLD, HARD_THRESHOLD, get_effective_vram_total
            pct = vram.get("percent", 0) / 100
            vram_total = get_effective_vram_total()
            # Profile state for WS
            _pcfg = _profiles_cfg()
            _active_profile_name = _pcfg.get("active")

            # Run blocking Docker calls in executor to avoid stalling the event loop
            svc_statuses = await loop.run_in_executor(None, all_status, svcs)
            kill_order   = await loop.run_in_executor(None, _build_kill_order, svcs)

            payload = {
                "vram":           vram,
                "gpu":            gpu,
                "ram":            {"used_gb": round(ram.used/1e9,2), "total_gb": round(ram.total/1e9,2), "percent": ram.percent},
                "ram_tier":       ram_tier.get_usage(),
                "accounting":     acct,
                "active_modes":   list(_active_modes),
                "services":       svc_statuses,
                "alerts":         check_alerts(),
                "kill_log":       list(_kill_log)[:10],
                "ollama_loaded":  loaded,
                "enforcement": {
                    "enabled":           _enforcer.enforcer_enabled,
                    "active_modes":      list(_active_modes),
                    "enforcing":         _enforcer.enforcer_enabled and pct >= HARD_THRESHOLD and bool(_active_modes),
                    "warn_threshold":    WARN_THRESHOLD,
                    "hard_threshold":    HARD_THRESHOLD,
                    "warn_at_gb":        round(vram_total * WARN_THRESHOLD, 1),
                    "hard_at_gb":        round(vram_total * HARD_THRESHOLD, 1),
                    "vram_ceiling_gb":   _enforcer.vram_virtual_ceiling_gb,
                    "real_total_gb":     vram.get("total_gb", 20),
                    "effective_total_gb": round(vram_total, 1),
                    "kill_order":        kill_order,
                },
                "profile": {
                    "active":          _active_profile_name is not None,
                    "name":            _active_profile_name,
                    "vram_ceiling_gb": _enforcer.vram_virtual_ceiling_gb,
                },
            }
            await websocket.send_json(payload)
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass


@app.get("/config/services", tags=["Config"])
def config_services_get():
    return json.loads(SERVICES_CFG_FILE.read_text())["services"]


@app.post("/config/services", tags=["Config"])
async def config_services_add(body: dict):
    name = body.get("name", "").strip().lower().replace(" ", "-")
    if not name:
        return {"error": "name is required"}
    if not _CONTAINER_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"Invalid service name: must match {_CONTAINER_NAME_RE.pattern}")
    container = body.get("container", name)
    if not _CONTAINER_NAME_RE.match(container):
        raise HTTPException(status_code=400, detail=f"Invalid container name: must match {_CONTAINER_NAME_RE.pattern}")
    async with _config_lock:
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
        _atomic_write(SERVICES_CFG_FILE, json.dumps(cfg, indent=2))
        return cfg["services"][name]


@app.patch("/config/services/{name}", tags=["Config"])
async def config_services_patch(name: str, body: dict):
    async with _config_lock:
        cfg = json.loads(SERVICES_CFG_FILE.read_text())
        if name not in cfg["services"]:
            raise HTTPException(status_code=404, detail=f"Service '{name}' not found")
        ALLOWED = {"priority", "limit_mode", "vram_est_gb", "ram_est_gb", "auto_restore",
                   "description", "group", "boot_with_system", "memory_mode", "bus_preference"}
        for k, v in body.items():
            if k in ALLOWED:
                cfg["services"][name][k] = v
        _atomic_write(SERVICES_CFG_FILE, json.dumps(cfg, indent=2))
        return cfg["services"][name]


@app.get("/docker/discover", tags=["Docker"])
def docker_discover():
    """List Docker containers on oaio-net not registered in services.json."""
    registered = set(s.get("container", k) for k, s in _services_cfg().items())
    return discover_unregistered(registered)


@app.get("/enforcement/status", tags=["Enforcement"])
def enforcement_status():
    """Current enforcement state — thresholds, live VRAM, what would be killed next."""
    vram = get_vram_usage()
    pct  = vram.get("percent", 0) / 100
    services = _services_cfg()

    from core.resources import WARN_THRESHOLD, HARD_THRESHOLD, _FALLBACK_TOTAL
    vram_total = vram.get("total_gb", _FALLBACK_TOTAL)
    enabled = _enforcer.enforcer_enabled
    return {
        "vram":            vram,
        "pct":             round(pct * 100, 1),
        "warn_threshold":  WARN_THRESHOLD,
        "hard_threshold":  HARD_THRESHOLD,
        "warn_at_gb":      round(vram_total * WARN_THRESHOLD, 1),
        "hard_at_gb":      round(vram_total * HARD_THRESHOLD, 1),
        "enabled":         enabled,
        "enforcing":       enabled and pct >= HARD_THRESHOLD and bool(_active_modes),
        "active_modes":    list(_active_modes),
        "paused":          not bool(_active_modes),
        "kill_order":      _build_kill_order(services),
        "kill_log":        list(_kill_log),
    }


@app.post("/enforcement/enable", tags=["Enforcement"])
def enforcement_enable():
    _enforcer.enforcer_enabled = True
    return {"enabled": True}


@app.post("/enforcement/disable", tags=["Enforcement"])
def enforcement_disable():
    _enforcer.enforcer_enabled = False
    return {"enabled": False}


@app.post("/enforcement/ceiling", tags=["Enforcement"])
def enforcement_set_ceiling(body: dict):
    """Set virtual VRAM ceiling and/or warn/hard thresholds.
    body: {vram_ceiling_gb?: float|null, warn_threshold?: float, hard_threshold?: float}
    Setting vram_ceiling_gb to null clears the ceiling (uses real VRAM total).
    """
    from core import resources
    from core.vram import get_vram_usage

    real_total = get_vram_usage().get("total_gb", 20.0)
    warnings = []

    if "vram_ceiling_gb" in body:
        val = body["vram_ceiling_gb"]
        if val is None or val == 0:
            _enforcer.vram_virtual_ceiling_gb = None
        else:
            val = float(val)
            if math.isnan(val) or math.isinf(val):
                return {"error": "VRAM ceiling must be a finite number"}
            if val > real_total:
                warnings.append(f"Ceiling {val}GB exceeds real VRAM ({real_total}GB)")
            if val < 0:
                return {"error": "VRAM ceiling cannot be negative"}
            _enforcer.vram_virtual_ceiling_gb = val

    if "warn_threshold" in body:
        wt = float(body["warn_threshold"])
        if math.isnan(wt) or math.isinf(wt):
            return {"error": "warn_threshold must be a finite number"}
        resources.WARN_THRESHOLD = max(0.0, min(1.0, wt))
    if "hard_threshold" in body:
        ht = float(body["hard_threshold"])
        if math.isnan(ht) or math.isinf(ht):
            return {"error": "hard_threshold must be a finite number"}
        resources.HARD_THRESHOLD = max(0.0, min(1.0, ht))

    effective = _enforcer.vram_virtual_ceiling_gb or real_total
    return {
        "vram_ceiling_gb": _enforcer.vram_virtual_ceiling_gb,
        "effective_total_gb": effective,
        "real_total_gb": real_total,
        "warn_threshold": resources.WARN_THRESHOLD,
        "hard_threshold": resources.HARD_THRESHOLD,
        "warn_at_gb": round(effective * resources.WARN_THRESHOLD, 1),
        "hard_at_gb": round(effective * resources.HARD_THRESHOLD, 1),
        "warnings": warnings,
    }


@app.post("/modes/{name}/reset", tags=["Modes"])
async def reset_mode_allocations(name: str):
    """Restore a mode's allocations and budget to their startup snapshot values."""
    if name not in _MODES_SNAPSHOT:
        return {"error": f"Unknown mode: {name}"}
    async with _config_lock:
        cfg = json.loads(MODES_CFG_FILE.read_text())
        if name not in cfg["modes"]:
            return {"error": f"Unknown mode: {name}"}
        snap = copy.deepcopy(_MODES_SNAPSHOT[name])
        cfg["modes"][name]["allocations"]   = snap.get("allocations", {})
        cfg["modes"][name]["vram_budget_gb"] = snap.get("vram_budget_gb", 0)
        _write_modes(cfg)
        return {"reset": name, "allocations": cfg["modes"][name]["allocations"],
                "vram_budget_gb": cfg["modes"][name]["vram_budget_gb"]}


# ── Profile / Bottleneck Simulator endpoints ──────────────────────────────────

@app.get("/config/profiles", tags=["Config"])
def config_profiles_get():
    return _profiles_cfg()


@app.post("/config/profiles", tags=["Config"])
def config_profiles_add(body: dict):
    """Add a custom hardware profile."""
    name = body.get("name", "").strip()
    if not name:
        return {"error": "name is required"}
    key = name.lower().replace(" ", "-")
    cfg = _profiles_cfg()
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
    _save_profiles(cfg)
    return {"created": key, "profile": cfg["profiles"][key]}


@app.post("/config/profiles/{name}/activate", tags=["Config"])
def config_profiles_activate(name: str):
    """Activate a hardware profile — sets VRAM ceiling + Docker cgroup limits."""
    cfg = _profiles_cfg()
    if name not in cfg.get("profiles", {}):
        return {"error": f"Unknown profile: {name}"}
    profile = cfg["profiles"][name]
    _apply_profile(profile, name)
    cfg["active"] = name
    _save_profiles(cfg)
    return {"activated": name, "profile": profile, "vram_ceiling_gb": _enforcer.vram_virtual_ceiling_gb}


@app.post("/config/profiles/deactivate", tags=["Config"])
def config_profiles_deactivate():
    """Deactivate the current hardware profile — remove all simulated limits."""
    _deactivate_profile()
    cfg = _profiles_cfg()
    cfg["active"] = None
    _save_profiles(cfg)
    return {"deactivated": True, "vram_ceiling_gb": None}


# ── API Monitor endpoints ──────────────────────────────────────────────────────

@app.get("/api/monitor/stream", tags=["Monitor"])
def monitor_stream(limit: int = 50):
    """Return the last N logged requests."""
    items = list(_req_log)
    return items[-limit:]


@app.get("/api/monitor/stats", tags=["Monitor"])
def monitor_stats():
    """Aggregated request stats."""
    total = _req_stats["total"]
    avg = round(_req_stats["latency_sum"] / total, 1) if total else 0
    top = sorted(_req_stats["endpoints"].items(), key=lambda x: -x[1])[:5]
    return {
        "total_reqs": total,
        "avg_latency_ms": avg,
        "error_count": _req_stats["errors"],
        "top_endpoints": [{"path": p, "count": c} for p, c in top],
    }


@app.websocket("/api/monitor/ws")
async def monitor_ws(websocket: WebSocket):
    """Live push of new requests as they happen."""
    await websocket.accept()
    _monitor_ws_clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        try:
            _monitor_ws_clients.remove(websocket)
        except ValueError:
            pass


# ── Load extensions (routers mounted before static-file catch-all) ───────────
_load_extensions(app)


# ── Extension API ─────────────────────────────────────────────────────────────

@app.get("/extensions", tags=["Extensions"])
def extensions_list():
    return _list_extensions()


@app.post("/extensions/{name}/enable", tags=["Extensions"])
def extensions_enable(name: str):
    return _ext_set_enabled(name, True)


@app.post("/extensions/{name}/disable", tags=["Extensions"])
def extensions_disable(name: str):
    return _ext_set_enabled(name, False)


# ── Serve extension assets — before main frontend catch-all ───────────────────
if EXTENSIONS_DIR.exists():
    app.mount("/ext", _NoCacheStatic(directory=str(EXTENSIONS_DIR)), name="extensions")

# ── Serve frontend — must be last (catches all unmatched paths) ───────────────
_frontend = Path(__file__).parent.parent.parent / "frontend" / "src"
if _frontend.exists():
    app.mount("/", _NoCacheStatic(directory=str(_frontend), html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000)
