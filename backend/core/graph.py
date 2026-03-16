"""
Graph engine — nodes, typed ports, edges, persistence.

The authoritative data model for oAIo's service topology. Nodes represent
services (containers). Plugins represent capabilities. Ports are typed I/O
points. Edges define data flow between ports.

Graph states are saved as JSON files in config/graphs/.
Modes reference graph states for deployment.
"""
import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

GRAPHS_DIR = Path(__file__).parent.parent.parent / "config" / "graphs"
GRAPHS_DIR.mkdir(exist_ok=True)

# ── Type system ──────────────────────────────────────────────────────────────
# Valid port data types. Connections are only allowed between compatible types.
VALID_DATA_TYPES = {"text", "audio", "image", "embedding", "json", "file-path", "number", "video", "any"}

# Type compatibility matrix — source type -> set of compatible target types
_TYPE_COMPAT = {
    "text":      {"text", "json", "any"},
    "audio":     {"audio", "file-path", "any"},
    "image":     {"image", "file-path", "any"},
    "embedding": {"embedding", "json", "any"},
    "json":      {"json", "text", "any"},
    "file-path": {"file-path", "text", "any"},
    "number":    {"number", "text", "json", "any"},
    "video":     {"video", "file-path", "any"},
    "any":       VALID_DATA_TYPES,
}

VALID_SYNC_MODES = {"on-demand", "auto", "realtime"}


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class PortEndpoint:
    """Maps a port to a specific API endpoint on the service."""
    method: str = "POST"
    path: str = ""
    field: str = ""
    content_type: str = "application/json"


@dataclass
class Port:
    """A typed I/O point on a plugin."""
    id: str = ""
    name: str = ""
    direction: str = "in"           # "in" | "out"
    data_type: str = "text"         # from VALID_DATA_TYPES
    endpoint: Optional[dict] = None # PortEndpoint as dict, or None


@dataclass
class Plugin:
    """A capability a service exposes (e.g. 'LLM Chat', 'TTS', 'Voice Conversion')."""
    id: str = ""
    name: str = ""
    capability: str = ""            # capability key (chat, tts, embeddings, etc.)
    ports: list = field(default_factory=list)  # list of Port dicts


@dataclass
class Node:
    """A service in the graph. Maps to a Docker container."""
    id: str = ""
    name: str = ""
    host: str = "local"             # "local" or fleet node ID
    service: str = ""               # services.json key
    plugins: list = field(default_factory=list)  # list of Plugin dicts
    meta: dict = field(default_factory=dict)     # carries services.json fields


@dataclass
class Edge:
    """A connection between an output port and an input port."""
    id: str = ""
    source_port: str = ""           # port ID (e.g. "ollama:chat:response_out")
    target_port: str = ""           # port ID (e.g. "indextts:tts:text_in")
    sync_mode: str = "on-demand"    # from VALID_SYNC_MODES
    transform: Optional[dict] = None
    data_format: Optional[str] = None


@dataclass
class GraphState:
    """Complete graph — all nodes, edges, and visual layout data."""
    id: str = ""
    name: str = ""
    created_at: str = ""
    updated_at: str = ""
    nodes: dict = field(default_factory=dict)    # node_id -> Node dict
    edges: dict = field(default_factory=dict)    # edge_id -> Edge dict
    litegraph: dict = field(default_factory=dict) # visual-only data for CONFIG tab


# ── Helper constructors ──────────────────────────────────────────────────────

def make_port(node_id: str, plugin_id: str, name: str, direction: str,
              data_type: str = "text", endpoint: dict = None) -> dict:
    """Create a Port dict with a properly namespaced ID."""
    port_id = f"{node_id}:{plugin_id}:{name}_{direction}"
    return {
        "id": port_id,
        "name": name,
        "direction": direction,
        "data_type": data_type,
        "endpoint": endpoint,
    }


def make_plugin(node_id: str, name: str, capability: str,
                in_ports: list = None, out_ports: list = None) -> dict:
    """Create a Plugin dict with ports."""
    plugin_id = capability
    ports = []
    for pname, ptype in (in_ports or []):
        ports.append(make_port(node_id, plugin_id, pname, "in", ptype))
    for pname, ptype in (out_ports or []):
        ports.append(make_port(node_id, plugin_id, pname, "out", ptype))
    return {
        "id": f"{node_id}:{plugin_id}",
        "name": name,
        "capability": capability,
        "ports": ports,
    }


