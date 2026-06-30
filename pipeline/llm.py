"""Minimal Anthropic-protocol GLM (智谱 BigModel) client.

Zero third-party deps: stdlib urllib only. Reads credentials from the
environment so no secret is ever written to disk or logged. Used by the LLM
smoke test and the M2 LLM clustering/summary runtime.

Routes to the GLM Coding Plan via the Anthropic Messages protocol at
``https://open.bigmodel.cn/api/anthropic``. The 2026-06-30 live probe proved a
generic urllib client calling ``/api/anthropic/v1/messages`` consumes Coding
Plan quota (Sam-confirmed in the GLM backend) — no Claude-Code User-Agent
needed — so the pipeline rides Sam's prepaid plan instead of billing
pay-as-you-go on the general ``/paas/v4`` endpoint. This supersedes the earlier
2026-06-29 conclusion that the general endpoint was required.

Env:
  LLM_API_KEY   required for live calls (never printed).
  LLM_BASE_URL  default is the Coding Plan Anthropic endpoint below.
  LLM_MODEL     model id; defaults to glm-5.2 (Coding-Plan-listed). On the plan
                legacy glm-5.1/glm-5 auto-redirect to glm-5.2.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


# The Coding Plan Anthropic endpoint (domestic open.bigmodel.cn, China region —
# not overseas Z.AI). The 2026-06-30 probe proved a generic client calling
# /api/anthropic/v1/messages rides the Coding Plan quota (Sam-confirmed in the
# GLM backend); the plan's "designated tools only" wording is not enforced at
# this endpoint. The general /paas/v4 endpoint (kept only as a reference
# constant) is pay-as-you-go and is no longer the default.
ANTHROPIC_BASE_URL = "https://open.bigmodel.cn/api/anthropic"
GENERAL_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_BASE_URL = ANTHROPIC_BASE_URL
DEFAULT_MODEL = "glm-5.2"
DEFAULT_TIMEOUT = 90.0
ANTHROPIC_VERSION = "2023-06-01"


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
            # Always resolve to a concrete model so callers never depend on
            # live model discovery (the Coding-Plan Anthropic proxy may not
            # expose /v1/models). Explicit LLM_MODEL wins; else the plan default.
            self.model = os.environ.get("LLM_MODEL") or DEFAULT_MODEL

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
            "Accept": "application/json",
            # Anthropic-native auth + version; Bearer kept as a compatibility
            # fallback. The 2026-06-30 probe succeeded with both headers present.
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Authorization": f"Bearer {key}",
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
        """GET /v1/models. Best-effort discovery; callers tolerate failure and
        fall back to the resolved default model. Returns OpenAI-shaped items.
        """
        data = self._request("/v1/models")
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
        """Anthropic Messages call returning a parsed JSON object.

        Returns ``(parsed_json_object, raw_response)``. The system prompt must
        mention JSON (kept as a sanity guard even though Anthropic has no native
        ``json_object`` mode). Without that mode the model may occasionally wrap
        output in ```json fences or prose; :func:`_extract_json` strips that
        before parsing. ``thinking`` is accepted for signature compatibility but
        not sent — Anthropic "disabled" means omitted, and the probe confirmed
        fast schema-faithful JSON without it.
        """
        if "json" not in system.lower() and "json" not in user.lower():
            raise LLMError("JSON output requires the prompt to mention JSON.")
        if not self.model:
            raise LLMError("LLM_MODEL is not set; discover it via list_models() first.")
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": temperature,
        }
        resp = self._request("/v1/messages", method="POST", payload=payload)
        content = _extract_content(resp)
        parsed = _extract_json(content)
        usage = resp.get("usage") or {}
        self.usage.calls += 1
        self.usage.prompt_tokens += int(usage.get("input_tokens") or 0)
        self.usage.completion_tokens += int(usage.get("output_tokens") or 0)
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
    """Read the assistant text from an Anthropic Messages response
    (``{"content": [{"type": "text", "text": "..."}], ...}``)."""
    content = resp.get("content") if isinstance(resp, dict) else None
    if not isinstance(content, list) or not content:
        raise LLMError(f"response has no content: {str(resp)[:300]}")
    block = content[0] if isinstance(content[0], dict) else {}
    text = block.get("text")
    if not isinstance(text, str) or not text.strip():
        raise LLMError(f"empty content in response: {str(resp)[:300]}")
    return text


def _extract_json(text: str) -> Any:
    """Parse JSON from model text that may be wrapped in ```json fences or have
    surrounding prose. Anthropic has no native json_object mode, so this adds
    tolerance. Falls back to the first ``{...}`` span. Raises :class:`LLMError`
    if no valid JSON object can be recovered.
    """
    cleaned = text.strip()
    # Strip one set of code fences if present (```json\n ... \n```).
    if cleaned.startswith("```"):
        newline = cleaned.find("\n")
        cleaned = cleaned[newline + 1 :] if newline != -1 else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Fallback: outermost {...} span.
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise LLMError(f"model did not return valid JSON: {text[:300]}")
