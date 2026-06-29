"""Minimal OpenAI-compatible GLM (智谱 BigModel) client.

Zero third-party deps: stdlib urllib only. Reads credentials from the
environment so no secret is ever written to disk or logged. Used first by the
LLM smoke test; intended to become the runtime client for M2 LLM clustering and
summaries once the clustering route is decided.

Env:
  LLM_API_KEY   required for live calls (never printed).
  LLM_BASE_URL  default is the Coding Plan endpoint; fall back to the general
                BigModel endpoint if the workload/model requires it.
  LLM_MODEL     model id. If unset, discover it live via list_models() and pass
                it to the constructor (the smoke test does this on purpose).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


# Both endpoints are on the domestic China region (open.bigmodel.cn), not the
# overseas Z.AI. The 2026-06-29 smoke test proved the Coding Plan endpoint
# (/coding/paas/v4) is a coding-assistant surface: ~75s latency and it ignores
# response_format (returns {"answer": ...}). The general endpoint (/paas/v4)
# honors json_object mode in ~7s, so it is the correct default here.
CODING_BASE_URL = "https://open.bigmodel.cn/api/coding/paas/v4"
GENERAL_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_BASE_URL = GENERAL_BASE_URL
DEFAULT_TIMEOUT = 90.0


class LLMError(RuntimeError):
    """Raised for any LLM call failure (transport, auth, model, parsing)."""


class LLMNotConfigured(LLMError):
    """Raised when a live call is attempted without an API key."""


@dataclass
class LLMUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


@dataclass
class GLMClient:
    api_key: str | None = field(default=None)
    base_url: str = DEFAULT_BASE_URL
    model: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    usage: LLMUsage = field(default_factory=LLMUsage)

    def __post_init__(self) -> None:
        if self.api_key is None:
            self.api_key = os.environ.get("LLM_API_KEY")
        if not self.base_url:
            self.base_url = os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL
        self.base_url = self.base_url.rstrip("/")
        if self.model is None:
            self.model = os.environ.get("LLM_MODEL")

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _require_key(self) -> str:
        if not self.api_key:
            raise LLMNotConfigured(
                "LLM_API_KEY is not set; live GLM calls are disabled."
            )
        return self.api_key

    def _request(self, path: str, method: str = "GET", payload: Any = None) -> Any:
        key = self._require_key()
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        body = None
        timeout = self.timeout
        for attempt in (1, 2):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    body = resp.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                raise LLMError(f"HTTP {exc.code} {exc.reason} from {path}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                # socket timeouts surface as TimeoutError (not URLError) on 3.10+.
                if attempt == 2:
                    reason = getattr(exc, "reason", str(exc))
                    raise LLMError(f"transport error to {path}: {reason}") from exc
                timeout *= 2  # retry once with a longer read timeout
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise LLMError(f"non-JSON response from {path}: {(body or '')[:300]}") from exc

    def list_models(self) -> list[dict[str, Any]]:
        """GET /models. Proves auth and lists model ids available to this key."""
        data = self._request("/models")
        # OpenAI-compatible shape: {"data": [{"id": "..."}, ...]}
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def chat_json(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 2000,
        temperature: float = 0.2,
        thinking: str = "disabled",
    ) -> tuple[Any, dict[str, Any]]:
        """OpenAI-compatible chat/completions forced to JSON output.

        Returns (parsed_json_object, raw_response). The system prompt must
        mention JSON (provider requirement for json_object mode); callers should
        include the exact schema in the prompt. ``thinking`` defaults to
        "disabled": GLM 5.x are reasoning models whose reasoning_content tokens
        count against max_tokens and can exhaust the budget before the final
        JSON; disabling thinking gives fast, schema-faithful JSON for
        classification/clustering tasks (per the project LLM decision doc).
        """
        if "json" not in system.lower() and "json" not in user.lower():
            raise LLMError("json_object mode requires the prompt to mention JSON.")
        if not self.model:
            raise LLMError("LLM_MODEL is not set; discover it via list_models() first.")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if thinking == "disabled":
            payload["thinking"] = {"type": "disabled"}
        elif thinking == "enabled":
            payload["thinking"] = {"type": "enabled"}
        resp = self._request("/chat/completions", method="POST", payload=payload)
        content = _extract_content(resp)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"model did not return valid JSON: {content[:300]}") from exc
        usage = resp.get("usage") or {}
        self.usage.calls += 1
        self.usage.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.usage.completion_tokens += int(usage.get("completion_tokens") or 0)
        return parsed, resp


def pick_model(models: list[dict[str, Any]]) -> str | None:
    """Pick a chat-capable model id from a list_models() result.

    Prefer the BRIEF-decided GLM 5.1, then newer 5.x, then 4.x chat ids. Avoid
    non-chat SKUs. Best-effort; the smoke test logs every candidate so a human
    can override via LLM_MODEL.
    """
    ids = [str(m.get("id")) for m in models if m.get("id")]
    if not ids:
        return None
    non_chat = ("embed", "image", "vision", "voice", "tts", "asr", "cog")
    chat = [i for i in ids if not any(x in i.lower() for x in non_chat)]
    for preferred in (
        "glm-5.1",
        "glm-5.2",
        "glm-5",
        "glm-5-turbo",
        "glm-4.7",
        "glm-4.6",
        "glm-4.5",
        "glm-4.5-air",
    ):
        for model_id in chat:
            if model_id == preferred:
                return model_id
    return chat[0]


def _extract_content(resp: Any) -> str:
    choices = resp.get("choices") if isinstance(resp, dict) else None
    if not isinstance(choices, list) or not choices:
        raise LLMError(f"response has no choices: {str(resp)[:300]}")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise LLMError(f"empty content in response: {str(resp)[:300]}")
    return content
