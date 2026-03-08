"""
Fleet extension — multi-node orchestration with auto-discovery.
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

Heartbeat:
  POST /heartbeat        — self-report from remote node
  GET  /config           — heartbeat + discovery configuration
  PATCH /config          — update configuration

Discovery:
  POST /discover         — trigger manual discovery scan
  GET  /discover/status  — discovery system status
  Background UDP broadcast on port 9001 (configurable)

WebSocket:
  WS /ws                 — fleet-wide status stream (1Hz)
"""
import asyncio
import ipaddress
import json
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

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

_DEFAULT_CONFIG = {
    "heartbeat_mode":     "both",
    "heartbeat_interval": 30,
    "stale_after":        90,
    "discovery_enabled":  True,
    "discovery_port":     9001,
    "discovery_interval": 15,
}

_VALID_HB_MODES = {"self-report", "hub-poll", "both"}

_DISCOVERY_MAGIC = b"oAIo-fleet-v1"
_discovery_task: asyncio.Task | None = None


# ─── State helpers ────────────────────────────────────────────────────────────

def _load_initial() -> dict:
    """Load state from disk once at import time."""
    if _STATE_FILE.exists():
        try:
            data = json.loads(_STATE_FILE.read_text())
        except Exception:
            data = {}
    else:
        data = {}
    data.setdefault("nodes", {})
    data.setdefault("jobs", {})
    data.setdefault("config", dict(_DEFAULT_CONFIG))
    return data


# Module-level in-memory state — loaded once, mutated in place
_state: dict = _load_initial()


def _save() -> None:
    """Persist _state to disk atomically (temp + rename)."""
    tmp = _STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_state, indent=2))
    tmp.rename(_STATE_FILE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── URL safety ──────────────────────────────────────────────────────────────

_BLOCKED_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),   # link-local (cloud metadata)
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
)


