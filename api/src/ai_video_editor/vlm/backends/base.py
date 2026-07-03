"""VLMBackend protocol + shared JSON-schema retry logic.

Every backend takes (frames, system_prompt, user_prompt, response_model)
and returns a parsed Pydantic instance. Retry-on-parse-fail lives here
so an ollama/hosted/mock backend all get identical fallback behavior.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class VLMUnavailableError(RuntimeError):
    """Raised when the backend can't be reached / model isn't pulled.

    Loop code catches this and marks the clip's verdict as
    `skip_vlm_unavailable` — never fatal to the compile.
    """


class VLMBackend(Protocol):
    """Contract for a vision backend.

    Implementations MAY throw `VLMUnavailableError` from `health()` or
    `call()` when the underlying service is unreachable. All other
    exceptions bubble; the shared `run_with_retry` below funnels
    JSONDecodeError / ValidationError into a bounded retry.
    """

    name: str  # short backend identifier — surfaces in Langfuse tags

    def health(self) -> dict:
        """Report reachable status + active model + canary latency.

        Returns a dict, e.g. `{"ok": True, "model": "qwen3-vl:4b",
        "latency_ms": 8321}`. Never raises for "just unreachable" —
        return `{"ok": False, "reason": "..."}` instead so callers can
        display the message.
        """
        ...

    def call(
        self,
        *,
        frames: list[Path],
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """Send frames + prompts to the model, return raw text.

        Text is expected to be JSON (backends should request JSON mode
        where possible). Parsing + validation happens in `run_with_retry`
        so the backend layer stays a thin transport.
        """
        ...


def run_with_retry(
    backend: VLMBackend,
    *,
    frames: list[Path],
    system_prompt: str,
    user_prompt: str,
    response_model: type[T],
    max_retries: int = 2,
) -> T:
    """Call the backend, parse JSON, validate against `response_model`.

    Bounded retry on JSON-parse or schema-validation failure — a hint
    to the model gets appended to the user prompt on each retry.
    Backends that return non-JSON garbage burn a retry each time; after
    `max_retries` we raise the last error for the caller to log.

    Never retries on `VLMUnavailableError` — that's a service state, not
    a bad response.
    """
    hint = ""
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        raw = backend.call(
            frames=frames,
            system_prompt=system_prompt,
            user_prompt=user_prompt + hint,
        )
        try:
            data = json.loads(_strip_code_fence(raw))
            return response_model.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_err = exc
            hint = (
                "\n\nYour previous response could not be parsed. Return "
                "ONLY valid JSON matching the schema — no markdown, no "
                "prose, no explanation before or after."
            )
            _ = attempt  # attempt count is implicit in the loop
    # Exhausted retries — surface the last parse error
    assert last_err is not None
    raise last_err


def _strip_code_fence(text: str) -> str:
    """Strip a ```json ... ``` fence if the model wrapped its output.

    Ollama's JSON mode usually returns bare JSON, but some models still
    fence their output. Best-effort — falls through unchanged on
    anything unexpected.
    """
    s = text.strip()
    if s.startswith("```"):
        # Drop the first fence line + a possible language tag
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines)
    return s
