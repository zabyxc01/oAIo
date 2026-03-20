"""
Knowledge client — queries Open WebUI's RAG via its HTTP API.

Open WebUI stores embedded documents in ChromaDB, queryable via:
  POST /api/v1/retrieval/query/collection
    body: {"collection_names": [...], "query": "...", "k": N}
    returns: {"documents": [[...]], "distances": [[...]], "metadatas": [[...]]}

Knowledge bases listed via:
  GET /api/v1/knowledge/
    returns: {"items": [{id, name, data: {file_ids: [...]}}]}

Usage:
    client = KnowledgeClient(api_key="sk-...")
    results = await client.query("what is the probe scan resolution?")
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx

_SERVICES_FILE = Path(__file__).parent.parent.parent / "config" / "services.json"


def _get_webui_url() -> str:
    """Resolve Open WebUI's internal URL for container-to-container access.

    services.json stores the host-mapped port (3000), but inside Docker
    Open WebUI listens on 8080. We always use the internal port.
    """
    return "http://open-webui:8080"


@dataclass
class KnowledgeResult:
    """A single RAG result from the knowledge store."""
    text: str
    source: str = ""
    score: float = 0.0
    metadata: dict = field(default_factory=dict)


class KnowledgeClient:
    """Queries Open WebUI's retrieval API.

    Uses the collection-based query endpoint which searches across
    knowledge base collections stored in ChromaDB.
    """

    def __init__(self, api_key: str = "", base_url: str = ""):
        self.base_url = base_url or _get_webui_url()
        self.api_key = api_key
        self._client: httpx.AsyncClient | None = None
        self._kb_cache: list[dict] | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=15.0,
            )
        return self._client

    async def _get_all_collection_names(self) -> list[str]:
        """Get collection names for all knowledge bases.

        Each knowledge base has an ID that doubles as a collection name.
        Files within knowledge bases use 'file-{file_id}' as collection names.
        We query using the knowledge base IDs which aggregate all files.
        """
        kbs = await self.list_knowledge_bases()
        return [kb["id"] for kb in kbs if kb.get("id")]

    async def query(
        self,
        text: str,
        collection_name: str = "",
        n_results: int = 5,
    ) -> list[KnowledgeResult]:
        """Query the knowledge store for relevant documents.

        Args:
            text: the query string
            collection_name: specific collection/KB ID (empty = search all)
            n_results: max results to return

        Returns:
            List of KnowledgeResult with text, source, score.
        """
        client = await self._ensure_client()

        # Determine which collections to search
        if collection_name:
            collection_names = [collection_name]
        else:
            collection_names = await self._get_all_collection_names()
            if not collection_names:
                return []

        payload = {
            "collection_names": collection_names,
            "query": text,
            "k": n_results,
        }

        try:
            r = await client.post(
                "/api/v1/retrieval/query/collection",
                json=payload,
            )
            if r.status_code == 401:
                print("[knowledge] auth failed — set webui_api_key in companion config")
                return []
            if r.status_code == 404:
                print("[knowledge] retrieval endpoint not found — check Open WebUI version")
                return []
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            print(f"[knowledge] query failed: {e}")
            return []

        results = []

        # Response shape: {"documents": [[str, ...]], "distances": [[float, ...]], "metadatas": [[dict, ...]]}
        documents = data.get("documents", [])
        distances = data.get("distances", [])
        metadatas = data.get("metadatas", [])

        for i, doc_list in enumerate(documents):
            dist_list = distances[i] if i < len(distances) else []
            meta_list = metadatas[i] if i < len(metadatas) else []

            for j, doc_text in enumerate(doc_list):
                if not doc_text:
                    continue
                distance = dist_list[j] if j < len(dist_list) else 0.0
                meta = meta_list[j] if j < len(meta_list) else {}
                results.append(KnowledgeResult(
                    text=doc_text,
                    source=meta.get("source", meta.get("name", "")),
                    score=distance,
                    metadata=meta,
                ))

        # Sort by distance (lower = more relevant) and limit
        results.sort(key=lambda r: r.score)
        # Filter out weak matches — distance > 1.0 is usually irrelevant
        # Tighter threshold prevents casual conversation from pulling random docs
        results = [r for r in results if r.score < 1.0]
        return results[:n_results]

    async def list_knowledge_bases(self) -> list[dict]:
        """List available knowledge bases in Open WebUI."""
        client = await self._ensure_client()
        try:
            r = await client.get("/api/v1/knowledge/")
            if r.status_code != 200:
                return []
            data = r.json()
            items = data if isinstance(data, list) else data.get("items", [])
            return items
        except Exception as e:
            print(f"[knowledge] list failed: {e}")
            return []

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