def _is_safe_url(url: str) -> tuple[bool, str]:
    """
    Validate that a node URL uses http(s) and does not resolve to a
    link-local or metadata address (SSRF protection).
    Private/loopback IPs are allowed — fleet is designed for LAN + Tailscale.
    Returns (safe, reason).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, "URL scheme must be http or https"
    hostname = parsed.hostname
    if not hostname:
        return False, "URL has no hostname"
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return False, f"Cannot resolve hostname '{hostname}'"
    for _family, _type, _proto, _canonname, sockaddr in infos:
        addr = ipaddress.ip_address(sockaddr[0])
        for net in _BLOCKED_NETWORKS:
            if addr in net:
                return False, f"Hostname resolves to blocked address {addr}"
    return True, ""


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

    safe, reason = _is_safe_url(url)
    if not safe:
        return {"error": f"Rejected node URL: {reason}"}

    # Check if already registered by URL — update rather than duplicate
    existing_id = next(
        (nid for nid, n in _state["nodes"].items() if n["url"] == url), None
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
        "registered_at": _state["nodes"].get(node_id, {}).get("registered_at", _now()),
        "last_seen":    _now(),
        "reachable":    reachable,
        "capabilities": capabilities,
    }
    _state["nodes"][node_id] = node
    _save()

    return {"registered": True, "id": node_id, "reachable": reachable}


@router.get("/nodes")
def list_nodes():
    return list(_state["nodes"].values())


@router.get("/nodes/{node_id}")
async def get_node(node_id: str):
    node = _state["nodes"].get(node_id)
    if not node:
        return {"error": "Node not found"}

    # Fetch live status + modes from remote
    live = {}
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(f"{node['url']}/system/status")
            if r.status_code == 200:
                live = r.json()
                node["last_seen"] = _now()
                node["reachable"] = True
            # Also fetch modes config
            try:
                r2 = await client.get(f"{node['url']}/modes")
                if r2.status_code == 200:
                    live["modes"] = r2.json()
            except Exception:
                pass
            _save()
    except Exception:
        node["reachable"] = False

    return {**node, "live": live}


@router.post("/nodes/{node_id}/ping")
async def ping_node(node_id: str):
    node = _state["nodes"].get(node_id)
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
    _save()
    return {"id": node_id, "reachable": ok, "last_seen": node["last_seen"]}


@router.delete("/nodes/{node_id}")
def deregister_node(node_id: str):
    if node_id not in _state["nodes"]:
        return {"error": "Node not found"}
    name = _state["nodes"].pop(node_id)["name"]
    _save()
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

    node = _state["nodes"].get(node_id)
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
    _state["jobs"][job_id] = job
    _save()

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

    _save()
    return job


@router.get("/jobs")
def list_jobs(node_id: str = "", status: str = ""):
    jobs = list(_state["jobs"].values())
    if node_id:
        jobs = [j for j in jobs if j["node_id"] == node_id]
    if status:
        jobs = [j for j in jobs if j["status"] == status]
    return sorted(jobs, key=lambda j: j["created_at"], reverse=True)


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = _state["jobs"].get(job_id)
    return job or {"error": "Job not found"}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = _state["jobs"].get(job_id)
    if not job:
        return {"error": "Job not found"}
    if job["status"] != "dispatching":
        return {"error": f"Cannot cancel job in state '{job['status']}'"}
    job["status"]       = "cancelled"
    job["completed_at"] = _now()
    _save()
    return job


# ─── Heartbeat system ─────────────────────────────────────────────────────────

_poll_task: asyncio.Task | None = None


@router.post("/heartbeat")
def heartbeat(body: dict):
    """
    Self-report heartbeat from a remote node.
    body: {node_id, vram?, status?}
    """
    node_id = body.get("node_id", "").strip()
    if not node_id:
        return {"error": "node_id is required"}
    node = _state["nodes"].get(node_id)
    if not node:
        return {"error": "Node not found"}

    node["last_seen"] = _now()
    node["reachable"] = True

    if "vram" in body and body["vram"]:
        node["vram_snapshot"] = body["vram"]
    if "status" in body:
        node["status"] = body["status"]

    _save()
    return {"ok": True, "node_id": node_id, "last_seen": node["last_seen"]}


@router.get("/config")
def get_config():
    """Return heartbeat configuration."""
    return dict(_state["config"])


@router.patch("/config")
def patch_config(body: dict):
    """
    Update heartbeat configuration.
    body: {heartbeat_mode?, heartbeat_interval?, stale_after?}
    """
    cfg = _state["config"]
    errors = []

    if "heartbeat_mode" in body:
        if body["heartbeat_mode"] not in _VALID_HB_MODES:
            errors.append(f"heartbeat_mode must be one of: {', '.join(sorted(_VALID_HB_MODES))}")
        else:
            cfg["heartbeat_mode"] = body["heartbeat_mode"]

    if "discovery_enabled" in body:
        cfg["discovery_enabled"] = bool(body["discovery_enabled"])

    if "discovery_interval" in body:
        val = body["discovery_interval"]
        if not isinstance(val, (int, float)) or val < 5:
            errors.append("discovery_interval must be >= 5")
        else:
            cfg["discovery_interval"] = int(val)

    if "heartbeat_interval" in body:
        val = body["heartbeat_interval"]
        if not isinstance(val, (int, float)) or val < 10:
            errors.append("heartbeat_interval must be >= 10")
        else:
            cfg["heartbeat_interval"] = int(val)

    if "stale_after" in body:
        val = body["stale_after"]
        if not isinstance(val, (int, float)) or val < cfg["heartbeat_interval"]:
            errors.append(f"stale_after must be >= heartbeat_interval ({cfg['heartbeat_interval']})")
        else:
            cfg["stale_after"] = int(val)

    if errors:
        return {"error": errors}

    _save()
    _restart_poll_loop()
    _restart_discovery()
    return dict(cfg)


# ─── Discovery endpoints ────────────────────────────────────────────────────

@router.post("/discover")
async def trigger_discover():
    """Manually trigger a discovery broadcast + listen cycle."""
    found = await _discovery_scan()
    return {"discovered": len(found), "nodes": found}


@router.get("/discover/status")
def discover_status():
    cfg = _state["config"]
    return {
        "enabled":  cfg.get("discovery_enabled", True),
        "port":     cfg.get("discovery_port", 9001),
        "interval": cfg.get("discovery_interval", 15),
        "running":  _discovery_task is not None and not _discovery_task.done(),
    }


async def _hub_poll_loop() -> None:
    """Background coroutine: poll all nodes on heartbeat_interval."""
    while True:
        cfg = _state["config"]
        if cfg["heartbeat_mode"] not in ("hub-poll", "both"):
            await asyncio.sleep(cfg["heartbeat_interval"])
            continue

        interval = cfg["heartbeat_interval"]
        stale    = cfg["stale_after"]

        for node in list(_state["nodes"].values()):
            try:
                async with httpx.AsyncClient(timeout=4.0) as client:
                    r = await client.get(f"{node['url']}/vram")
                    if r.status_code == 200:
                        node["last_seen"] = _now()
                        node["reachable"] = True
                        vram_data = r.json()
                        if vram_data:
                            node["vram_snapshot"] = vram_data
            except Exception:
                last = node.get("last_seen", "")
                if last:
                    try:
                        dt = datetime.fromisoformat(last)
                        age = (datetime.now(timezone.utc) - dt).total_seconds()
                        if age > stale:
                            node["reachable"] = False
                    except Exception:
                        node["reachable"] = False
                else:
                    node["reachable"] = False

        _save()
        await asyncio.sleep(interval)


def _restart_poll_loop() -> None:
    """Cancel and restart the hub-poll background task."""
    global _poll_task
    if _poll_task and not _poll_task.done():
        _poll_task.cancel()
    try:
        loop = asyncio.get_running_loop()
        _poll_task = loop.create_task(_hub_poll_loop())
    except RuntimeError:
        pass  # No running loop yet — will be started by _start_poll_on_load


def _start_poll_on_load() -> None:
    """Start the hub-poll loop. Called once when the module is first used."""
    global _poll_task
    if _poll_task is not None:
        return
    try:
        loop = asyncio.get_running_loop()
        _poll_task = loop.create_task(_hub_poll_loop())
    except RuntimeError:
        pass  # No event loop yet — router startup will handle it


# Attempt to start on import (works if event loop is running)
_start_poll_on_load()


# ─── Auto-discovery (UDP broadcast) ─────────────────────────────────────────

def _get_local_ips() -> list[str]:
    """Get non-loopback IPv4 addresses of this machine."""
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if not addr.startswith("127."):
                ips.append(addr)
    except Exception:
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    return ips


async def _discovery_scan() -> list[dict]:
    """
    Send a UDP broadcast beacon and listen for replies.
    Returns list of newly discovered nodes.
    """
    import os
    cfg = _state["config"]
    port = cfg.get("discovery_port", 9001)
    local_ips = _get_local_ips()
    my_name = os.environ.get("OAIO_NODE_NAME", socket.gethostname())
    my_port = int(os.environ.get("OAIO_API_PORT", "9000"))

    # Build beacon payload
    beacon = json.dumps({
        "magic": _DISCOVERY_MAGIC.decode(),
        "name":  my_name,
        "port":  my_port,
        "ips":   local_ips,
    }).encode()

    found = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(0.0)

    try:
        # Broadcast beacon
        sock.sendto(beacon, ("255.255.255.255", port))

        # Also bind to listen for replies and other beacons
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.settimeout(0.0)
        try:
            listener.bind(("", port))
        except OSError:
            listener.close()
            listener = None

        # Listen for 2 seconds
        deadline = asyncio.get_event_loop().time() + 2.0
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.1)
            if not listener:
                continue
            while True:
                try:
                    data, addr = listener.recvfrom(4096)
                except BlockingIOError:
                    break
                except Exception:
                    break
                try:
                    msg = json.loads(data.decode())
                except Exception:
                    continue
                if msg.get("magic") != _DISCOVERY_MAGIC.decode():
                    continue
                sender_ip = addr[0]
                # Skip self
                if sender_ip in local_ips:
                    continue
                sender_port = msg.get("port", 9000)
                sender_name = msg.get("name", sender_ip)
                sender_url = f"http://{sender_ip}:{sender_port}"

                # Check if already registered
                already = any(
                    n["url"] == sender_url for n in _state["nodes"].values()
                )
                if already:
                    # Update last_seen
                    for n in _state["nodes"].values():
                        if n["url"] == sender_url:
                            n["last_seen"] = _now()
                            n["reachable"] = True
                    continue

                # Auto-register
                node_id = str(uuid.uuid4())[:8]
                node = {
                    "id":            node_id,
                    "name":          sender_name,
                    "url":           sender_url,
                    "tags":          ["auto-discovered"],
                    "registered_at": _now(),
                    "last_seen":     _now(),
                    "reachable":     True,
                    "capabilities":  {},
                    "discovered_via": "udp-broadcast",
                }
                # Try to probe capabilities
                try:
                    async with httpx.AsyncClient(timeout=3.0) as client:
                        r = await client.get(f"{sender_url}/config/services")
                        if r.status_code == 200:
                            node["capabilities"] = r.json()
                except Exception:
                    pass

                _state["nodes"][node_id] = node
                found.append({"id": node_id, "name": sender_name, "url": sender_url})

        if listener:
            listener.close()
    except Exception:
        pass
    finally:
        sock.close()

    if found:
        _save()
    return found


async def _discovery_loop() -> None:
    """Background loop: broadcast beacon + listen on interval."""
    import os
    cfg = _state["config"]
    port = cfg.get("discovery_port", 9001)

    # Also run a persistent listener that responds to incoming beacons
    while True:
        if not cfg.get("discovery_enabled", True):
            await asyncio.sleep(cfg.get("discovery_interval", 15))
            continue

        try:
            await _discovery_scan()
        except Exception:
            pass

        await asyncio.sleep(cfg.get("discovery_interval", 15))


def _restart_discovery() -> None:
    """Cancel and restart the discovery background task."""
    global _discovery_task
    if _discovery_task and not _discovery_task.done():
        _discovery_task.cancel()
    cfg = _state["config"]
    if not cfg.get("discovery_enabled", True):
        return
    try:
        loop = asyncio.get_running_loop()
        _discovery_task = loop.create_task(_discovery_loop())
    except RuntimeError:
        pass


@router.on_event("startup")
async def _on_startup():
    """Ensure hub-poll loop and discovery are running when FastAPI starts."""
    _restart_poll_loop()
    _restart_discovery()


# ─── Fleet WebSocket ──────────────────────────────────────────────────────────

@router.websocket("/ws")
async def fleet_ws(websocket: WebSocket):
    """1Hz fleet status — node reachability + recent jobs."""
    await websocket.accept()
    try:
        while True:
            nodes = list(_state["nodes"].values())
            recent_jobs = sorted(
                _state["jobs"].values(),
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
