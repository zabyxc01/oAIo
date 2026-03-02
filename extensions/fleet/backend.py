"""
Fleet extension — HTTP-only multi-node orchestration.
Mounted at /extensions/fleet by the extension loader.

Node lifecycle:
  POST /nodes/register   — remote oAIo registers (or re-registers) itself
  GET  /nodes            — list all known nodes
  GET  /nodes/{id}       — single node detail + live status
  DELETE /nodes/{id}     — deregister
  POST /nodes/{id}/ping  — manual health check

Job lifecycle:
  POST /jobs             — dispatch a job to a node
  GET  /jobs             — list jobs (filterable by node/status)
  GET  /jobs/{id}        — single job detail
  POST /jobs/{id}/cancel — cancel pending job

WebSocket:
  WS /ws                 — fleet-wide status stream (1Hz)
"""
import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()

# Persistent state lives next to this file
_STATE_FILE = Path(__file__).parent / "fleet.json"

_JOB_TYPES = {
    "mode_activate":   "POST /modes/{target}/activate",
    "mode_deactivate": "POST /modes/{target}/deactivate",
    "service_start":   "POST /services/{target}/start",
    "service_stop":    "POST /services/{target}/stop",
    "template_load":   "POST /templates/{target}/load",
}


# ─── State helpers ────────────────────────────────────────────────────────────

def _load() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except Exception:
            pass
    return {"nodes": {}, "jobs": {}}


def _save(state: dict) -> None:
    _STATE_FILE.write_text(json.dumps(state, indent=2))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Node endpoints ───────────────────────────────────────────────────────────

@router.post("/nodes/register")
async def register_node(body: dict):
    """
    Remote oAIo calls this to join the fleet.
    body: {name, url, tags?}
    url: the remote oAIo API base (e.g. "http://192.168.1.50:9000")
    """
    name = body.get("name", "").strip()
    url  = body.get("url",  "").strip().rstrip("/")
    if not name or not url:
        return {"error": "name and url are required"}

    state = _load()

    # Check if already registered by URL — update rather than duplicate
    existing_id = next(
        (nid for nid, n in state["nodes"].items() if n["url"] == url), None
    )
    node_id = existing_id or str(uuid.uuid4())[:8]

    # Probe the remote to get its capabilities
    capabilities = {}
    reachable = False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{url}/config/services")
            if r.status_code == 200:
                capabilities = r.json()
                reachable = True
    except Exception:
        pass

    node = {
        "id":           node_id,
        "name":         name,
        "url":          url,
        "tags":         body.get("tags", []),
        "registered_at": state["nodes"].get(node_id, {}).get("registered_at", _now()),
        "last_seen":    _now(),
        "reachable":    reachable,
        "capabilities": capabilities,
    }
    state["nodes"][node_id] = node
    _save(state)

    return {"registered": True, "id": node_id, "reachable": reachable}


@router.get("/nodes")
def list_nodes():
    return list(_load()["nodes"].values())


@router.get("/nodes/{node_id}")
async def get_node(node_id: str):
    state = _load()
    node = state["nodes"].get(node_id)
    if not node:
        return {"error": "Node not found"}

    # Fetch live status from remote
    live = {}
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(f"{node['url']}/system/status")
            if r.status_code == 200:
                live = r.json()
                node["last_seen"] = _now()
                node["reachable"] = True
                _save(state)
    except Exception:
        node["reachable"] = False

    return {**node, "live": live}


@router.post("/nodes/{node_id}/ping")
async def ping_node(node_id: str):
    state = _load()
    node  = state["nodes"].get(node_id)
    if not node:
        return {"error": "Node not found"}

    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(f"{node['url']}/vram")
            ok = r.status_code == 200
    except Exception:
        ok = False

    node["reachable"] = ok
    node["last_seen"] = _now()
    _save(state)
    return {"id": node_id, "reachable": ok, "last_seen": node["last_seen"]}


