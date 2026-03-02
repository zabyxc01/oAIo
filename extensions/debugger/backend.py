"""
Debugger extension — container log streaming and error filtering.
Mounted at /extensions/debugger by the extension loader.

Endpoints:
  GET /logs/{container}?lines=100   — tail N lines from container logs
  GET /errors/{container}?lines=500 — tail N lines filtered to ERROR|WARN|Exception|Traceback|Critical
  WS  /ws/{container}               — live log stream, one JSON line at a time
"""
import asyncio
import json
import re

import docker
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()

# Regex for level detection (case-insensitive)
_ERROR_PAT = re.compile(r"error|exception|traceback|critical", re.IGNORECASE)
_WARN_PAT  = re.compile(r"warn|warning", re.IGNORECASE)
_FILTER_PAT = re.compile(r"error|warn|exception|traceback|critical", re.IGNORECASE)


def _get_client():
    return docker.from_env()


def _detect_level(line: str) -> str:
    if _ERROR_PAT.search(line):
        return "error"
    if _WARN_PAT.search(line):
        return "warn"
    return "info"


def _decode_log_bytes(raw) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace").rstrip("\n")
    return str(raw).rstrip("\n")


# ─── REST endpoints ──────────────────────────────────────────────────────────

@router.get("/logs/{container}")
def get_logs(container: str, lines: int = 100):
    """Tail N lines from container logs."""
    try:
        client = _get_client()
        ctr = client.containers.get(container)
        raw = ctr.logs(tail=lines, stream=False, follow=False)
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        log_lines = [l for l in text.split("\n") if l]
        return {
            "container": container,
            "lines": log_lines,
            "count": len(log_lines),
        }
    except docker.errors.NotFound:
        return {"error": f"Container '{container}' not found", "lines": [], "count": 0}
    except Exception as e:
        return {"error": str(e), "lines": [], "count": 0}


@router.get("/errors/{container}")
def get_errors(container: str, lines: int = 500):
    """Tail N lines filtered to ERROR|WARN|Exception|Traceback|Critical (case-insensitive)."""
    try:
        client = _get_client()
        ctr = client.containers.get(container)
        raw = ctr.logs(tail=lines, stream=False, follow=False)
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        all_lines = [l for l in text.split("\n") if l]
        filtered = [l for l in all_lines if _FILTER_PAT.search(l)]
        return {
            "container": container,
            "lines": filtered,
            "count": len(filtered),
            "scanned": len(all_lines),
        }
    except docker.errors.NotFound:
        return {"error": f"Container '{container}' not found", "lines": [], "count": 0}
    except Exception as e:
        return {"error": str(e), "lines": [], "count": 0}


# ─── WebSocket — live log stream ──────────────────────────────────────────────

@router.websocket("/ws/{container}")
async def log_ws(websocket: WebSocket, container: str):
    """
    Live log stream for a container.
    Sends one JSON object per line: {"line": "...", "level": "error"|"warn"|"info"}
    """
    await websocket.accept()
    loop = asyncio.get_event_loop()

    try:
        client = _get_client()
        ctr = client.containers.get(container)
    except docker.errors.NotFound:
        await websocket.send_text(json.dumps({
            "line": f"[debugger] Container '{container}' not found",
            "level": "error",
        }))
        await websocket.close()
        return
    except Exception as e:
        await websocket.send_text(json.dumps({
            "line": f"[debugger] Error: {e}",
            "level": "error",
        }))
        await websocket.close()
        return

    # Stream logs in a thread so we don't block the event loop
    def _stream():
        for chunk in ctr.logs(stream=True, follow=True, tail=50):
            line = _decode_log_bytes(chunk)
            if not line:
                continue
            level = _detect_level(line)
            yield json.dumps({"line": line, "level": level})

    try:
        gen = _stream()
        while True:
            try:
                msg = await loop.run_in_executor(None, next, gen)
                await websocket.send_text(msg)
            except StopIteration:
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
