"""Personal notes store — user-explicit "remember this" persistent memory.

Storage: persona_state/{persona_id}_notes.json
Data: [{"key": str, "value": str, "ts": float}]
"""
import json
import time
from pathlib import Path

_STATE_DIR = Path(__file__).parent / "persona_state"


class NotesStore:
    """Persistent personal notes — user explicitly said "remember this"."""

    def __init__(self, persona_id: str):
        self.persona_id = persona_id
        self._notes: list[dict] = []
        self._load()

    def _file(self) -> Path:
        _STATE_DIR.mkdir(exist_ok=True)
        return _STATE_DIR / f"{self.persona_id}_notes.json"

    def _load(self) -> None:
        f = self._file()
        if f.exists():
            try:
                self._notes = json.loads(f.read_text())
            except Exception:
                self._notes = []

    def _save(self) -> None:
        f = self._file()
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._notes, indent=2))
        tmp.rename(f)

    def add(self, key: str, value: str) -> None:
        # Update existing key if present
        for note in self._notes:
            if note["key"].lower() == key.lower():
                note["value"] = value
                note["ts"] = time.time()
                self._save()
                return
        self._notes.append({"key": key, "value": value, "ts": time.time()})
        self._save()

    def get(self, key: str) -> str | None:
        for note in self._notes:
            if note["key"].lower() == key.lower():
                return note["value"]
        return None

    def delete(self, key: str) -> bool:
        before = len(self._notes)
        self._notes = [n for n in self._notes if n["key"].lower() != key.lower()]
        if len(self._notes) < before:
            self._save()
            return True
        return False

    def search(self, query: str, max_results: int = 5) -> list[dict]:
        query_lower = query.lower()
        scored = []
        for note in self._notes:
            # Exact key match ranked highest
            if note["key"].lower() == query_lower:
                scored.append((0, note))
            elif query_lower in note["key"].lower():
                scored.append((1, note))
            elif query_lower in note["value"].lower():
                scored.append((2, note))
        scored.sort(key=lambda x: x[0])
        return [s[1] for s in scored[:max_results]]

    def list_all(self) -> list[dict]:
        return list(self._notes)
