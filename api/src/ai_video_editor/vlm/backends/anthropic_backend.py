"""Hosted VLM backend — Claude Haiku 4.5 vision (Anthropic).

Chosen over local Ollama for the reasons documented in Notion:
Cost + Performance Tradeoffs (2026-07-03). Short version: on the
user's GTX 1660 every local Ollama call hit our 120s timeout;
Anthropic Haiku vision finishes each call in ~2-3s at
~$0.01-0.02 per call.

Same `VLMBackend` protocol as `OllamaBackend`. Selection via
`VLM_BACKEND=anthropic` in `.env`. Reuses the existing
`ANTHROPIC_API_KEY` — no new credentials.

Native Anthropic instrumentation via `AnthropicInstrumentor` is
already loaded in `tracing.py`, so every `client.messages.create`
call auto-populates as a Langfuse generation span with token usage
+ cost. No manual span attach needed here.
"""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path

import anthropic

from ...config import settings
from .base import VLMBackend, VLMUnavailableError

_log = logging.getLogger(__name__)


class AnthropicBackend(VLMBackend):
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        # Distinguish explicit empty-string (test-passes-no-key) from
        # unset (fall through to settings). An `or` fallback would silently
        # swap `""` for the real key.
        self.api_key = settings.anthropic_api_key if api_key is None else api_key
        self.model = model or settings.vlm_anthropic_model
        self.timeout = timeout_seconds or settings.vlm_call_timeout_seconds
        # For parity with OllamaBackend.active_model — the model that
        # most recently returned successfully. On Anthropic this is
        # always the configured model (no fallback ladder).
        self._active_model: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def health(self) -> dict:
        """Probe the API with a tiny canary call. Never raises."""
        if not self.api_key:
            return {
                "ok": False,
                "reason": (
                    "ANTHROPIC_API_KEY is empty. Set it in api/.env to "
                    "use the hosted VLM backend."
                ),
            }
        started = time.perf_counter()
        try:
            client = anthropic.Anthropic(api_key=self.api_key, timeout=10.0)
            client.messages.create(
                model=self.model,
                max_tokens=32,
                messages=[{"role": "user", "content": "reply with the word ok"}],
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
        except Exception as exc:
            return {"ok": False, "model": self.model, "reason": f"{exc!s}"}
        self._active_model = self.model
        return {"ok": True, "model": self.model, "latency_ms": latency_ms}

    def call(
        self,
        *,
        frames: list[Path],
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """Send frames + prompts to Claude vision, return the raw JSON string.

        Uses `messages.create` (not `messages.parse`) so the shared
        `run_with_retry` in base.py can drive schema validation +
        retries the same way it does for Ollama. The system prompt
        already instructs the model to return only JSON.
        """
        if not self.api_key:
            raise VLMUnavailableError("ANTHROPIC_API_KEY is empty")
        client = anthropic.Anthropic(
            api_key=self.api_key,
            timeout=self.timeout,
            max_retries=2,
        )
        content: list[dict] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": _encode_image(p),
                },
            }
            for p in frames
        ]
        content.append({"type": "text", "text": user_prompt})
        try:
            resp = client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        # Frozen per-request system prompt — cacheable to
                        # drop input cost on subsequent iter's in the loop.
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.APIStatusError as exc:
            # 4xx/5xx from the API — surface as unavailable so the loop's
            # graceful degradation fires (skip_vlm_unavailable), never a
            # crash of the compile.
            _log.warning("anthropic vlm call failed: %s", exc)
            raise VLMUnavailableError(f"anthropic API error: {exc!s}") from exc
        self._active_model = getattr(resp, "model", None) or self.model
        # Extract text from the first text block. Anthropic vision replies
        # are always a single-text-block response for our prompt shape.
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                return block.text
        return ""

    @property
    def active_model(self) -> str | None:
        return self._active_model


def _encode_image(path: Path) -> str:
    with path.open("rb") as f:
        return base64.b64encode(f.read()).decode("ascii")
