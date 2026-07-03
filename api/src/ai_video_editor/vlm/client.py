"""Backend selection — reads settings.vlm_backend and returns the impl.

Kept trivial today (one supported backend) but the seam is here so
adding a hosted backend is a one-line switch, not a rewrite of every
caller. The `VLMBackend` protocol lives in `backends/base.py`.
"""

from __future__ import annotations

from ..config import settings
from .backends.base import VLMBackend
from .backends.ollama_backend import OllamaBackend


class UnsupportedVLMBackendError(RuntimeError):
    """Raised when `VLM_BACKEND` doesn't match any known implementation."""


def select_backend(name: str | None = None) -> VLMBackend:
    """Return an instance of the backend named by `name` (or the env-configured
    default). Raises `UnsupportedVLMBackendError` for unknown names."""
    resolved = (name or settings.vlm_backend).lower()
    if resolved == "ollama":
        return OllamaBackend()
    raise UnsupportedVLMBackendError(
        f"Unknown VLM_BACKEND {resolved!r}. Supported: ollama."
    )
