"""
Graph engine routes — state management, discovery, nodes, edges, validation.
"""
from fastapi import APIRouter, HTTPException

from api.shared import (
    OLLAMA_URL,
    make_graph, make_edge, save_graph, load_graph, list_graphs, delete_graph,
    validate_graph, add_node, remove_node, add_edge, remove_edge,
    discover_all, discover_service, discover_ollama_models,
    generate_default_graph, discover_service_dirs,
    route_manager,
)

router = APIRouter()


@router.get("/graph/states", tags=["Graph"])
def graph_list_states():
    return list_graphs()


@router.post("/graph/states", tags=["Graph"])
async def graph_create_state(body: dict):
    name = body.get("name", "Untitled")
    graph = make_graph(name=name)
    save_graph(graph)
    return graph


@router.get("/graph/states/{graph_id}", tags=["Graph"])
def graph_get_state(graph_id: str):
    graph = load_graph(graph_id)
    if not graph:
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    return graph


@router.put("/graph/states/{graph_id}", tags=["Graph"])
async def graph_update_state(graph_id: str, body: dict):
    existing = load_graph(graph_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    body["id"] = graph_id
    body["created_at"] = existing.get("created_at", "")
    save_graph(body)
    return body


@router.delete("/graph/states/{graph_id}", tags=["Graph"])
async def graph_delete_state(graph_id: str):
    if delete_graph(graph_id):
        return {"deleted": graph_id}
    raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")


@router.post("/graph/discover", tags=["Graph"])
def graph_discover():
    nodes = discover_all()
    return {"nodes": nodes, "count": len(nodes)}


@router.post("/graph/discover/{service_name}", tags=["Graph"])
def graph_discover_service(service_name: str):
    node = discover_service(service_name)
    if not node:
        raise HTTPException(status_code=404, detail=f"Service '{service_name}' not found")
    return node


@router.post("/graph/discover/ollama/models", tags=["Graph"])
def graph_discover_ollama_models():
    plugins = discover_ollama_models(OLLAMA_URL)
    return {"plugins": plugins, "count": len(plugins)}


@router.get("/graph/discover/{service_name}/dirs", tags=["Graph"])
def graph_discover_dirs(service_name: str):
    return discover_service_dirs(service_name)


@router.post("/graph/generate-default", tags=["Graph"])
async def graph_generate_default(body: dict = None):
    name = (body or {}).get("name", "Default")
    graph = generate_default_graph(name=name)
    save_graph(graph)
    return graph


@router.get("/graph/nodes/{graph_id}", tags=["Graph"])
def graph_get_nodes(graph_id: str):
    graph = load_graph(graph_id)
    if not graph:
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    return graph.get("nodes", {})


@router.post("/graph/nodes/{graph_id}", tags=["Graph"])
async def graph_add_node(graph_id: str, body: dict):
    graph = load_graph(graph_id)
    if not graph:
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    add_node(graph, body)
    save_graph(graph)
    return {"added": body.get("id")}


@router.delete("/graph/nodes/{graph_id}/{node_id}", tags=["Graph"])
async def graph_remove_node(graph_id: str, node_id: str):
    graph = load_graph(graph_id)
    if not graph:
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    remove_node(graph, node_id)
    save_graph(graph)
    return {"removed": node_id}


@router.get("/graph/edges/{graph_id}", tags=["Graph"])
def graph_get_edges(graph_id: str):
    graph = load_graph(graph_id)
    if not graph:
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    return graph.get("edges", {})


@router.post("/graph/edges/{graph_id}", tags=["Graph"])
async def graph_add_edge(graph_id: str, body: dict):
    graph = load_graph(graph_id)
    if not graph:
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    edge = make_edge(
        source_port_id=body.get("source_port", ""),
        target_port_id=body.get("target_port", ""),
        sync_mode=body.get("sync_mode", "on-demand"),
        data_format=body.get("data_format"),
    )
    graph, errors = add_edge(graph, edge)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    save_graph(graph)
    return edge


@router.delete("/graph/edges/{graph_id}/{edge_id}", tags=["Graph"])
async def graph_remove_edge(graph_id: str, edge_id: str):
    graph = load_graph(graph_id)
    if not graph:
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    remove_edge(graph, edge_id)
    save_graph(graph)
    return {"removed": edge_id}


@router.post("/graph/validate/{graph_id}", tags=["Graph"])
def graph_validate(graph_id: str):
    graph = load_graph(graph_id)
    if not graph:
        raise HTTPException(status_code=404, detail=f"Graph '{graph_id}' not found")
    errors = validate_graph(graph)
    return {"valid": len(errors) == 0, "errors": errors}


@router.get("/graph/router/status", tags=["Graph"])
def graph_router_status():
    return route_manager.get_status()
