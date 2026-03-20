"""Vision memory store — persistent screen descriptions for "what was I doing" queries.

Storage: persona_state/{persona_id}_vision.json
Data: [{"description": str, "activity": str, "ts": float}] — capped at 200 FIFO
"""
import json
import time
from pathlib import Path

_STATE_DIR = Path(__file__).parent / "persona_state"
_MAX_ENTRIES = 200


class VisionMemoryStore:
    """Persistent vision descriptions — searchable history of screen observations."""

    def __init__(self, persona_id: str):
        self.persona_id = persona_id
        self._entries: list[dict] = []
        self._load()

    def _file(self) -> Path:
        _STATE_DIR.mkdir(exist_ok=True)
        return _STATE_DIR / f"{self.persona_id}_vision.json"

    def _load(self) -> None:
        f = self._file()
        if f.exists():
            try:
                self._entries = json.loads(f.read_text())
            except Exception:
                self._entries = []

    def _save(self) -> None:
        f = self._file()
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._entries, indent=2))
        tmp.rename(f)

    def add(self, description: str, activity: str = "") -> None:
        self._entries.append({
            "description": description,
            "activity": activity,
            "ts": time.time(),
        })
        # FIFO cap
        if len(self._entries) > _MAX_ENTRIES:
            self._entries = self._entries[-_MAX_ENTRIES:]
        self._save()

    def search(self, query: str, max_results: int = 3) -> list[dict]:
        query_lower = query.lower()
        matches = []
        for entry in reversed(self._entries):  # newest first
            if (query_lower in entry["description"].lower()
                    or query_lower in entry.get("activity", "").lower()):
                matches.append(entry)
                if len(matches) >= max_results:
                    break
        return matches

    def list_recent(self, n: int = 5) -> list[dict]:
        return self._entries[-n:]
