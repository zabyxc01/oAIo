"""
Auto-discovery — scans running Docker containers and generates graph nodes
with typed plugins and ports.

For known services (ollama, kokoro-tts, etc.), generates rich plugin definitions
from scan data and capability mappings. For unknown services, generates a basic
node that the user can configure manually.

Reads from existing config/scans.json and config/service_ports.json when available,
and can trigger live scans for fresh data.
"""
import json
import logging
from pathlib import Path
from .graph import make_node, make_plugin, make_port

log = logging.getLogger("discovery")

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"
SCANS_FILE = CONFIG_DIR / "scans.json"
SERVICE_PORTS_FILE = CONFIG_DIR / "service_ports.json"
SERVICES_FILE = CONFIG_DIR / "services.json"

# ── Capability → Plugin mapping ──────────────────────────────────────────────
# Maps capability names (from scans) to plugin definitions with typed ports.
# This is the graph-native evolution of _CAPABILITY_PORTS in main.py.

CAPABILITY_PLUGINS = {
    "chat": {
        "name": "LLM Chat",
        "in_ports": [("prompt", "text")],
        "out_ports": [("response", "text")],
    },
    "embeddings": {
        "name": "Embeddings",
        "in_ports": [("text", "text")],
        "out_ports": [("embedding", "embedding")],
    },
    "tts": {
        "name": "Text-to-Speech",
        "in_ports": [("text", "text")],
        "out_ports": [("audio", "audio")],
    },
    "stt": {
        "name": "Speech-to-Text",
        "in_ports": [("audio", "audio")],
        "out_ports": [("text", "text")],
    },
    "voice_conversion": {
        "name": "Voice Conversion",
        "in_ports": [("audio", "audio")],
        "out_ports": [("audio", "audio")],
    },
    "voice_clone": {
        "name": "Voice Cloning",
        "in_ports": [("text", "text"), ("ref_audio", "audio")],
        "out_ports": [("audio", "audio")],
    },
    "image_gen": {
        "name": "Image Generation",
        "in_ports": [("prompt", "text")],
        "out_ports": [("image", "image")],
    },
    "text_gen": {
        "name": "Text Generation",
        "in_ports": [("prompt", "text")],
        "out_ports": [("text", "text")],
    },
}

# ── Known service overrides ──────────────────────────────────────────────────
# For services where scans don't capture the full picture, we define
# the plugins explicitly. These take priority over scan-derived plugins.

KNOWN_SERVICE_PLUGINS = {
    "ollama": [
        {"capability": "chat", "name": "LLM Chat"},
        {"capability": "embeddings", "name": "Embeddings"},
    ],
    "kokoro-tts": [
        {"capability": "tts", "name": "Kokoro TTS"},
    ],
    "indextts": [
        {"capability": "voice_clone", "name": "IndexTTS Voice Clone"},
        {"capability": "tts", "name": "IndexTTS TTS"},
    ],
    "faster-whisper": [
        {"capability": "stt", "name": "Speech-to-Text"},
    ],
    "rvc": [
        {"capability": "voice_conversion", "name": "RVC Voice Conversion"},
        {"capability": "tts", "name": "RVC TTS Proxy"},
    ],
    "f5-tts": [
        {"capability": "voice_clone", "name": "F5 Voice Cloning"},
        {"capability": "tts", "name": "F5 TTS"},
    ],
    "styletts2": [
        {"capability": "tts", "name": "StyleTTS2"},
        {"capability": "voice_clone", "name": "StyleTTS2 Voice Clone"},
    ],
    "comfyui": [
        {"capability": "image_gen", "name": "ComfyUI Image Gen"},
    ],
    "open-webui": [
        {"capability": "chat", "name": "Open WebUI Chat"},
    ],
}


# ── Core discovery functions ─────────────────────────────────────────────────

def _load_scans() -> dict:
    """Load cached scan results."""
    if SCANS_FILE.exists():
        try:
            return json.loads(SCANS_FILE.read_text())
        except Exception:
            pass
    return {}


def _load_services() -> dict:
    """Load services config."""
    if SERVICES_FILE.exists():
        try:
            return json.loads(SERVICES_FILE.read_text()).get("services", {})
        except Exception:
            pass
    return {}


