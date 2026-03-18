"""
Service management routes — CRUD, scanner, autowire, ollama/rvc/comfyui.
"""
import json
import re
import threading
from datetime import datetime
from pathlib import Path

import docker as docker_sdk
import httpx
from fastapi import APIRouter, HTTPException

from api.shared import (
    CONTAINER_NAME_RE, OLLAMA_URL, RVC_GRADIO, COMFYUI_USER_PATH,
    SCANS_CFG_FILE, SERVICE_PORTS_FILE, SERVICES_CFG_FILE,
    config_lock, atomic_write, services_cfg, load_scans,
    register_manual_stop,
    get_status, start, stop, get_logs,
)

router = APIRouter()


# ── Service CRUD ─────────────────────────────────────────────────────────────

@router.get("/services", tags=["Services"])
def list_services():
    return services_cfg()


@router.post("/services/{name}/start", tags=["Services"])
def start_service(name: str):
    services = services_cfg()
    if name not in services:
        return {"error": f"Unknown service: {name}"}
    return start(services[name]["container"])


@router.post("/services/{name}/stop", tags=["Services"])
def stop_service(name: str):
    services = services_cfg()
    if name not in services:
        return {"error": f"Unknown service: {name}"}
    svc = services[name]
    ctr = svc["container"]
    register_manual_stop(ctr, name, svc.get("priority", 3))
    threading.Thread(target=stop, args=(ctr,), daemon=True).start()
    return {"name": ctr, "action": "stopping", "ok": True}


@router.get("/services/{name}/status", tags=["Services"])
def service_status(name: str):
    services = services_cfg()
    if name not in services:
        return {"error": f"Unknown service: {name}"}
    return get_status(services[name]["container"])


@router.get("/services/{name}/logs", tags=["Services"])
def service_logs(name: str, lines: int = 50):
    services = services_cfg()
    if name not in services:
        return {"error": f"Unknown service: {name}"}
    return {"logs": get_logs(services[name]["container"], lines)}


# ── API Scanner ──────────────────────────────────────────────────────────────

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
    try:
        r = await client.head(url)
        return True, r.status_code
    except Exception:
        return False, 0


async def _probe_post_empty(client: httpx.AsyncClient, url: str) -> tuple[bool, int]:
    try:
        r = await client.post(url, json={})
        return True, r.status_code
    except Exception:
        return False, 0


async def _scan_service(base_url: str, service_name: str) -> dict:
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
        alive, status, _ = await _probe_get(client, f"{base_url}/")
        if not alive:
            alive, status, _ = await _probe_get(client, f"{base_url}/health")
        result["reachable"] = alive
        if not alive:
            print(f"[Scanner] {service_name}: unreachable at {base_url}")
            return result

        print(f"[Scanner] {service_name}: reachable at {base_url}")
        endpoints: list[dict] = []

        # OpenAPI detection
        openapi_found = False
        ok, code, body = await _probe_get(client, f"{base_url}/openapi.json")
        if ok and code == 200 and isinstance(body, dict) and "paths" in body:
            print(f"[Scanner] {service_name}: OpenAPI spec found")
            result["api_type"] = "openapi"
            result["openapi_spec"] = body
            openapi_found = True
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
                        for p in detail.get("parameters", []):
                            ep["parameters"].append({
                                "name": p.get("name", ""),
                                "in": p.get("in", ""),
                                "type": p.get("schema", {}).get("type", "string"),
                                "required": p.get("required", False),
                            })
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

        ok_docs, code_docs, _ = await _probe_get(client, f"{base_url}/docs")
        if ok_docs and code_docs == 200:
            print(f"[Scanner] {service_name}: Swagger UI (/docs) available")

        # Gradio detection
        gradio_found = False
        gradio_endpoints: list[dict] = []

        # Gradio v6
        ok_g6, code_g6, body_g6 = await _probe_get(client, f"{base_url}/gradio_api/info")
        if ok_g6 and code_g6 == 200 and isinstance(body_g6, dict):
            print(f"[Scanner] {service_name}: Gradio v6 API detected")
            gradio_found = True
            if not openapi_found:
                result["api_type"] = "gradio_v6"
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

        # OpenAI-compatible detection
        openai_compat = False
        if not openapi_found:
            ok_models, code_models, body_models = await _probe_get(client, f"{base_url}/v1/models")
            if ok_models and code_models == 200 and isinstance(body_models, dict) and body_models.get("object") == "list":
                print(f"[Scanner] {service_name}: OpenAI-compatible API detected")
                openai_compat = True
                if not gradio_found:
                    result["api_type"] = "openai_compat"
                openai_probes = [
                    ("/v1/chat/completions", "POST", "Chat completions"),
                    ("/v1/audio/speech", "POST", "Text-to-speech"),
                    ("/v1/embeddings", "POST", "Embeddings"),
                    ("/v1/completions", "POST", "Text completions"),
                ]
                for path, method, summary in openai_probes:
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
                if not any(ep["path"] == "/v1/models" for ep in endpoints):
                    endpoints.append({
                        "method": "GET",
                        "path": "/v1/models",
                        "summary": "List models",
                        "parameters": [],
                        "tags": ["openai"],
                        "source": "openai_compat",
                    })

        # Generic probing
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

        # Finalize
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