def make_node(service_name: str, display_name: str, service_cfg: dict,
              plugins: list = None, host: str = "local") -> dict:
    """Create a Node dict from a services.json entry."""
    return {
        "id": service_name,
        "name": display_name,
        "host": host,
        "service": service_name,
        "plugins": plugins or [],
        "meta": {
            "container": service_cfg.get("container", service_name),
            "port": service_cfg.get("port", 0),
            "vram_est_gb": service_cfg.get("vram_est_gb", 0),
            "ram_est_gb": service_cfg.get("ram_est_gb", 0),
            "priority": service_cfg.get("priority", 50),
            "limit_mode": service_cfg.get("limit_mode", "hard"),
            "auto_restore": service_cfg.get("auto_restore", False),
            "group": service_cfg.get("group", ""),
            "memory_mode": service_cfg.get("memory_mode", "vram"),
            "boot_with_system": service_cfg.get("boot_with_system", False),
            "description": service_cfg.get("description", ""),
        },
    }


def make_edge(source_port_id: str, target_port_id: str,
              sync_mode: str = "on-demand", data_format: str = None) -> dict:
    """Create an Edge dict."""
    edge_id = f"e_{uuid.uuid4().hex[:8]}"
    return {
        "id": edge_id,
        "source_port": source_port_id,
        "target_port": target_port_id,
        "sync_mode": sync_mode,
        "transform": None,
        "data_format": data_format,
    }


def make_graph(name: str, nodes: dict = None, edges: dict = None) -> dict:
    """Create a new GraphState dict."""
    now = datetime.now(timezone.utc).isoformat()
    graph_id = f"gs_{uuid.uuid4().hex[:8]}"
    return {
        "id": graph_id,
        "name": name,
        "created_at": now,
        "updated_at": now,
        "nodes": nodes or {},
        "edges": edges or {},
        "litegraph": {"positions": {}, "groups": [], "config": {}},
    }


# ── Persistence ──────────────────────────────────────────────────────────────

def _graph_path(graph_id: str) -> Path:
    """Return the file path for a graph state."""
    # Sanitize ID to prevent path traversal
    safe_id = "".join(c for c in graph_id if c.isalnum() or c in "_-")
    return GRAPHS_DIR / f"{safe_id}.json"


