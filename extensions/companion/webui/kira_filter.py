"""
Open WebUI Filter — #Kira persona injection.
"""
import json
import urllib.request
import urllib.error
from pydantic import BaseModel, Field
from typing import Optional


class Filter:
    class Valves(BaseModel):
        oaio_hub_url: str = Field(
            default="http://oaio:9000",
            description="oAIo hub base URL (oaio is the container name on oaio-net)",
        )
        kira_tag: str = Field(
            default="#Kira",
            description="Tag that triggers persona injection",
        )
        priority_override: int = Field(
            default=-1,
            description="Override priority dial (-1 = use hub's current setting)",
        )
        timeout: float = Field(
            default=5.0,
            description="HTTP timeout for hub calls (seconds)",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _http_get(self, url: str) -> dict | None:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=self.valves.timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            print(f"[kira_filter] GET {url} failed: {e}")
            return None

    def _http_post(self, url: str, data: dict) -> None:
        try:
            body = json.dumps(data).encode()
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=2.0)
        except Exception:
            pass

    def _has_kira_tag(self, messages: list[dict]) -> tuple[bool, int | None]:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                content = messages[i].get("content", "")
                if self.valves.kira_tag in content:
                    return True, i
                return False, i
        return False, None

    def _fetch_prompt(self) -> dict | None:
        url = f"{self.valves.oaio_hub_url}/extensions/companion/persona/prompt"
        if self.valves.priority_override >= 0:
            url += f"?priority={self.valves.priority_override}"
        data = self._http_get(url)
        if not data or not data.get("enabled") or not data.get("prompt"):
            return None
        return data

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        messages = body.get("messages", [])
        if not messages:
            return body

        has_tag, user_idx = self._has_kira_tag(messages)
        if not has_tag or user_idx is None:
            return body

        original = messages[user_idx]["content"]
        cleaned = original.replace(self.valves.kira_tag, "").strip()
        messages[user_idx]["content"] = cleaned

        persona_data = self._fetch_prompt()
        if not persona_data:
            return body

        messages = [m for m in messages if m.get("role") != "system"]
        messages.insert(0, {
            "role": "system",
            "content": persona_data["prompt"],
        })
        body["messages"] = messages

        self._http_post(
            f"{self.valves.oaio_hub_url}/extensions/companion/persona/record",
            {"role": "user", "text": cleaned},
        )

        body["__kira_active"] = True
        return body

    async def outlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        if not body.get("__kira_active"):
            return body

        messages = body.get("messages", [])
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                text = msg.get("content", "")
                if text:
                    self._http_post(
                        f"{self.valves.oaio_hub_url}/extensions/companion/persona/record",
                        {"role": "assistant", "text": text},
                    )
                break

        body.pop("__kira_active", None)
        return body
