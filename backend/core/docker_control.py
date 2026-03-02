"""
Docker container management via Docker SDK.
"""
import docker

client = docker.from_env()


def get_status(container_name: str) -> dict:
    try:
        c = client.containers.get(container_name)
        return {"name": container_name, "status": c.status}
    except docker.errors.NotFound:
        return {"name": container_name, "status": "not_found"}
    except Exception as e:
        return {"name": container_name, "status": "error", "error": str(e)}


def start(container_name: str) -> dict:
    try:
        c = client.containers.get(container_name)
        c.start()
        return {"name": container_name, "action": "started", "ok": True}
    except Exception as e:
        return {"name": container_name, "action": "start", "ok": False, "error": str(e)}


def stop(container_name: str, timeout: int = 3) -> dict:
    try:
        c = client.containers.get(container_name)
        c.stop(timeout=timeout)
        return {"name": container_name, "action": "stopped", "ok": True}
    except Exception as e:
        return {"name": container_name, "action": "stop", "ok": False, "error": str(e)}


def get_logs(container_name: str, lines: int = 50) -> str:
    try:
        c = client.containers.get(container_name)
        return c.logs(tail=lines).decode("utf-8")
    except Exception as e:
        return str(e)


def all_status(service_registry: dict) -> list:
    return [
        get_status(s["container"])
        for s in service_registry.values()
        if s.get("container")
    ]
