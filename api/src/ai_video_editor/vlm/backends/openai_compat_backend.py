"""OpenAI-compatible chat backend: Gemini (Google AI Studio) or OpenRouter.

Both speak the OpenAI `/chat/completions` shape, so one transport covers
Google's Gemini models and every OpenRouter vision model (Qwen3-VL, ...).
Frames go up as base64 JPEG data URLs on the user message; the reply is
asked for in JSON mode and parsed by `run_with_retry`.

Spend is not written to the Anthropic ledger: Gemini's free tier is $0 and
OpenRouter is capped by its own prepaid credit. A missing key, a network
failure or a rate limit that outlasts the retries raises
`VLMUnavailableError`, so callers fall back exactly as for a down Ollama.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from ...config import settings
from .base import VLMBackend, VLMUnavailableError

_log = logging.getLogger(__name__)

RATE_LIMIT_RETRIES = 4
RATE_LIMIT_WAIT_S = 15.0
# 429 rate limited; 500/502/503/504 = overloaded or flaky (common on the free tier).
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key: str | None
    model: str
    key_setting: str


def provider(name: str) -> Provider:
    """The settings for `gemini` or `openrouter`. Raises ValueError otherwise."""
    if name == "gemini":
        return Provider(
            "gemini",
            settings.gemini_base_url,
            settings.gemini_api_key,
            settings.gemini_model,
            "GEMINI_API_KEY",
        )
    if name == "openrouter":
        return Provider(
            "openrouter",
            settings.openrouter_base_url,
            settings.openrouter_api_key,
            settings.openrouter_model,
            "OPENROUTER_API_KEY",
        )
    raise ValueError(f"unknown OpenAI-compatible provider {name!r}")


class OpenAICompatBackend(VLMBackend):
    def __init__(
        self,
        provider_name: str,
        *,
        model: str | None = None,
        timeout_seconds: float | None = None,
        client: httpx.Client | None = None,
        sleep=time.sleep,
    ) -> None:
        self.provider = provider(provider_name)
        self.name = provider_name
        self.model = model or self.provider.model
        self.timeout = timeout_seconds or settings.vlm_call_timeout_seconds
        self._client = client
        self._sleep = sleep

    def health(self) -> dict:
        if not self.provider.api_key:
            return {"ok": False, "reason": f"{self.provider.key_setting} is not set in .env"}
        started = time.perf_counter()
        try:
            self._chat(
                [
                    {"role": "system", "content": "Reply with one word."},
                    {"role": "user", "content": "ping"},
                ],
                max_tokens=8,
                json_mode=False,
            )
        except Exception as exc:
            return {"ok": False, "model": self.model, "reason": str(exc)}
        return {
            "ok": True,
            "model": self.model,
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }

    def call(
        self,
        *,
        frames: list[Path],
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 1024,
    ) -> str:
        if not self.provider.api_key:
            raise VLMUnavailableError(f"{self.provider.key_setting} is not set in .env")
        content: list[dict] = [{"type": "text", "text": user_prompt}]
        content += [{"type": "image_url", "image_url": {"url": _data_url(p)}} for p in frames]
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ]
        return self._chat(messages, max_tokens=max_tokens, json_mode=True)

    def _chat(self, messages: list[dict], *, max_tokens: int, json_mode: bool) -> str:
        payload: dict = {"model": self.model, "messages": messages, "max_tokens": max_tokens}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.provider.api_key}"}
        url = f"{self.provider.base_url.rstrip('/')}/chat/completions"
        client = self._client or httpx.Client(timeout=self.timeout)
        try:
            for attempt in range(RATE_LIMIT_RETRIES + 1):
                try:
                    resp = client.post(url, json=payload, headers=headers)
                except httpx.HTTPError as exc:
                    raise VLMUnavailableError(f"{self.name} unreachable: {exc}") from exc
                if resp.status_code in RETRY_STATUSES and attempt < RATE_LIMIT_RETRIES:
                    _log.info("%s rate limited, waiting %.0fs", self.name, RATE_LIMIT_WAIT_S)
                    self._sleep(RATE_LIMIT_WAIT_S)
                    continue
                if resp.status_code in (401, 403, 429) or resp.status_code >= 500:
                    raise VLMUnavailableError(
                        f"{self.name} HTTP {resp.status_code}: {resp.text[:300]}"
                    )
                resp.raise_for_status()
                break
        finally:
            if self._client is None:
                client.close()
        choices = resp.json().get("choices") or []
        if not choices:
            return ""
        return (choices[0].get("message") or {}).get("content") or ""


def _data_url(path: Path) -> str:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
