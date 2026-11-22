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

import json

from pydantic import BaseModel

from .anthropic_surface import (
    check_budget,
    make_client,
    observe,
    record_generation,
    record_spend,
    reserve_call,
)
from .config import settings
from .models import RankedCandidate

# Frozen — no timestamps/IDs/per-request data, so it forms a stable
# cacheable prefix (90%+ cache-hit on second call onward).
#
# Editing taste is encoded explicitly per the user's reference channels:
# - skill like Zekken (mechanical brilliance)
# - funny like JawGemo (skill + humor together)
# - insight like PewDiePie (commentary + reactions)
# - random comedic timing like all (surprise cuts)
#
# Sources are listed strongest-first so the model weights them correctly
# even without explicit confidence translation in metadata.
_SYSTEM_PROMPT = """\
You are a senior gameplay-highlight editor. You judge candidate moments
from a JSON list — you never see the video itself. Channel taste from
the channels the user wants to be like:

- **Skill like Zekken** — clean mechanics, decisive kills, no wasted moves.
- **Funny like JawGemo** — humor doesn't mean low-skill; the best moments
  are skill AND personality together.
- **Insight like PewDiePie** — what the player SAID matters as much as
  what they did. Reactions, commentary, real laughter.
- **Random comedic timing like all of them** — surprise cuts hit hardest;
  a goofy line right before a multikill beats the multikill alone.

The goal: a reel that shows the player's SKILL while drawing out their
PERSONALITY. Every clip should earn its place.

# Sources (strongest signal first)

- `riot_api`: Riot's official match timeline — ground truth when
  `metadata.correlation_confidence` is "high"/"medium". Ignore on "low".
- `cv_kda`: OCR on the in-game scoreboard. `metadata.kda_before` ->
  `kda_after` shows the exact change. event_type = kill / death /
  assist. Anchor accurate to ~2.5s. Multiple within ~10s = one
  teamfight, keep the best one.
- `outplayed_clip`: Outplayed pre-detected an event. Usually worth keeping.
- `transcript_keyword`: Whisper STT caught a hype/funny line.
  `metadata.text` = the actual sentence. Personality signal.
- `audio_peak`: Just a loud region. Weak alone; strong near another source.

# Editing principles

- **Pair across sources**: a transcript_keyword or audio_peak within ~15s
  of a cv_kda/riot_api/outplayed_clip event is the gold pattern. KEEP
  BOTH; mention the pairing in `reason`.
- **Cut redundancy**: back-to-back same-source clusters dilute the reel.
- **Hype scale**: pentakill/ace = 0.9+, solo kill = 0.5-0.7, lone audio
  peak = 0.2-0.4. Be opinionated — USE the extremes; don't average at 0.5.

# Output

Per candidate, preserving `candidate_id`:
- `keep`: aim ~30-50% on a typical list.
- `funny_score`, `hype_score`, `story_score`: 0.0-1.0.
- `suggested_start_seconds` / `suggested_end_seconds`: tighten or pad.
  In-source only. Never start >= end.
- `reason`: one sentence; call out cross-source pairings."""


class _RankerOutput(BaseModel):
    results: list[RankedCandidate]


@observe(name="rank-highlight-candidates", as_type="generation")
def rank_candidates(
    game: str | None, candidates: list[dict], asset_id: str | None = None
) -> list[RankedCandidate]:
    """Rank candidates for one video. `candidates` are highlight_candidates
    rows (id, source, start/end, event_type, confidence, metadata)."""
    if not candidates:
        return []

    check_budget(asset_id)
    reserve_call("rank", settings.anthropic_max_rank_calls, "ANTHROPIC_MAX_RANK_CALLS")

    # Volatile payload — deterministic ordering so the cached system
    # prefix stays valid and prompts are reproducible.
    payload = json.dumps(
        {"game": game, "candidates": candidates},
        sort_keys=True,
        default=str,
    )

    client = make_client()
    response = client.messages.parse(
        model=settings.ranker_model,
        # Verbose reasons on scrim-length videos (15+ candidates) can
        # blow past 8k; 32k is well under Haiku's ~200k output cap and
        # gives headroom for candidates that carry rich metadata.
        max_tokens=32000,
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
    record_spend(asset_id, "rank", settings.ranker_model, response.usage)
    results = response.parsed_output.results

    record_generation(
        response,
        trace_input={"game": game, "num_candidates": len(candidates)},
        trace_output={"kept": sum(1 for r in results if r.keep)},
        tags=["highlight-ranker"],
    )

    return results
