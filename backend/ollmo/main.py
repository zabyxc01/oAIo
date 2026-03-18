"""
oLLMo API — system orchestration.
Port: 9000

This file handles: FastAPI app initialization, middleware, and router imports.
All route handlers live in backend/api/ modules.
"""
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse, JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# Ensure backend/ is on sys.path so `api.*` and `core.*` imports work
sys.path.insert(0, str(Path(__file__).parent.parent))

from api.shared import (
    ACTIVE_MODES_FILE, PATHS_CFG_FILE, PROFILES_CFG_FILE,
    active_modes, enforcer, enforcement_mode,
    services_cfg, profiles_cfg, apply_profile,
    atomic_write, heal_dangling,
    enforcement_loop, docker_client,
    req_log, req_stats, monitor_ws_clients,
    load_extensions, list_extensions, ext_set_enabled, EXTENSIONS_DIR,
)

import httpx
RVC_GRADIO = os.environ.get("RVC_GRADIO", "http://rvc:7865")


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

        if path == "/":
            await self.app(scope, receive, send)
            return
        if any(path.startswith(p) for p in _AUTH_SKIP_PREFIXES):
            await self.app(scope, receive, send)
            return
        last_seg = path.rsplit("/", 1)[-1]
        if "." in last_seg:
            ext = "." + last_seg.rsplit(".", 1)[-1].lower()
            if ext in _AUTH_SKIP_EXTENSIONS:
                await self.app(scope, receive, send)
                return

        if scope["type"] == "websocket":
            qs = scope.get("query_string", b"").decode("utf-8", errors="replace")
            params = parse_qs(qs)
            token = params.get("token", [None])[0]
            if token != _API_TOKEN:
                await send({"type": "websocket.close", "code": 4001})
                return
            await self.app(scope, receive, send)
            return

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


# ── Request monitor middleware ───────────────────────────────────────────────
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
            req_log.append(entry)
            req_stats["total"] += 1
            req_stats["latency_sum"] += latency_ms
            if status_code >= 400:
                req_stats["errors"] += 1
            req_stats["endpoints"][path] = req_stats["endpoints"].get(path, 0) + 1

            for ws in list(monitor_ws_clients):
                try:
                    await ws.send_json(entry)
                except Exception:
                    try:
                        monitor_ws_clients.remove(ws)
                    except ValueError:
                        pass


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


# ── Lifespan ─────────────────────────────────────────────────────────────────

async def _rvc_startup_refresh():
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(f"{RVC_GRADIO}/run/infer_refresh", json={"data": []})
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Restore active_modes from last session
    if ACTIVE_MODES_FILE.exists():
        try:
            for m in json.loads(ACTIVE_MODES_FILE.read_text()):
                active_modes.add(m)
        except Exception:
            pass

    # Restore active profile
    try:
        pcfg = profiles_cfg()
        active_name = pcfg.get("active")
        if active_name and active_name in pcfg.get("profiles", {}):
            apply_profile(pcfg["profiles"][active_name], active_name)
    except Exception:
        pass

    # Heal dangling symlinks
    try:
        paths_cfg = json.loads(PATHS_CFG_FILE.read_text())
        healed = heal_dangling(paths_cfg)
        if healed:
            print(f"[oAIo] Healed {len(healed)} dangling symlink targets: {healed}")
    except Exception:
        pass

    enforcer.enforcer_enabled = True

    asyncio.create_task(_rvc_startup_refresh())
    task = asyncio.create_task(
        enforcement_loop(services_cfg, docker_client, enforcement_mode)
    )
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ── App creation ─────────────────────────────────────────────────────────────

app = FastAPI(title="oLLMo", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:9000", "http://127.0.0.1:9000", "http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestLogMiddleware)
if _API_TOKEN:
    app.add_middleware(TokenAuthMiddleware)


# ── Include route modules ────────────────────────────────────────────────────
from api.services import router as services_router
from api.modes import router as modes_router
from api.config import router as config_router
from api.monitoring import router as monitoring_router
from api.graph import router as graph_router
from api.service_add import router as service_add_router

app.include_router(services_router)
app.include_router(modes_router)
app.include_router(config_router)
app.include_router(service_add_router)
app.include_router(monitoring_router)
app.include_router(graph_router)


# ── Extension API ────────────────────────────────────────────────────────────

@app.get("/extensions", tags=["Extensions"])
def extensions_list():
    return list_extensions()


@app.post("/extensions/{name}/enable", tags=["Extensions"])
def extensions_enable(name: str):
    return ext_set_enabled(name, True)


@app.post("/extensions/{name}/disable", tags=["Extensions"])
def extensions_disable(name: str):
    return ext_set_enabled(name, False)


# ── Load extensions (routers mounted before static-file catch-all) ───────────
load_extensions(app)


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
