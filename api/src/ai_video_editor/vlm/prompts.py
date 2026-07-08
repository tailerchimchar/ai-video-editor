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

import re
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


PayoffType = Literal[
    "kill", "multi_kill", "ace", "clutch",
    "plant", "defuse", "save", "outplay", "other",
]


class ClipAnalysis(BaseModel):
    """Per-clip source-analyzer response.

    VLM watches an unfiltered source clip (e.g. a raw Outplayed clip up
    to 60s) and returns a trim recommendation: WHERE the payoff moment
    is + WHAT window of buildup + resolution to keep. The compile then
    re-cuts the clip to that window using ffmpeg `-ss` / `-t`.

    All times are seconds from clip start (offset within the clip
    file itself, NOT the source recording).
    """

    payoff_anchor_seconds: float = Field(
        ge=0.0,
        description=(
            "The single frame that IS the highlight (kill, plant, "
            "defuse, clutch shot). If nothing worth highlighting is "
            "in the clip, put the midpoint here and set confidence low."
        ),
    )
    buildup_start_seconds: float = Field(
        ge=0.0,
        description=(
            "Where meaningful engagement begins. This becomes the new "
            "clip start. Preserve OP holds / ability setups / peek "
            "prep. Cut walking / buy phase / dead map view."
        ),
    )
    resolution_end_seconds: float = Field(
        ge=0.0,
        description=(
            "Where the aftermath resolves. This becomes the new clip "
            "end. Cut before reload / repeated pauses / next round buy."
        ),
    )
    payoff_type: PayoffType = Field(
        default="other",
        description=(
            "Category of the payoff. Used for title overlays and to "
            "flag high-priority montage picks (ace / clutch / multi_kill)."
        ),
    )
    description: str = Field(
        description="One-sentence account of what happens in the clip."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "0..1 self-reported confidence in the anchor + trim window. "
            "Below `vlm_clip_analyzer_min_confidence` the compile "
            "ignores the trim and uses the full clip as fallback."
        ),
    )


# ---------------------------------------------------------------------
# Game hints loader
# ---------------------------------------------------------------------


