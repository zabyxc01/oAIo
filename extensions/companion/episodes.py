"""Episodic memory store — structured conversation summaries from narrative compression.

Storage: persona_state/{persona_id}_episodes.json
Data: [{"summary": str, "topics": [str], "emotions": [str], "date": str, "ts": float, "exchange_count": int}]
"""
import json
import time
from datetime import datetime
from pathlib import Path

_STATE_DIR = Path(__file__).parent / "persona_state"


class EpisodeStore:
    """Persistent episodic memory — structured summaries of past conversations."""

    def __init__(self, persona_id: str):
        self.persona_id = persona_id
        self._episodes: list[dict] = []
        self._load()

    def _file(self) -> Path:
        _STATE_DIR.mkdir(exist_ok=True)
        return _STATE_DIR / f"{self.persona_id}_episodes.json"

    def _load(self) -> None:
        f = self._file()
        if f.exists():
            try:
                self._episodes = json.loads(f.read_text())
            except Exception:
                self._episodes = []

    def _save(self) -> None:
        f = self._file()
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._episodes, indent=2))
        tmp.rename(f)

    def add(self, summary: str, topics: list[str], emotions: list[str],
            exchange_count: int) -> None:
        self._episodes.append({
            "summary": summary,
            "topics": topics,
            "emotions": emotions,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "ts": time.time(),
            "exchange_count": exchange_count,
        })
        self._save()

    def search(self, query: str, max_results: int = 3) -> list[dict]:
        query_lower = query.lower()
        matches = []
        for ep in self._episodes:
            score = 0
            if query_lower in ep["summary"].lower():
                score += 2
            for topic in ep.get("topics", []):
                if query_lower in topic.lower():
                    score += 1
            if score > 0:
                matches.append((score, ep))
        matches.sort(key=lambda x: -x[0])
        return [m[1] for m in matches[:max_results]]

    def list_recent(self, n: int = 10) -> list[dict]:
        return self._episodes[-n:]

    def list_all(self) -> list[dict]:
        return list(self._episodes)
