"""
Service add API — scan and register new Docker services.
Used by the BUILD view's "+ Service" flow.
"""
import asyncio
import json
from datetime import datetime

import docker as docker_sdk
from fastapi import APIRouter, HTTPException

from api.shared import (
    CONTAINER_NAME_RE, SERVICES_CFG_FILE, SCANS_CFG_FILE,
    config_lock, atomic_write, services_cfg, load_scans, docker_client,
)

router = APIRouter()


@router.post("/services/add", tags=["Services"])
async def add_service_scan(body: dict):
    """Scan an existing Docker container or pull an image and scan it.

    body: {
        name: "my-service",              # required
        container: "existing-container",  # use existing container
        image: "docker.io/...",           # OR pull this image
        port: 8000,                       # expected API port
    }

    Returns scan result + suggested config for confirmation.
    """
    name = body.get("name", "").strip().lower().replace(" ", "-")
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if not CONTAINER_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"Invalid name: must match {CONTAINER_NAME_RE.pattern}")

    # Check not already registered
    existing = services_cfg()
    if name in existing:
        raise HTTPException(status_code=409, detail=f"Service '{name}' already registered")

    container_name = body.get("container", "").strip()
    image = body.get("image", "").strip()
    port = body.get("port", 8000)

    if not container_name and not image:
        raise HTTPException(status_code=400, detail="Either 'container' or 'image' is required")

    dc = docker_client()
    sandbox = False

    try:
        if container_name:
            # Use existing container — check it exists
            try:
                ctr = dc.containers.get(container_name)
            except docker_sdk.errors.NotFound:
                raise HTTPException(status_code=404, detail=f"Container '{container_name}' not found")
            if ctr.status != "running":
                raise HTTPException(status_code=400, detail=f"Container '{container_name}' is {ctr.status}, must be running to scan")

        elif image:
            # Pull image and start sandbox container
            container_name = f"oaio-scan-{name}"
            try:
                print(f"[service-add] Pulling image: {image}")
                dc.images.pull(image)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Failed to pull image: {e}")

            try:
                ctr = dc.containers.run(
                    image,
                    name=container_name,
                    detach=True,
                    remove=False,
                    network="oaio-net",
                    mem_limit="2g",
                    cpu_count=2,
                )
                sandbox = True
                # Wait for container to be ready
                for _ in range(30):
                    ctr.reload()
                    if ctr.status == "running":
                        break
                    await asyncio.sleep(1)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to start sandbox: {e}")

        # Run scanner
        from api.services import _scan_service
        base_url = f"http://{container_name}:{port}"
        scan_result = await _scan_service(base_url, name)

        # Build suggested config
        suggested = {
            "name": name,
            "container": container_name,
            "port": port,
            "vram_est_gb": 0,
            "ram_est_gb": 1.0,
            "priority": 20,
            "group": "Other",
            "description": "",
            "capabilities": scan_result.get("capabilities", []),
            "scan": scan_result,
            "sandbox": sandbox,
        }

        # Cache scan result
        async with config_lock:
            scans = load_scans()
            scans[name] = scan_result
            atomic_write(SCANS_CFG_FILE, json.dumps(scans, indent=2))

        return suggested

    except HTTPException:
        raise
    except Exception as e:
        # Cleanup sandbox on error
        if sandbox:
            try:
                dc.containers.get(container_name).remove(force=True)
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/services/add/confirm", tags=["Services"])
async def confirm_service(body: dict):
    """After scanning, confirm to register the service.

    body: {name, container, port, vram_est_gb, ram_est_gb, priority, group, description, sandbox?}
    """
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    container = body.get("container", name)
    sandbox = body.get("sandbox", False)

    async with config_lock:
        cfg = json.loads(SERVICES_CFG_FILE.read_text())
        if name in cfg["services"]:
            raise HTTPException(status_code=409, detail=f"Service '{name}' already registered")

        cfg["services"][name] = {
            "container": container,
            "port": body.get("port", 8000),
            "vram_est_gb": body.get("vram_est_gb", 0),
            "ram_est_gb": body.get("ram_est_gb", 1.0),
            "priority": body.get("priority", 20),
            "limit_mode": body.get("limit_mode", "soft"),
            "group": body.get("group", "Other"),
            "description": body.get("description", ""),
            "capabilities": body.get("capabilities", []),
        }
        atomic_write(SERVICES_CFG_FILE, json.dumps(cfg, indent=2))

    # Stop sandbox container if it was created for scanning
    if sandbox:
        try:
            dc = docker_client()
            dc.containers.get(container).stop(timeout=5)
        except Exception:
            pass

    return {"registered": name, "service": cfg["services"][name]}