def load_game_hints(game: str | None) -> tuple[str, str]:
    """Return (`hint_text`, `resolved_hint_name`).

    Resolution order:
      1. `<game>.md` if the file exists (case-insensitive)
      2. Substring match against known game-hint filenames
         (e.g. asset `game="League of Legends"` -> `league.md`,
         `"League of Legends_05-29-2026_..."` -> `league.md`)
      3. `_default.md`

    Never raises — missing hints degrade to a minimal generic prompt
    rather than crashing the compile.
    """
    if game:
        g = game.lower().strip()
        specific = _HINTS_DIR / f"{g}.md"
        if specific.is_file():
            return specific.read_text(encoding="utf-8"), specific.stem
        # Substring match — Outplayed session folders / display names
        # often carry the game as a prefix ("League of Legends", "Valorant")
        # so a simple `<name> in game` catches most real-world inputs.
        for candidate in _HINTS_DIR.glob("*.md"):
            stem = candidate.stem
            if stem.startswith("_"):
                continue
            if stem in g:
                return candidate.read_text(encoding="utf-8"), stem
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
or is a false positive (there's no meaningful gameplay moment visible).

You are judging what a viewer would SEE — you cannot hear audio,
character voices, or callouts. Judge visually.

Your verdict is one of exactly three values:

- **pass** — the frames show a meaningful gameplay moment: an active
  fight, a kill / assist / death (visible on the killfeed or from
  ability effects), a notable engage or play, or a dramatic reaction
  (e.g. a big teamfight even if the outcome isn't a kill). It has
  enough visible setup and a clean end. Keep it as-is.
- **fixable** — a meaningful moment is present but the clip cuts in
  too late / ends too early / has dead air at the end. Suggest ONE
  fix + how many seconds to shift.
- **false_positive** — the frames show NO meaningful gameplay moment.
  Reserve this for clips where the sampled frames are just downtime:
  buy phase / shop menu, empty map with nobody around, post-game
  scoreboard the whole way through, agent select, loading screen.
  If ANYTHING interesting happens (teamfight, kill on killfeed,
  dramatic engage, ultimate ability being used), the clip is `pass`
  or `fixable` — NOT false_positive.

Important: the `claimed_event` in the prompt is a HINT about what the
finder thought was there. It might be labeled `funny_audio` or
`hype_callout` because it was detected via audio signals we can't
verify. Don't reject a clip just because you can't verify the audio
label — verify the VISIBLE gameplay instead. If the frames show a
teamfight or kill, that's `pass` regardless of what audio label the
finder attached.

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


# ---------------------------------------------------------------------
# Per-clip source analyzer — decides trim window before the compile
# ---------------------------------------------------------------------


_ANALYZER_SYSTEM_TEMPLATE = """\
You are a video editor deciding where to cut a raw gameplay clip so
it lands as a great highlight when stitched into a montage.

You are shown sampled frames spanning the entire clip. Each frame is
labeled with its timestamp (`t=N.NNs`) so you can pinpoint moments.

Your ONE job: pick a trim window that gives the right amount of
context before the payoff and cuts cleanly after. The user cares
about SEQUENCING QUALITY above all else — not too little context
(payoff feels random), not too much (dead air, walking, reloads
kill the vibe).

Return exactly one JSON object with these fields:

- `payoff_anchor_seconds` — the frame that IS the highlight moment
  (the kill, plant, defuse, clutch shot). If nothing worth
  highlighting is visible, put the clip midpoint here and mark
  `confidence` low.
- `buildup_start_seconds` — where meaningful engagement begins.
  Becomes the new clip START. Preserve buildup that adds tension
  (OP holds, ability setups, peek prep) but cut dead time (walking,
  buy phase, empty map).
- `resolution_end_seconds` — where the aftermath resolves. Becomes
  the new clip END. Cut before reload / repeated pauses / next round
  buy screens.
- `payoff_type` — one of: "kill", "multi_kill", "ace", "clutch",
  "plant", "defuse", "save", "outplay", "other". Use "other" when
  nothing highlight-worthy is visible.
- `description` — one sentence: what happens in the clip.
- `confidence` — 0..1. Mark below 0.5 if you're unsure whether the
  payoff really landed or whether the trim window is right.

Constraints:
- `buildup_start_seconds` <= `payoff_anchor_seconds` <= `resolution_end_seconds`
- All three MUST be within [0, clip_duration_seconds]
- The trim window (`resolution_end - buildup_start`) should generally
  be 5-15 seconds. Very hard ceilings: minimum 3 seconds, maximum 30.

Return ONLY valid JSON matching the schema — no prose before or after:

{{
  "payoff_anchor_seconds": <number>,
  "buildup_start_seconds": <number>,
  "resolution_end_seconds": <number>,
  "payoff_type": "kill"|"multi_kill"|"ace"|"clutch"|"plant"|"defuse"|"save"|"outplay"|"other",
  "description": "<one sentence>",
  "confidence": <0..1>
}}

## Game-specific editing hints (apply these to your trim decision)

{game_hints}
"""


def build_analyzer_system_prompt(game: str | None) -> tuple[str, str]:
    """Return (prompt, resolved_hint_name)."""
    hints, resolved = load_game_hints(game)
    return _ANALYZER_SYSTEM_TEMPLATE.format(game_hints=hints), resolved


def build_analyzer_user_prompt(clip_duration_seconds: float) -> str:
    return (
        f"Analyze this {clip_duration_seconds:.1f}-second clip. "
        "Pick the payoff anchor and the trim window. Return the JSON."
    )


# ---------------------------------------------------------------------
# PROMPT_VERSION extraction — used as cache invalidation key
# ---------------------------------------------------------------------


_PROMPT_VERSION_RE = re.compile(
    r"<!--\s*PROMPT_VERSION\s*:\s*(\d+)\s*-->", re.IGNORECASE
)


def game_hints_prompt_version(game: str | None) -> int:
    """Read the `<!-- PROMPT_VERSION: N -->` marker from the game's hint
    file. Falls back to 0 when the marker is missing. Bumping the
    version in the .md file invalidates every cached analyzer sidecar."""
    hints_text, _ = load_game_hints(game)
    m = _PROMPT_VERSION_RE.search(hints_text)
    return int(m.group(1)) if m else 0
