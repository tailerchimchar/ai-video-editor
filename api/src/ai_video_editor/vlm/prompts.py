"""VLM prompt templates + verdict schemas.

Pure: no I/O beyond reading the game-hints markdown files (which are
package-shipped assets, not user data). Two levels of prompt:

- **Per-clip** — validate one candidate's cut window. Returns a
  `ClipVerdict`.
- **Whole-comp** — review a rendered compilation for pacing / cohesion.
  Returns a `CompilationReview` (list of fixes).

Verdict schemas are pinned Pydantic models so both the Ollama JSON-mode
response and any hosted backend can be validated the same way. Adding
a new game means dropping a file in `game_hints/` — this module is
game-agnostic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

_HINTS_DIR = Path(__file__).parent / "game_hints"


# ---------------------------------------------------------------------
# Verdict schemas
# ---------------------------------------------------------------------


class ClipVerdict(BaseModel):
    """Per-clip verdict returned by the VLM after watching sampled frames.

    Three verdicts. `fixable` MUST carry a `fix` and `fix_seconds`; the
    other two must not (validated in-model). This makes the loop's
    routing logic a simple switch, no fallback handling.
    """

    verdict: Literal["pass", "fixable", "false_positive"]
    why: str = Field(
        description="One-sentence explanation the loop logs + the user reads."
    )
    fix: Literal["extend_before", "extend_after", "trim_start", "trim_end"] | None = None
    fix_seconds: float | None = Field(
        default=None,
        ge=0.0,
        le=8.0,
        description="How many seconds to shift the boundary. Bounded so a "
        "wild verdict can't extend a clip by minutes.",
    )


class CompilationFix(BaseModel):
    """One suggested edit to the whole compilation."""

    clip_ref: str = Field(
        description="Clip identifier — 1-based index (e.g. '03'), UUID prefix, "
        "or 'M:SS' timestamp; same contract as the MCP tools."
    )
    issue: str = Field(description="What's wrong with this clip in context.")
    fix: Literal[
        "extend_before",
        "extend_after",
        "trim_start",
        "trim_end",
        "remove_clip",
        "apply_zoom",
        "apply_focus",
    ]
    fix_seconds: float | None = Field(default=None, ge=0.0, le=8.0)
    # For apply_zoom: name of an ROI preset (see edits._ROI_PRESETS).
    roi: str | None = None
    # For apply_focus: fractional target (0..1).
    focus_x: float | None = Field(default=None, ge=0.0, le=1.0)
    focus_y: float | None = Field(default=None, ge=0.0, le=1.0)


class CompilationReview(BaseModel):
    """Whole-comp review response — a list of fixes plus a pass flag."""

    is_cohesive: bool = Field(
        description="True when no fixes are needed. Loop stops immediately."
    )
    fixes: list[CompilationFix] = Field(default_factory=list)


# ---------------------------------------------------------------------
# Game hints loader
# ---------------------------------------------------------------------


def load_game_hints(game: str | None) -> tuple[str, str]:
    """Return (`hint_text`, `resolved_hint_name`).

    Resolution order:
      1. `<game>.md` if the file exists
      2. `_default.md`

    Never raises — missing hints degrade to a minimal generic prompt
    rather than crashing the compile.
    """
    if game:
        specific = _HINTS_DIR / f"{game.lower()}.md"
        if specific.is_file():
            return specific.read_text(encoding="utf-8"), specific.stem
    default = _HINTS_DIR / "_default.md"
    if default.is_file():
        return default.read_text(encoding="utf-8"), "_default"
    return "", "_missing"


# ---------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------


_CLIP_SYSTEM_TEMPLATE = """\
You are a video-clip taste reviewer for a highlight-reel editor. You
watch a small number of sampled frames from a short game clip and decide
whether the clip should be kept, is fixable with a small window change,
or is a false positive (the claimed event isn't actually visible).

Your verdict is one of exactly three values:

- **pass** — the clip contains the claimed event with enough setup and
  a clean end. Keep it as-is.
- **fixable** — the event is present but the clip cuts in too late /
  ends too early / has dead air at the end. Suggest ONE fix + how many
  seconds to shift.
- **false_positive** — the claimed event is NOT in these frames. This
  short-circuits the loop so no retries are wasted.

Available fixes for `fixable`:
- `extend_before` — add N seconds of pre-context (event happens too
  close to the start)
- `extend_after` — add N seconds of post-context (event happens too
  close to the end)
- `trim_start` — remove N seconds from the start (dead air before setup)
- `trim_end` — remove N seconds from the end (dead air after payoff)

Bounds: `fix_seconds` is between 0.1 and 8.0.

Return ONLY valid JSON matching this exact schema — no prose before or
after:

{{
  "verdict": "pass" | "fixable" | "false_positive",
  "why": "one-sentence explanation",
  "fix": "extend_before" | "extend_after" | "trim_start" | "trim_end" | null,
  "fix_seconds": <number 0.1..8.0> | null
}}

`fix` and `fix_seconds` must be null for `pass` and `false_positive`.

## Game-specific hints

{game_hints}
"""


_COMP_SYSTEM_TEMPLATE = """\
You are a video-editor reviewing a highlight compilation for pacing,
cohesion, and variety. You watch sampled frames spread across the
whole rendered compilation and return a list of specific fixes.

You may recommend zero to several fixes. Fixes reference clips by
`clip_ref` — either a 1-based index like "03", a UUID prefix, or a
"M:SS" timestamp. When you're not sure which clip, use the timestamp
form.

Available fixes:

- `extend_before` / `extend_after` — grow the clip's window
- `trim_start` / `trim_end` — shrink the clip's window
- `remove_clip` — drop the clip entirely (use when it's redundant or
  breaks flow)
- `apply_zoom` — draw attention to a ROI; requires `roi` (one of the
  preset names below)
- `apply_focus` — spotlight a point; requires `focus_x`, `focus_y`
  (fractions of frame, 0..1)

Available ROI presets: `scoreline_lol`, `minimap_lol`,
`champion_portrait_lol`, `killfeed_lol`, `item_bar_lol`, `center`.

Return ONLY valid JSON matching this schema — no prose:

{{
  "is_cohesive": true | false,
  "fixes": [
    {{
      "clip_ref": "03" | "abc12345" | "1:24",
      "issue": "one-sentence problem",
      "fix": "extend_before" | "extend_after" | "trim_start" | "trim_end"
             | "remove_clip" | "apply_zoom" | "apply_focus",
      "fix_seconds": <number 0.1..8.0> | null,
      "roi": "<preset name>" | null,
      "focus_x": <0..1> | null,
      "focus_y": <0..1> | null
    }}
  ]
}}

`is_cohesive: true` with an empty `fixes` list means the compilation
is good — the loop stops immediately.

## Game-specific hints (apply per-clip judgement using these)

{game_hints}
"""


def build_clip_system_prompt(game: str | None) -> tuple[str, str]:
    """Return (prompt, resolved_hint_name). The resolved hint name is
    what got picked (e.g. `"league"`, `"_default"`) so the trace can
    record which file was applied."""
    hints, resolved = load_game_hints(game)
    return _CLIP_SYSTEM_TEMPLATE.format(game_hints=hints), resolved


def build_comp_system_prompt(game: str | None) -> tuple[str, str]:
    hints, resolved = load_game_hints(game)
    return _COMP_SYSTEM_TEMPLATE.format(game_hints=hints), resolved


def build_clip_user_prompt(
    *,
    event_type: str | None,
    source: str | None,
    anchor_seconds: float | None,
    clip_duration: float,
    extra_context: str | None = None,
) -> str:
    """One-line context lead for the VLM — what the finder claims is in
    this clip. Kept short so the model spends attention on the frames."""
    bits: list[str] = []
    if event_type:
        bits.append(f"claimed event: {event_type}")
    if source:
        bits.append(f"source: {source}")
    if anchor_seconds is not None:
        bits.append(f"anchor: t={anchor_seconds:.1f}s in source")
    bits.append(f"clip length: {clip_duration:.1f}s")
    if extra_context:
        bits.append(extra_context.strip())
    return (
        "Verdict on this clip? "
        + " · ".join(bits)
        + " · watch the sampled frames and return the JSON verdict."
    )


def build_comp_user_prompt(*, clip_count: int, total_seconds: float) -> str:
    return (
        f"Review this {total_seconds:.0f}-second compilation ({clip_count} clips)."
        " Focus on pacing, cohesion, and variety across the clips shown."
        " Return the JSON review."
    )