def _build_plugins_for_service(service_name: str, scan_data: dict = None) -> list[dict]:
    """Build plugin definitions for a service.

    Uses KNOWN_SERVICE_PLUGINS if available, falls back to scan-derived
    capabilities, and finally generates a basic pass-through plugin.
    """
    plugins = []

    # Priority 1: known service overrides
    if service_name in KNOWN_SERVICE_PLUGINS:
        for override in KNOWN_SERVICE_PLUGINS[service_name]:
            cap = override["capability"]
            cap_def = CAPABILITY_PLUGINS.get(cap)
            if cap_def:
                plugin = make_plugin(
                    node_id=service_name,
                    name=override.get("name", cap_def["name"]),
                    capability=cap,
                    in_ports=cap_def["in_ports"],
                    out_ports=cap_def["out_ports"],
                )
                plugins.append(plugin)
        return plugins

    # Priority 2: scan-derived capabilities
    if scan_data:
        capabilities = scan_data.get("capabilities", [])
        for cap in capabilities:
            cap_name = cap if isinstance(cap, str) else cap.get("type", "")
            cap_def = CAPABILITY_PLUGINS.get(cap_name)
            if cap_def:
                plugin = make_plugin(
                    node_id=service_name,
                    name=cap_def["name"],
                    capability=cap_name,
                    in_ports=cap_def["in_ports"],
                    out_ports=cap_def["out_ports"],
                )
                plugins.append(plugin)

    # Priority 3: basic pass-through if nothing found
    if not plugins:
        plugins.append(make_plugin(
            node_id=service_name,
            name="Service",
            capability="generic",
            in_ports=[("input", "any")],
            out_ports=[("output", "any")],
        ))

    return plugins


def discover_all() -> dict[str, dict]:
    """Discover all registered services and generate graph nodes.

    Returns a dict of {service_name: Node dict}.
    """
    services = _load_services()
    scans = _load_scans()
    nodes = {}

    for svc_name, svc_cfg in services.items():
        scan_data = scans.get(svc_name)
        plugins = _build_plugins_for_service(svc_name, scan_data)

        display_name = svc_cfg.get("description", svc_name)
        # Use a clean display name
        if display_name == svc_name:
            display_name = svc_name.replace("-", " ").title()

        node = make_node(
            service_name=svc_name,
            display_name=display_name,
            service_cfg=svc_cfg,
            plugins=plugins,
        )
        nodes[svc_name] = node

    return nodes


def discover_service(service_name: str) -> dict | None:
    """Discover a single service and return its Node dict."""
    services = _load_services()
    svc_cfg = services.get(service_name)
    if not svc_cfg:
        return None

    scans = _load_scans()
    scan_data = scans.get(service_name)
    plugins = _build_plugins_for_service(service_name, scan_data)

    display_name = svc_cfg.get("description", service_name)
    if display_name == service_name:
        display_name = service_name.replace("-", " ").title()

    return make_node(
        service_name=service_name,
        display_name=display_name,
        service_cfg=svc_cfg,
        plugins=plugins,
    )


def discover_ollama_models(ollama_url: str = "http://ollama:11434") -> list[dict]:
    """Query Ollama for loaded models and generate plugin instances.

    Each loaded model becomes a separate plugin with its own ports,
    so they can be independently wired in the graph.
    """
    import httpx

    plugins = []
    try:
        # Get loaded models
        resp = httpx.get(f"{ollama_url}/api/ps", timeout=5)
        if resp.status_code != 200:
            return plugins

        loaded = resp.json().get("models", [])
        for model in loaded:
            model_name = model.get("name", "unknown")
            short_name = model_name.replace(":latest", "")

            # Each loaded model gets its own chat plugin
            plugin = make_plugin(
                node_id="ollama",
                name=f"{short_name} Chat",
                capability=f"chat:{short_name}",
                in_ports=[("prompt", "text")],
                out_ports=[("response", "text")],
            )
            # Tag with model name for routing
            plugin["model"] = model_name
            plugin["vram_gb"] = round(model.get("size_vram", 0) / 1e9, 2)
            plugins.append(plugin)

    except Exception as e:
        log.warning("Failed to discover Ollama models: %s", e)

    return plugins


def generate_default_graph(name: str = "Default") -> dict:
    """Generate a complete graph from current services config and scans.

    This is the bootstrap function — creates a graph that mirrors
    the current system state without changing anything.
    """
    from .graph import make_graph

    nodes = discover_all()
    graph = make_graph(name=name, nodes=nodes)
    return graph