@router.post("/services/{name}/scan", tags=["Services"])
async def scan_service(name: str):
    if not CONTAINER_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"Invalid service name: must match {CONTAINER_NAME_RE.pattern}")
    services = services_cfg()
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

    async with config_lock:
        scans = load_scans()
        scans[name] = scan_result
        atomic_write(SCANS_CFG_FILE, json.dumps(scans, indent=2))

    return scan_result


@router.get("/services/{name}/scan", tags=["Services"])
async def get_scan_result(name: str):
    if not CONTAINER_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"Invalid service name: must match {CONTAINER_NAME_RE.pattern}")
    services = services_cfg()
    if name not in services:
        raise HTTPException(status_code=404, detail=f"Service '{name}' not found")
    scans = load_scans()
    if name not in scans:
        raise HTTPException(status_code=404, detail=f"No scan result for '{name}' — run POST /services/{name}/scan first")
    return scans[name]


# ── Autowire ─────────────────────────────────────────────────────────────────

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

_CAPABILITY_PORTS = {
    "chat":             {"in": [["prompt", "string"]],  "out": [["response", "string"]]},
    "tts":              {"in": [["text", "string"]],    "out": [["audio", "audio"]]},
    "embeddings":       {"in": [["text", "string"]],    "out": [["embedding", "array"]]},
    "voice_conversion": {"in": [["audio", "audio"]],    "out": [["audio", "audio"]]},
    "image_gen":        {"in": [["prompt", "string"]],  "out": [["image", "image"]]},
    "text_gen":         {"in": [["prompt", "string"]],  "out": [["text", "string"]]},
}

_GRADIO_TYPE_MAP = {
    "string": "string", "str": "string", "text": "string", "textbox": "string",
    "number": "number", "int": "number", "float": "number", "slider": "number",
    "audio": "audio", "image": "image", "video": "video", "file": "file",
    "checkbox": "boolean", "bool": "boolean",
    "dropdown": "string", "radio": "string",
    "json": "object", "dataframe": "array",
}


def _gradio_type_to_litegraph(gradio_type: str) -> str:
    return _GRADIO_TYPE_MAP.get(gradio_type.lower().strip(), "any")


def _generate_ports_from_scan(scan: dict) -> dict:
    capabilities = scan.get("capabilities", [])
    endpoints = scan.get("endpoints", [])

    in_ports, out_ports = [], []
    seen_in, seen_out = set(), set()

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

    for cap in capabilities:
        cap_name = cap if isinstance(cap, str) else cap.get("type", "")
        if cap_name == "gradio_app":
            for ep in endpoints:
                for p in ep.get("parameters", []):
                    pname = p.get("name", "input")
                    ptype = _gradio_type_to_litegraph(p.get("type", "any"))
                    if pname not in seen_in:
                        in_ports.append([pname, ptype])
                        seen_in.add(pname)
                for r in ep.get("returns", []):
                    rname = r.get("name", "output")
                    rtype = _gradio_type_to_litegraph(r.get("type", "any"))
                    if rname not in seen_out:
                        out_ports.append([rname, rtype])
                        seen_out.add(rname)

    if not in_ports:
        in_ports = [["input", "any"]]
    if not out_ports:
        out_ports = [["output", "any"]]

    return {"in": in_ports, "out": out_ports}


def _read_service_ports() -> dict:
    if SERVICE_PORTS_FILE.exists():
        return json.loads(SERVICE_PORTS_FILE.read_text())
    return {}