@router.delete("/nodes/{node_id}")
def deregister_node(node_id: str):
    state = _load()
    if node_id not in state["nodes"]:
        return {"error": "Node not found"}
    name = state["nodes"].pop(node_id)["name"]
    _save(state)
    return {"deregistered": node_id, "name": name}


# ─── Job endpoints ────────────────────────────────────────────────────────────

@router.post("/jobs")
async def dispatch_job(body: dict):
    """
    Dispatch a job to a fleet node.
    body: {node_id, type, target?, payload?}
    type: one of mode_activate | mode_deactivate | service_start | service_stop | template_load
    target: mode name / service name / template name (for the remote URL param)
    payload: extra body to forward (e.g. {force: true} for mode_activate)
    """
    node_id  = body.get("node_id", "").strip()
    job_type = body.get("type", "").strip()
    target   = body.get("target", "").strip()
    payload  = body.get("payload", {})

    if not node_id or not job_type:
        return {"error": "node_id and type are required"}
    if job_type not in _JOB_TYPES:
        return {"error": f"Unknown job type. Valid: {list(_JOB_TYPES)}"}

    state = _load()
    node = state["nodes"].get(node_id)
    if not node:
        return {"error": f"Node '{node_id}' not found"}

    job_id = str(uuid.uuid4())[:8]
    job = {
        "id":           job_id,
        "node_id":      node_id,
        "node_name":    node["name"],
        "type":         job_type,
        "target":       target,
        "payload":      payload,
        "status":       "dispatching",
        "result":       None,
        "error":        None,
        "created_at":   _now(),
        "completed_at": None,
    }
    state["jobs"][job_id] = job
    _save(state)

    # Build remote URL
    method, path_tpl = _JOB_TYPES[job_type].split(" ", 1)
    remote_path = path_tpl.format(target=target) if target else path_tpl
    remote_url  = f"{node['url']}{remote_path}"

    # Dispatch
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if method == "POST":
                r = await client.post(remote_url, json=payload)
            else:
                r = await client.get(remote_url)

        job["status"]       = "complete" if r.status_code < 400 else "failed"
        job["result"]       = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
        job["completed_at"] = _now()
        node["last_seen"]   = _now()
        node["reachable"]   = True
    except Exception as e:
        job["status"]       = "failed"
        job["error"]        = str(e)
        job["completed_at"] = _now()
        node["reachable"]   = False

    _save(state)
    return job


@router.get("/jobs")
def list_jobs(node_id: str = "", status: str = ""):
    jobs = list(_load()["jobs"].values())
    if node_id:
        jobs = [j for j in jobs if j["node_id"] == node_id]
    if status:
        jobs = [j for j in jobs if j["status"] == status]
    return sorted(jobs, key=lambda j: j["created_at"], reverse=True)


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = _load()["jobs"].get(job_id)
    return job or {"error": "Job not found"}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    state = _load()
    job = state["jobs"].get(job_id)
    if not job:
        return {"error": "Job not found"}
    if job["status"] != "dispatching":
        return {"error": f"Cannot cancel job in state '{job['status']}'"}
    job["status"]       = "cancelled"
    job["completed_at"] = _now()
    _save(state)
    return job


# ─── Fleet WebSocket ──────────────────────────────────────────────────────────

@router.websocket("/ws")
async def fleet_ws(websocket: WebSocket):
    """1Hz fleet status — node reachability + recent jobs."""
    await websocket.accept()
    try:
        while True:
            state = _load()
            nodes = list(state["nodes"].values())
            recent_jobs = sorted(
                state["jobs"].values(),
                key=lambda j: j["created_at"],
                reverse=True,
            )[:20]
            await websocket.send_json({
                "nodes":      nodes,
                "jobs":       recent_jobs,
                "node_count": len(nodes),
                "online":     sum(1 for n in nodes if n.get("reachable")),
            })
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
