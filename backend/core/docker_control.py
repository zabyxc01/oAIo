"""
Docker container management via Docker SDK.
"""
import docker

_client = None


def _get_client():
    global _client
    try:
        if _client is not None:
            _client.ping()
            return _client
    except Exception:
        pass
    _client = docker.from_env()
    return _client


def get_status(container_name: str) -> dict:
    try:
        c = _get_client().containers.get(container_name)
        return {"name": container_name, "status": c.status}
    except docker.errors.NotFound:
        return {"name": container_name, "status": "not_found"}
    except Exception as e:
        return {"name": container_name, "status": "error", "error": str(e)}


def start(container_name: str) -> dict:
    try:
        c = _get_client().containers.get(container_name)
        c.start()
        return {"name": container_name, "action": "started", "ok": True}
    except Exception as e:
        return {"name": container_name, "action": "start", "ok": False, "error": str(e)}


def stop(container_name: str, timeout: int = 3) -> dict:
    try:
        c = _get_client().containers.get(container_name)
        c.stop(timeout=timeout)
        return {"name": container_name, "action": "stopped", "ok": True}
    except Exception as e:
        return {"name": container_name, "action": "stop", "ok": False, "error": str(e)}


def get_logs(container_name: str, lines: int = 50) -> str:
    try:
        c = _get_client().containers.get(container_name)
        return c.logs(tail=lines).decode("utf-8")
    except Exception as e:
        return str(e)


def apply_resource_limits(container_name: str, mem_limit_gb: float, cpu_count: int,
                          min_mem_gb: float = 0.25, max_cpu: int = 128) -> dict:
    """Apply cgroup resource limits to a running container."""
    try:
        if mem_limit_gb < 0:
            return {"name": container_name, "ok": False, "error": "mem_limit_gb cannot be negative"}
        if cpu_count < 0:
            return {"name": container_name, "ok": False, "error": "cpu_count cannot be negative"}
        if mem_limit_gb > 0 and mem_limit_gb < min_mem_gb:
            return {"name": container_name, "ok": False,
                    "error": f"mem_limit_gb {mem_limit_gb} below minimum {min_mem_gb}GB"}
        if cpu_count > max_cpu:
            return {"name": container_name, "ok": False,
                    "error": f"cpu_count {cpu_count} exceeds maximum {max_cpu}"}
        c = _get_client().containers.get(container_name)
        if c.status != "running":
            return {"name": container_name, "ok": False, "error": "not running"}
        update_kwargs = {}
        if mem_limit_gb > 0:
            update_kwargs["mem_limit"] = f"{mem_limit_gb}g"
        if cpu_count > 0:
            update_kwargs["nano_cpus"] = int(cpu_count * 1e9)
        if update_kwargs:
            c.update(**update_kwargs)
        return {"name": container_name, "ok": True, "mem_limit_gb": mem_limit_gb, "cpu_count": cpu_count}
    except Exception as e:
        return {"name": container_name, "ok": False, "error": str(e)}


def remove_resource_limits(container_name: str) -> dict:
    """Remove cgroup resource limits (set to 0 = unlimited)."""
    try:
        c = _get_client().containers.get(container_name)
        if c.status != "running":
            return {"name": container_name, "ok": False, "error": "not running"}
        c.update(mem_limit=0, nano_cpus=0)
        return {"name": container_name, "ok": True}
    except Exception as e:
        return {"name": container_name, "ok": False, "error": str(e)}


def set_restart_policy(container_name: str, policy: str = "unless-stopped") -> dict:
    """Set restart policy on a container. policy: 'no', 'unless-stopped', 'always', 'on-failure'."""
    try:
        c = _get_client().containers.get(container_name)
        c.update(restart_policy={"Name": policy, "MaximumRetryCount": 0})
        return {"name": container_name, "ok": True, "restart_policy": policy}
    except Exception as e:
        return {"name": container_name, "ok": False, "error": str(e)}


def all_status(service_registry: dict) -> list:
    result = []
    for svc_name, s in service_registry.items():
        ctr = s.get("container")
        if not ctr:
            continue
        info = get_status(ctr)
        # Enrich with per-service resource estimates for frontend sparklines
        info["vram_est_gb"] = s.get("vram_est_gb", 0)
        info["ram_est_gb"] = s.get("ram_est_gb", 0)
        info["memory_mode"] = s.get("memory_mode", "vram")
        info["group"] = s.get("group", "")
        result.append(info)
    return result


def discover_unregistered(registered_containers: set) -> list:
    """List Docker containers on oaio-net not in services.json."""
    containers = _get_client().containers.list(all=True)
    found = []
    for c in containers:
        if c.name in registered_containers:
            continue
        if c.name == "oaio":
            continue
        nets = c.attrs.get("NetworkSettings", {}).get("Networks", {})
        if "oaio-net" not in nets and "oaio_oaio-net" not in nets:
            continue
        ports = c.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
        exposed = []
        for port_spec, bindings in ports.items():
            if bindings:
                for b in bindings:
                    hp = b.get("HostPort", 0)
                    if hp:
                        exposed.append(int(hp))
        found.append({
            "container": c.name,
            "status": c.status,
            "image": c.image.tags[0] if c.image.tags else c.attrs.get("Config", {}).get("Image", "unknown"),
            "ports": exposed,
        })
    return found