def save_graph(state: dict) -> dict:
    """Persist a graph state to disk. Atomic write."""
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    path = _graph_path(state["id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)
    return state


def load_graph(graph_id: str) -> dict | None:
    """Load a graph state from disk."""
    path = _graph_path(graph_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def list_graphs() -> list[dict]:
    """List all saved graph states (summary only)."""
    results = []
    for f in sorted(GRAPHS_DIR.glob("gs_*.json")):
        try:
            data = json.loads(f.read_text())
            results.append({
                "id": data.get("id", f.stem),
                "name": data.get("name", ""),
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at", ""),
                "node_count": len(data.get("nodes", {})),
                "edge_count": len(data.get("edges", {})),
            })
        except Exception:
            continue
    return results


def delete_graph(graph_id: str) -> bool:
    """Delete a graph state file."""
    path = _graph_path(graph_id)
    if path.exists():
        path.unlink()
        return True
    return False


# ── Validation ───────────────────────────────────────────────────────────────

def _find_port(graph: dict, port_id: str) -> dict | None:
    """Find a port definition by ID across all nodes/plugins."""
    for node in graph.get("nodes", {}).values():
        for plugin in node.get("plugins", []):
            for port in plugin.get("ports", []):
                if port.get("id") == port_id:
                    return port
    return None


def validate_graph(graph: dict) -> list[str]:
    """Validate a graph state. Returns list of error strings (empty = valid)."""
    errors = []

    # Check nodes have required fields
    for nid, node in graph.get("nodes", {}).items():
        if not node.get("id"):
            errors.append(f"Node '{nid}' missing id")
        if not node.get("service"):
            errors.append(f"Node '{nid}' missing service")

        # Check port data types
        for plugin in node.get("plugins", []):
            for port in plugin.get("ports", []):
                if port.get("data_type") not in VALID_DATA_TYPES:
                    errors.append(f"Port '{port.get('id')}' has invalid data_type: {port.get('data_type')}")
                if port.get("direction") not in ("in", "out"):
                    errors.append(f"Port '{port.get('id')}' has invalid direction: {port.get('direction')}")

    # Check edges
    for eid, edge in graph.get("edges", {}).items():
        source = _find_port(graph, edge.get("source_port", ""))
        target = _find_port(graph, edge.get("target_port", ""))

        if not source:
            errors.append(f"Edge '{eid}': source port '{edge.get('source_port')}' not found")
            continue
        if not target:
            errors.append(f"Edge '{eid}': target port '{edge.get('target_port')}' not found")
            continue

        # Direction check
        if source.get("direction") != "out":
            errors.append(f"Edge '{eid}': source port '{source['id']}' is not an output")
        if target.get("direction") != "in":
            errors.append(f"Edge '{eid}': target port '{target['id']}' is not an input")

        # Type compatibility check
        src_type = source.get("data_type", "any")
        tgt_type = target.get("data_type", "any")
        compat = _TYPE_COMPAT.get(src_type, {"any"})
        if tgt_type not in compat:
            errors.append(
                f"Edge '{eid}': type mismatch — {src_type} (source) incompatible with {tgt_type} (target)"
            )

        # Sync mode check
        if edge.get("sync_mode") not in VALID_SYNC_MODES:
            errors.append(f"Edge '{eid}': invalid sync_mode '{edge.get('sync_mode')}'")

    return errors


# ── Graph utilities ──────────────────────────────────────────────────────────

def graph_to_services_list(graph: dict, host: str = "local") -> list[str]:
    """Extract service names from a graph state (for enforcer/mode compat).
    Only returns services assigned to the specified host.
    """
    return [
        node["service"]
        for node in graph.get("nodes", {}).values()
        if node.get("host", "local") == host and node.get("service")
    ]


def get_node_ports(graph: dict, node_id: str) -> dict:
    """Get all input and output ports for a node, grouped by plugin."""
    node = graph.get("nodes", {}).get(node_id)
    if not node:
        return {"in": [], "out": []}

    in_ports = []
    out_ports = []
    for plugin in node.get("plugins", []):
        for port in plugin.get("ports", []):
            entry = {**port, "plugin": plugin.get("name", "")}
            if port.get("direction") == "in":
                in_ports.append(entry)
            else:
                out_ports.append(entry)
    return {"in": in_ports, "out": out_ports}


def get_edges_for_node(graph: dict, node_id: str) -> list[dict]:
    """Get all edges connected to a node (in or out)."""
    prefix = f"{node_id}:"
    return [
        edge for edge in graph.get("edges", {}).values()
        if edge.get("source_port", "").startswith(prefix) or
           edge.get("target_port", "").startswith(prefix)
    ]


def get_downstream(graph: dict, port_id: str) -> list[dict]:
    """Get all edges originating from a specific output port."""
    return [
        edge for edge in graph.get("edges", {}).values()
        if edge.get("source_port") == port_id
    ]


def get_upstream(graph: dict, port_id: str) -> list[dict]:
    """Get all edges targeting a specific input port."""
    return [
        edge for edge in graph.get("edges", {}).values()
        if edge.get("target_port") == port_id
    ]


def add_node(graph: dict, node: dict) -> dict:
    """Add a node to a graph. Returns the updated graph."""
    graph["nodes"][node["id"]] = node
    return graph


def remove_node(graph: dict, node_id: str) -> dict:
    """Remove a node and all its edges from a graph."""
    graph["nodes"].pop(node_id, None)
    # Remove edges connected to this node
    prefix = f"{node_id}:"
    to_remove = [
        eid for eid, edge in graph.get("edges", {}).items()
        if edge.get("source_port", "").startswith(prefix) or
           edge.get("target_port", "").startswith(prefix)
    ]
    for eid in to_remove:
        graph["edges"].pop(eid, None)
    return graph


def add_edge(graph: dict, edge: dict) -> tuple[dict, list[str]]:
    """Add an edge to a graph with validation. Returns (graph, errors)."""
    # Validate before adding
    test_graph = {**graph, "edges": {**graph.get("edges", {}), edge["id"]: edge}}
    errors = []

    source = _find_port(test_graph, edge.get("source_port", ""))
    target = _find_port(test_graph, edge.get("target_port", ""))

    if not source:
        errors.append(f"Source port '{edge.get('source_port')}' not found")
    if not target:
        errors.append(f"Target port '{edge.get('target_port')}' not found")

    if source and target:
        if source.get("direction") != "out":
            errors.append(f"Source port '{source['id']}' is not an output")
        if target.get("direction") != "in":
            errors.append(f"Target port '{target['id']}' is not an input")

        src_type = source.get("data_type", "any")
        tgt_type = target.get("data_type", "any")
        compat = _TYPE_COMPAT.get(src_type, {"any"})
        if tgt_type not in compat:
            errors.append(f"Type mismatch: {src_type} -> {tgt_type}")

    if errors:
        return graph, errors

    graph["edges"][edge["id"]] = edge
    return graph, []


def remove_edge(graph: dict, edge_id: str) -> dict:
    """Remove an edge from a graph."""
    graph["edges"].pop(edge_id, None)
    return graph