@router.post("/services/{name}/autowire", tags=["Services"])
async def autowire_service(name: str):
    if not CONTAINER_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"Invalid service name: must match {CONTAINER_NAME_RE.pattern}")
    services = services_cfg()
    if name not in services:
        raise HTTPException(status_code=404, detail=f"Service '{name}' not registered")

    if not SCANS_CFG_FILE.exists():
        raise HTTPException(status_code=404, detail=f"No scans found. Run POST /services/{name}/scan first.")

    scans = json.loads(SCANS_CFG_FILE.read_text())
    if name not in scans:
        raise HTTPException(status_code=404, detail=f"No scan result for '{name}'. Run POST /services/{name}/scan first.")

    scan = scans[name]

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

    ports = _generate_ports_from_scan(scan)

    entry = {
        "in": ports["in"],
        "out": ports["out"],
        "auto_wired": True,
        "wired_at": datetime.now().isoformat(),
        "source_capabilities": [
            (c if isinstance(c, str) else c.get("type", ""))
            for c in scan.get("capabilities", [])
        ],
    }

    async with config_lock:
        all_ports = _read_service_ports()
        all_ports[name] = entry
        atomic_write(SERVICE_PORTS_FILE, json.dumps(all_ports, indent=2))

    result = {"service": name, "ports": entry}
    if warning:
        result["warning"] = warning
    return result


@router.get("/services/{name}/ports", tags=["Services"])
def get_service_ports(name: str):
    if not CONTAINER_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"Invalid service name: must match {CONTAINER_NAME_RE.pattern}")
    services = services_cfg()
    if name not in services:
        raise HTTPException(status_code=404, detail=f"Service '{name}' not registered")

    all_ports = _read_service_ports()
    if name in all_ports:
        return {"service": name, "source": "auto_wired", "ports": all_ports[name]}

    if name in _DEFAULT_SERVICE_PORTS:
        return {"service": name, "source": "default", "ports": {
            "in": _DEFAULT_SERVICE_PORTS[name]["in"],
            "out": _DEFAULT_SERVICE_PORTS[name]["out"],
            "auto_wired": False,
        }}

    return {"service": name, "source": "generic", "ports": {
        "in": [["input", "any"]],
        "out": [["output", "any"]],
        "auto_wired": False,
    }}


@router.delete("/services/{name}/autowire", tags=["Services"])
async def delete_autowire(name: str):
    if not CONTAINER_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"Invalid service name: must match {CONTAINER_NAME_RE.pattern}")
    services = services_cfg()
    if name not in services:
        raise HTTPException(status_code=404, detail=f"Service '{name}' not registered")

    async with config_lock:
        all_ports = _read_service_ports()
        if name not in all_ports:
            return {"service": name, "deleted": False, "detail": "No auto-wired ports to remove"}
        del all_ports[name]
        atomic_write(SERVICE_PORTS_FILE, json.dumps(all_ports, indent=2))

    return {"service": name, "deleted": True, "reverted_to": "default"}


# ── Ollama ───────────────────────────────────────────────────────────────────

@router.get("/services/ollama/models", tags=["Ollama"])
async def ollama_models():
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
        models = r.json().get("models", [])
        return [{"name": m["name"], "size_gb": round(m["size"] / 1e9, 1)} for m in models]
    except Exception as e:
        print(f"[oLLMo] ollama_models error: {e}")
        return {"error": "Failed to list Ollama models"}


@router.post("/services/ollama/models/{name}/load", tags=["Ollama"])
async def load_ollama_model(name: str):
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            await client.post(f"{OLLAMA_URL}/api/generate",
                json={"model": name, "prompt": "", "stream": False})
        return {"loaded": name}
    except Exception as e:
        print(f"[oLLMo] load_ollama_model error: {e}")
        return {"error": "Failed to load model"}


@router.post("/services/ollama/models/pull", tags=["Ollama"])
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


@router.delete("/services/ollama/models/{name}", tags=["Ollama"])
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


# ── RVC ──────────────────────────────────────────────────────────────────────

@router.get("/services/rvc/models", tags=["RVC"])
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


@router.post("/services/rvc/models/{name}/activate", tags=["RVC"])
async def activate_rvc_model(name: str):
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(f"{RVC_GRADIO}/run/infer_refresh", json={"data": []})
            r = await client.post(
                f"{RVC_GRADIO}/run/infer_change_voice",
                json={"data": [f"{name}.pth", 0.33, 0.33]},
            )
        result = r.json()
        index_path = result.get("data", [None, None, None, None])[-1] or ""
        return {"activated": name, "index": index_path}
    except Exception as e:
        print(f"[oLLMo] activate_rvc_model error: {e}")
        return {"error": "Failed to activate RVC model"}


# ── ComfyUI ──────────────────────────────────────────────────────────────────

@router.get("/services/comfyui/workflows", tags=["ComfyUI"])
def comfyui_workflows():
    workflows_path = COMFYUI_USER_PATH / "default" / "workflows"
    if not workflows_path.exists():
        return []
    files = sorted(f for f in workflows_path.glob("*.json") if not f.stem.startswith("."))
    return [{"name": f.stem, "file": f.name} for f in files]
