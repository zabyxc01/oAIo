"""
Data routing engine — moves data between services along graph edges.

This is what M3 (multi-model pipelines) becomes. Reads edges from the
active graph and routes data between output ports and input ports.

Sync modes:
  on-demand  — data queued until consumer pulls it
  auto       — data forwarded immediately via HTTP to target endpoint
  realtime   — streaming (WebSocket/SSE, future)
"""
import asyncio
import json
import logging
import time
import httpx
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger("router")

# ── Data packet ──────────────────────────────────────────────────────────────

@dataclass
class DataPacket:
    """A unit of data flowing through the graph."""
    source_port: str
    data: bytes | str
    content_type: str = "text/plain"
    timestamp: float = 0.0
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()


# ── Route manager ────────────────────────────────────────────────────────────

class RouteManager:
    """Manages data flow along graph edges.

    Maintains queues for on-demand delivery and handles auto-delivery
    by forwarding data via HTTP to target service endpoints.
    """

    def __init__(self):
        self._graph = None
        self._edges_by_source = {}      # source_port_id -> [edge, ...]
        self._queues = {}               # target_port_id -> deque of DataPacket
        self._stats = {
            "packets_routed": 0,
            "packets_queued": 0,
            "packets_delivered": 0,
            "errors": 0,
        }
        self._service_urls = {}         # service_name -> base URL
        self._port_index = {}           # port_id -> port dict
        self._node_index = {}           # port_id -> node dict (for URL resolution)

    def set_active_graph(self, graph: dict, service_urls: dict = None):
        """Rebuild routing tables from a graph state.

        Args:
            graph: GraphState dict
            service_urls: {service_name: "http://host:port"} for HTTP delivery
        """
        self._graph = graph
        self._service_urls = service_urls or {}
        self._edges_by_source = {}
        self._queues = {}
        self._port_index = {}
        self._node_index = {}

        # Index all ports for lookup
        for node in graph.get("nodes", {}).values():
            for plugin in node.get("plugins", []):
                for port in plugin.get("ports", []):
                    self._port_index[port["id"]] = port
                    self._node_index[port["id"]] = node

        # Index edges by source port for fast routing
        for edge in graph.get("edges", {}).values():
            src = edge.get("source_port", "")
            if src not in self._edges_by_source:
                self._edges_by_source[src] = []
            self._edges_by_source[src].append(edge)

            # Create queue for on-demand targets
            if edge.get("sync_mode") == "on-demand":
                tgt = edge.get("target_port", "")
                if tgt not in self._queues:
                    self._queues[tgt] = deque(maxlen=100)

        log.info(
            "Router loaded: %d edges, %d on-demand queues",
            len(graph.get("edges", {})),
            len(self._queues),
        )

    def clear(self):
        """Clear all routing state."""
        self._graph = None
        self._edges_by_source = {}
        self._queues = {}
        self._port_index = {}
        self._node_index = {}

    async def emit(self, source_port_id: str, data: bytes | str,
                   content_type: str = "text/plain", metadata: dict = None):
        """Emit data from an output port. Routes to all connected inputs.

        Called when a service produces output that should flow through the graph.
        """
        edges = self._edges_by_source.get(source_port_id, [])
        if not edges:
            return

        packet = DataPacket(
            source_port=source_port_id,
            data=data,
            content_type=content_type,
            metadata=metadata or {},
        )

        self._stats["packets_routed"] += 1

        for edge in edges:
            target_port_id = edge.get("target_port", "")
            sync_mode = edge.get("sync_mode", "on-demand")

            try:
                if sync_mode == "on-demand":
                    await self._queue_packet(target_port_id, packet)
                elif sync_mode == "auto":
                    await self._deliver_packet(target_port_id, packet)
                elif sync_mode == "realtime":
                    # Future: WebSocket/SSE streaming
                    log.warning("Realtime sync not yet implemented, falling back to auto")
                    await self._deliver_packet(target_port_id, packet)
            except Exception as e:
                self._stats["errors"] += 1
                log.error("Route error %s -> %s: %s", source_port_id, target_port_id, e)

    async def _queue_packet(self, target_port_id: str, packet: DataPacket):
        """Queue a packet for on-demand retrieval."""
        if target_port_id not in self._queues:
            self._queues[target_port_id] = deque(maxlen=100)
        self._queues[target_port_id].append(packet)
        self._stats["packets_queued"] += 1

    async def _deliver_packet(self, target_port_id: str, packet: DataPacket):
        """Deliver a packet via HTTP to the target service endpoint."""
        port = self._port_index.get(target_port_id)
        node = self._node_index.get(target_port_id)
        if not port or not node:
            log.error("Cannot deliver to unknown port: %s", target_port_id)
            return

        endpoint = port.get("endpoint")
        if not endpoint:
            # No endpoint defined — queue instead
            await self._queue_packet(target_port_id, packet)
            return

        service_name = node.get("service", "")
        base_url = self._service_urls.get(service_name)
        if not base_url:
            log.error("No URL for service '%s', cannot deliver", service_name)
            return

        url = f"{base_url}{endpoint.get('path', '')}"
        method = endpoint.get("method", "POST").upper()

        async with httpx.AsyncClient(timeout=30) as client:
            if isinstance(packet.data, str):
                # JSON payload
                try:
                    body = json.loads(packet.data)
                except (json.JSONDecodeError, TypeError):
                    body = {"text": packet.data}

                resp = await client.request(
                    method, url,
                    json=body,
                    headers={"Content-Type": "application/json"},
                )
            else:
                # Binary payload (audio, image, etc.)
                resp = await client.request(
                    method, url,
                    content=packet.data,
                    headers={"Content-Type": packet.content_type},
                )

            if resp.status_code < 400:
                self._stats["packets_delivered"] += 1
                log.debug("Delivered to %s: %d bytes -> %d", url, len(packet.data), resp.status_code)
            else:
                self._stats["errors"] += 1
                log.error("Delivery failed %s: %d %s", url, resp.status_code, resp.text[:200])

    def pull(self, target_port_id: str) -> DataPacket | None:
        """Pull the next queued packet for an on-demand target port."""
        queue = self._queues.get(target_port_id)
        if queue:
            return queue.popleft() if queue else None
        return None

    def peek(self, target_port_id: str) -> int:
        """Return the number of queued packets for a target port."""
        queue = self._queues.get(target_port_id)
        return len(queue) if queue else 0

    def get_status(self) -> dict:
        """Return router status — stats, queue depths, edge count."""
        queue_depths = {
            port_id: len(q) for port_id, q in self._queues.items() if q
        }
        return {
            "active": self._graph is not None,
            "graph_id": self._graph.get("id") if self._graph else None,
            "edge_count": sum(len(edges) for edges in self._edges_by_source.values()),
            "queue_depths": queue_depths,
            "stats": dict(self._stats),
        }


# ── Module-level singleton ───────────────────────────────────────────────────
# The main.py process creates and manages this instance.
route_manager = RouteManager()
