"""Companion tool functions — callable from the chat pipeline.

These provide Kira with abilities beyond conversation:
- search_web: look things up via SearXNG
- describe_screen: see what's on screen via Florence-2
- remember / recall: persistent memory via Letta
"""

import json
from pathlib import Path

import httpx

_SERVICES_FILE = Path(__file__).parent.parent.parent / "config" / "services.json"


def _get_url(service: str) -> str:
    try:
        cfg = json.loads(_SERVICES_FILE.read_text())
        svc = cfg.get("services", {}).get(service, {})
        container = svc.get("container", service)
        port = svc.get("port")
        if container and port:
            return f"http://{container}:{port}"
    except Exception:
        pass
    defaults = {
        "searxng": "http://searxng:8080",
        "florence-2": "http://florence-2:8010",
        "letta": "http://letta:8283",
    }
    return defaults.get(service, f"http://{service}:8000")


async def search_web(query: str, max_results: int = 3) -> list[dict]:
    """Search the web via SearXNG. Returns top results with title + url + content."""
    url = _get_url("searxng")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{url}/search", params={
                "q": query,
                "format": "json",
                "categories": "general",
            })
            r.raise_for_status()
            data = r.json()
            results = []
            for item in data.get("results", [])[:max_results]:
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "content": item.get("content", "")[:200],
                })
            return results
    except Exception as e:
        print(f"[companion-tools] search_web error: {e}")
        return [{"error": str(e)}]


async def describe_screen(screenshot_b64: str) -> str:
    """Send a screenshot to Florence-2 and get a description back."""
    url = _get_url("florence-2")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{url}/describe", json={
                "image_b64": screenshot_b64,
            })
            r.raise_for_status()
            return r.json().get("description", "")
    except Exception as e:
        print(f"[companion-tools] describe_screen error: {e}")
        return f"[Vision error: {e}]"


async def remember(key: str, value: str) -> bool:
    """Store a key-value memory in Letta."""
    url = _get_url("letta")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{url}/v1/agents/memories", json={
                "key": key,
                "value": value,
            })
            return r.status_code < 300
    except Exception as e:
        print(f"[companion-tools] remember error: {e}")
        return False


async def recall(key: str) -> str:
    """Retrieve a memory from Letta by key."""
    url = _get_url("letta")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{url}/v1/agents/memories/{key}")
            if r.status_code == 200:
                return r.json().get("value", "")
            return ""
    except Exception as e:
        print(f"[companion-tools] recall error: {e}")
        return ""
