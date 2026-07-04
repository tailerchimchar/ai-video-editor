"""Ollama backend — Qwen3-VL 4B/2B via localhost:11434.

Uses Ollama's `/api/chat` with `format: "json"` for structured output.
Frames are attached as base64 images on the user message per Ollama's
schema.

**Model fallback ladder** — the backend tries `model_primary` first;
on connection failure / model-not-pulled / OOM, falls through to
`model_fallback`. The active model + latency is reported via `health()`
so the UI can show which tier is running.
"""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path

import httpx

from ...config import settings
from .base import VLMBackend, VLMUnavailableError

_log = logging.getLogger(__name__)


class OllamaBackend(VLMBackend):
    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model_primary: str | None = None,
        model_fallback: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.base_url = (base_url or settings.vlm_ollama_url).rstrip("/")
        self.model_primary = model_primary or settings.vlm_model_primary
        self.model_fallback = model_fallback or settings.vlm_model_fallback
        self.timeout = timeout_seconds or settings.vlm_call_timeout_seconds
        # Which model actually succeeded most recently — used by `health()`
        # and included in trace metadata.
        self._active_model: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def health(self) -> dict:
        """Probe Ollama + the model ladder. Never raises."""
        tags = self._list_tags()
        if tags is None:
            return {
                "ok": False,
                "reason": f"Ollama not reachable at {self.base_url}. "
                "Install Ollama and run `ollama serve`.",
            }
        pulled = {t.get("name") for t in tags}
        model = self._pick_model(pulled)
        if model is None:
            return {
                "ok": False,
                "reason": (
                    "Ollama running but no VLM model pulled. Run "
                    f"`ollama pull {self.model_primary}` or "
                    f"`ollama pull {self.model_fallback}`."
                ),
            }
        # Canary — a tiny prompt to confirm the model actually loads.
        started = time.perf_counter()
        try:
            self._chat(model, system="You reply with one word.", user="ping", images=[])
            latency_ms = int((time.perf_counter() - started) * 1000)
        except Exception as exc:
            return {
                "ok": False,
                "model": model,
                "reason": f"model responded with error: {exc!s}",
            }
        self._active_model = model
        return {"ok": True, "model": model, "latency_ms": latency_ms}

    def call(
        self,
        *,
        frames: list[Path],
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """Route through the ladder + return the raw JSON string."""
        tags = self._list_tags()
        if tags is None:
            raise VLMUnavailableError(
                f"Ollama unreachable at {self.base_url}"
            )
        pulled = {t.get("name") for t in tags}
        model = self._pick_model(pulled)
        if model is None:
            raise VLMUnavailableError(
                f"No VLM model pulled ({self.model_primary} / {self.model_fallback})"
            )
        self._active_model = model
        images = [self._encode_image(p) for p in frames]
        return self._chat(model, system=system_prompt, user=user_prompt, images=images)

    @property
    def active_model(self) -> str | None:
        """The model that most recently returned successfully."""
        return self._active_model

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _pick_model(self, pulled: set[str | None]) -> str | None:
        """Try primary → fallback; return the first that's pulled."""
        for candidate in (self.model_primary, self.model_fallback):
            if not candidate:
                continue
            if candidate in pulled:
                return candidate
        return None

    def _list_tags(self) -> list[dict] | None:
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(f"{self.base_url}/api/tags")
                resp.raise_for_status()
        except Exception as exc:
            _log.debug("ollama /api/tags failed: %s", exc)
            return None
        try:
            return resp.json().get("models") or []
        except Exception:
            return []

    def _chat(
        self,
        model: str,
        *,
        system: str,
        user: str,
        images: list[str],
    ) -> str:
        payload: dict = {
            "model": model,
            "stream": False,
            "format": "json",
            # Bump context window — Ollama defaults to 4096 tokens which
            # gets blown out the moment a single image is attached
            # (~8-9k tokens per frame at typical resolutions). 32k gives
            # room for ~8 frames + prompts without truncation.
            "options": {"num_ctx": 32768},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user, **({"images": images} if images else {})},
            ],
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(f"{self.base_url}/api/chat", json=payload)
            resp.raise_for_status()
        body = resp.json()
        return (body.get("message") or {}).get("content", "") or ""

    @staticmethod
    def _encode_image(path: Path) -> str:
        with path.open("rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
