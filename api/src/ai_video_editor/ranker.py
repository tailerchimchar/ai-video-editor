"""LLM candidate ranker.

Takes the cheap, deterministically-generated HighlightCandidate rows and
asks Claude to reduce them to the best moments — keep/reject plus
funny/hype/story scores, refined in/out points, and a one-line reason.
The model never watches video; it ranks structured metadata.

- Anthropic SDK with `messages.parse()` for schema-validated output.
- Frozen system prompt cached via `cache_control` (volatile candidate
  JSON goes after the breakpoint, in the user turn).
- `@observe(as_type="generation")`; model + token usage attached
  manually (the AnthropicInstrumentor does not cover `messages.parse`)
  so Langfuse computes cost. Trace input set explicitly (not raw args).
"""

import contextlib
import json

import anthropic
from pydantic import BaseModel

from .config import settings
from .models import RankedCandidate

try:
    from langfuse import get_client, observe
except Exception:  # langfuse optional

    def observe(*_a, **_k):
        def deco(fn):
            return fn

        return deco

    def get_client():  # type: ignore[misc]
        return None


# Frozen — no timestamps/IDs/per-request data, so it forms a stable
# cacheable prefix (see prompt-caching guidance).
_SYSTEM_PROMPT = """\
You are a highlight editor for gameplay recordings. You are given a JSON \
list of candidate moments detected cheaply from a single video (sources: \
Outplayed event clips, audio peaks, transcript keywords). You never see \
the video itself — judge only from the structured metadata provided.

For EACH candidate, decide:
- keep: true if this is worth putting in a highlight reel, false otherwise.
- funny_score, hype_score, story_score: each 0.0-1.0.
  * funny_score  — comedic value (fails, reversals, funny audio).
  * hype_score   — raw excitement (multikills, clutches, big plays).
  * story_score  — narrative interest (comebacks, setup-payoff).
- suggested_start_seconds / suggested_end_seconds: tighten or pad the
  given window to the moment that actually matters. Stay within the
  source video; never produce start >= end.
- reason: one concise sentence explaining the call.

Outplayed clips are pre-detected events and usually worth keeping unless \
clearly redundant. Audio peaks are weak signals — keep only if the \
metadata suggests something genuinely interesting. Return one result per \
input candidate, preserving candidate_id."""


class _RankerOutput(BaseModel):
    results: list[RankedCandidate]


# Process-level spend guard. Resets when the server restarts (a fresh
# process = a fresh budget), which is the intended granularity: it stops
# a single run / stray loop from draining the Anthropic balance.
_rank_call_count = 0


class SpendCapError(RuntimeError):
    """Raised when the per-process rank-call cap is hit."""


@observe(name="rank-highlight-candidates", as_type="generation")
def rank_candidates(game: str | None, candidates: list[dict]) -> list[RankedCandidate]:
    """Rank candidates for one video. `candidates` are highlight_candidates
    rows (id, source, start/end, event_type, confidence, metadata)."""
    if not candidates:
        return []

    global _rank_call_count
    if _rank_call_count >= settings.anthropic_max_rank_calls:
        raise SpendCapError(
            f"rank-call cap reached ({settings.anthropic_max_rank_calls} per "
            f"server run) — raise ANTHROPIC_MAX_RANK_CALLS or restart to reset. "
            f"No Anthropic call was made."
        )
    _rank_call_count += 1

    # Volatile payload — deterministic ordering so the cached system
    # prefix stays valid and prompts are reproducible.
    payload = json.dumps(
        {"game": game, "candidates": candidates},
        sort_keys=True,
        default=str,
    )

    # Bounded: the SDK auto-retries 429/5xx with backoff; cap it and the
    # request timeout so a single rank job can't spin. (4xx like the
    # billing 400 are not retried at all — they fail fast.)
    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key,
        max_retries=settings.ranker_max_retries,
        timeout=settings.ranker_timeout_seconds,
    )
    response = client.messages.parse(
        model=settings.ranker_model,
        max_tokens=8000,
        system=[
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": payload}],
        output_format=_RankerOutput,
    )
    results = response.parsed_output.results

    # Explicit trace I/O + feature tag (skill: don't let raw args leak in).
    # Also attach model + token usage to this generation so Langfuse
    # computes cost — the AnthropicInstrumentor does NOT cover
    # `messages.parse`, so without this the trace shows $0 / 0 tokens.
    lf = get_client()
    if lf is not None:
        with contextlib.suppress(Exception):
            u = response.usage
            usage_details = {
                "input": u.input_tokens,
                "output": u.output_tokens,
            }
            for attr in ("cache_read_input_tokens", "cache_creation_input_tokens"):
                tokens = getattr(u, attr, None)
                if tokens:
                    usage_details[attr] = tokens
            lf.update_current_generation(
                model=response.model,
                usage_details=usage_details,
            )
            lf.update_current_trace(
                input={"game": game, "num_candidates": len(candidates)},
                output={"kept": sum(1 for r in results if r.keep)},
                tags=["highlight-ranker"],
            )

    return results
