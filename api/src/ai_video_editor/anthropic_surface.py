"""The single Anthropic surface: client factory, spend guard, tracing.

Every Anthropic call in the backend (ranker, typed decisions) goes
through these helpers so each call is bounded, counted and traced the
same way.

- Spend guard: process-level call counters, one budget per feature. They
  reset when the server restarts (a fresh process = a fresh budget),
  which stops a stray loop from draining the Anthropic balance.
- Tracing: model + token usage are attached manually because the
  AnthropicInstrumentor does not cover `messages.parse`; without it the
  trace shows $0 / 0 tokens.
"""

import contextlib

import anthropic

from .config import settings

try:
    from langfuse import get_client, observe
except Exception:  # langfuse optional

    def observe(*_a, **_k):
        def deco(fn):
            return fn

        return deco

    def get_client():  # type: ignore[misc]
        return None


_calls: dict[str, int] = {}


class SpendCapError(RuntimeError):
    """Raised when a feature's per-process Anthropic call cap is hit."""


def reserve_call(feature: str, cap: int, env_name: str) -> None:
    """Count one call for `feature`, or raise before any API call is made."""
    if _calls.get(feature, 0) >= cap:
        raise SpendCapError(
            f"{feature}-call cap reached ({cap} per server run) — raise "
            f"{env_name} or restart to reset. No Anthropic call was made."
        )
    _calls[feature] = _calls.get(feature, 0) + 1


def make_client() -> anthropic.Anthropic:
    """Bounded: the SDK auto-retries 429/5xx with backoff; cap it and the
    request timeout so a single call can't spin. (4xx like the billing
    400 are not retried at all — they fail fast.)"""
    return anthropic.Anthropic(
        api_key=settings.anthropic_api_key,
        max_retries=settings.ranker_max_retries,
        timeout=settings.ranker_timeout_seconds,
    )


def record_generation(
    response,
    *,
    trace_input: dict,
    trace_output: dict,
    tags: list[str],
    metadata: dict | None = None,
) -> None:
    """Attach model, token usage and explicit trace I/O to the current
    Langfuse generation. Never raises: tracing must not break a call."""
    lf = get_client()
    if lf is None:
        return
    with contextlib.suppress(Exception):
        u = response.usage
        usage_details = {"input": u.input_tokens, "output": u.output_tokens}
        for attr in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            tokens = getattr(u, attr, None)
            if tokens:
                usage_details[attr] = tokens
        extra = {"metadata": metadata} if metadata else {}
        lf.update_current_generation(model=response.model, usage_details=usage_details, **extra)
        lf.update_current_trace(input=trace_input, output=trace_output, tags=tags, **extra)
