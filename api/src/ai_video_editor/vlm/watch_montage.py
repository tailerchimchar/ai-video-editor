"""End-to-end "watch the montage" VLM critic.

Sends a compiled highlight montage to Claude Haiku 4.5 for a
per-interval, timestamp-grounded critique. Complements -- does not
replace -- the existing per-clip and whole-comp validators:

- `validate_clip` (per-clip loop) -- decides whether ONE clip's cut
  window is right.
- `validate_compilation` (whole-comp loop) -- 5-15 sampled frames, asks
  for pacing fixes referenced by clip index.
- **`watch_montage`** (this module) -- panel-packs the ENTIRE rendered
  montage at 1 fps and asks the VLM to emit timestamped critique
  flags (`t=12s: scoreboard, dead air`) plus an overall verdict.
  Rides the strict-mode structured-output surface so schema
  compliance is guaranteed without retry loops.

The insight (from deep-research 2026-07-08): panel-packing consecutive
frames into 2x2 mosaics halves the visual-token budget at equal
accuracy, and closed VLMs are best steered toward per-timestamp output
via a JSON-schema-constrained decoder. Combining the two lets Haiku
"watch" the full 60s output in one call for ~$0.02.

Language-model timestamp emissions drift toward priors, so returned
timestamps are snapped to panel boundaries before we hand them back.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import anthropic
from PIL import Image
from pydantic import BaseModel, Field

from ..anthropic_surface import record_spend
from ..candidates.probe import get_duration_seconds
from ..config import settings
from ..taxonomy import FlagCategory

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Structured output schema -- what the VLM emits
# ---------------------------------------------------------------------


# Deterministic ops the refine loop can perform on a sub-clip. `drop`
# is the existing behavior; the others are M2's shift/extend surgery.
SuggestedFix = Literal[
    "drop",  # remove the sub-clip entirely
    "extend_before",  # push the clip's start earlier by fix_seconds
    "extend_after",  # push the clip's end later by fix_seconds
    "trim_start",  # trim fix_seconds off the start
    "trim_end",  # trim fix_seconds off the end
    "shift_left",  # shift the whole window earlier by fix_seconds
    "shift_right",  # shift the whole window later by fix_seconds
    "keep",  # positive flag -- do not adjust
]


class MomentFlag(BaseModel):
    """One flag on a specific interval of the montage."""

    start_s: float = Field(
        ge=0.0,
        description=(
            "Start of the flagged interval, in seconds from the beginning "
            "of the montage. Will be snapped to the nearest panel boundary."
        ),
    )
    end_s: float = Field(
        ge=0.0,
        description="End of the flagged interval, in seconds from montage start.",
    )
    category: FlagCategory
    why: str = Field(
        description=(
            "One-sentence, specific explanation. Reference what is visible: "
            '"killfeed shows tpew killing HitTheQuang -- teammate frag, not user\'s".'
        )
    )
    suggested_fix: SuggestedFix | None = Field(
        default=None,
        description=(
            "The deterministic op the refine loop should apply. Set for "
            "actionable flags (kill_anchor_misplaced -> shift, cut_too_early "
            "-> extend_after, boring_pacing -> trim). Leave None for "
            "positive flags (great_moment, payoff_lands) and observations "
            "that don't map to a mechanical fix."
        ),
    )
    fix_seconds: float | None = Field(
        default=None,
        ge=0.0,
        le=5.0,
        description=(
            "Magnitude of the fix in seconds, 0-5 range. Required when "
            "suggested_fix is anything other than drop/keep/None."
        ),
    )


class MontageCritique(BaseModel):
    """The whole-montage critique response."""

    overall_verdict: Literal["ship_it", "needs_edits", "start_over"] = Field(
        description=(
            "`ship_it` -- no material issues, publish as-is. "
            "`needs_edits` -- a few flagged moments to fix. "
            "`start_over` -- pacing or content fundamentally broken."
        )
    )
    opener_strength: int = Field(
        ge=1,
        le=5,
        description="1-5: how strongly the first ~5 seconds hook the viewer.",
    )
    climax_strength: int = Field(
        ge=1,
        le=5,
        description="1-5: how strongly the biggest moment lands.",
    )
    pacing: Literal["too_slow", "just_right", "too_fast"]
    top_recommendation: str = Field(
        description=(
            "One-sentence: the single biggest change that would improve "
            "the montage. Actionable -- name the interval and what to do."
        )
    )
    flags: list[MomentFlag] = Field(
        default_factory=list,
        description=(
            "Zero or more intervals worth flagging. Include BOTH problems "
            "(dead air, scoreboard shots, own deaths) AND highlights "
            "(great_moment, good_buildup) so the caller can see what "
            "worked. Chronological order preferred."
        ),
    )


# ---------------------------------------------------------------------
# Panel extraction -- ffmpeg tile filter
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class Panel:
    """One 2x2 mosaic image + the time range it covers."""

    path: Path
    start_s: float
    """Start of the interval this panel spans (montage seconds)."""

    end_s: float
    """End of the interval (start_s + panel_seconds, clamped to duration)."""

    index: int
    """1-based panel index -- the label shown to the VLM."""


def extract_panels(
    video_path: str | Path,
    out_dir: Path,
    *,
    fps: float = 1.0,
    grid: tuple[int, int] = (2, 2),
    tile_width: int = 480,
    quality: int = 82,
) -> list[Panel]:
    """Sample `video_path` at `fps` and tile every `grid[0]*grid[1]`
    frames into one JPEG in `out_dir`.

    Two-step pipeline: ffmpeg to sampled JPEGs, then Pillow to compose
    the 2x2 mosaics. Splitting the work makes the ffmpeg command
    trivial (no tile filter -- that filter segfaults on some
    Windows builds when used together with `fps`) and gives Pillow
    full control over cell layout, labels, and quality.

    Each panel covers `(grid[0]*grid[1]) / fps` seconds of source.
    Default 2x2 at 1 fps -> each panel spans 4 seconds. A 60s montage
    yields 15 panels.

    `tile_width` is the width of ONE cell (not the whole tile), so at
    the 2x2 default the mosaic is 960 pixels wide. Sized to fit
    Haiku's ~1568px vision ceiling without downscaling.
    """
    path = Path(video_path)
    if not path.is_file():
        return []
    duration = get_duration_seconds(str(path))
    if duration <= 0:
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    cols, rows = grid
    cells = cols * rows
    panel_seconds = cells / fps

    # Step 1: sample frames at `fps` into a scratch subdir.
    scratch = out_dir / "_frames"
    scratch.mkdir(parents=True, exist_ok=True)
    cmd = [
        settings.ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-vf",
        f"fps={fps},scale={tile_width}:-2",
        "-q:v",
        "3",
        str(scratch / "f_%04d.jpg"),
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        _log.warning("watch_montage: ffmpeg frame sampling failed: %s", exc)
        return []

    frames = sorted(scratch.glob("f_*.jpg"))
    if not frames:
        _log.warning("watch_montage: ffmpeg produced 0 frames")
        return []

    # Step 2: Pillow tiles every `cells` frames into one mosaic.
    panels: list[Panel] = []
    for i in range(0, len(frames), cells):
        chunk = frames[i : i + cells]
        panel_idx = len(panels) + 1
        out_path = out_dir / f"panel_{panel_idx:03d}.jpg"
        try:
            _compose_mosaic(chunk, out_path, grid=grid, quality=quality)
        except Exception as exc:
            _log.warning("watch_montage: mosaic compose failed on %d: %s", panel_idx, exc)
            continue
        start_s = (panel_idx - 1) * panel_seconds
        end_s = min(duration, panel_idx * panel_seconds)
        panels.append(Panel(path=out_path, start_s=start_s, end_s=end_s, index=panel_idx))

    # Cleanup the scratch subdir -- its contents aren't needed after tiling.
    for f in scratch.glob("*"):
        with contextlib.suppress(OSError):
            f.unlink()
    with contextlib.suppress(OSError):
        scratch.rmdir()

    return panels


def _compose_mosaic(
    frame_paths: list[Path],
    out_path: Path,
    *,
    grid: tuple[int, int],
    quality: int,
) -> None:
    """Compose `frame_paths` (up to `grid[0]*grid[1]` items, left-to-right
    top-to-bottom) into a single JPEG. Missing cells are left black."""
    cols, rows = grid
    with Image.open(frame_paths[0]) as first:
        cell_w, cell_h = first.size
    tile_w = cell_w * cols
    tile_h = cell_h * rows
    mosaic = Image.new("RGB", (tile_w, tile_h), color=(0, 0, 0))
    for i, fp in enumerate(frame_paths):
        col = i % cols
        row = i // cols
        with Image.open(fp) as im:
            if im.size != (cell_w, cell_h):
                im = im.resize((cell_w, cell_h))
            mosaic.paste(im, (col * cell_w, row * cell_h))
    mosaic.save(out_path, format="JPEG", quality=quality, optimize=True)


# ---------------------------------------------------------------------
# Prompt -- canonical VTG-LLM-style timestamp emission
# ---------------------------------------------------------------------


# Bump when the prompt or schema changes -- invalidates any caller-side
# critique cache. Loop callers key their memoization on this value.
PROMPT_VERSION = 2

_SYSTEM_PROMPT = """\
You are a video editor reviewing a compiled highlight montage for a
YouTube Short / TikTok. Watch the whole thing, then return a critique.

You will see the montage as a sequence of 2x2 panels. Each panel shows
FOUR consecutive frames (top-left -> top-right -> bottom-left ->
bottom-right), sampled at 1 frame per second. Each panel is labeled
`[t=Xs-Ys]` so you know exactly which interval it covers. The panels
run chronologically -- panel 1 is the montage opener, the last panel
is the ending.

Your job: return a structured critique.

- **overall_verdict** -- the top-line answer.
- **opener_strength / climax_strength** -- 1-5, gut ratings.
- **pacing** -- too_slow / just_right / too_fast.
- **top_recommendation** -- the ONE change that would help most.
- **flags** -- zero or more intervals worth calling out. FLAG BOTH
  PROBLEMS AND HIGHLIGHTS so the editor sees what worked. Common
  flag categories:
  - `dead_air` -- walking, empty map, nothing happens
  - `scoreboard` -- post-round scoreboard / spectator view / KDA table
  - `own_death` -- the player DIES (killfeed shows them as victim,
    right side of the row). Flag this -- a montage should not feature
    the user's own deaths as payoffs.
  - `teammate_kill_only` -- the killfeed shows a teammate's kill but
    not the user's. Not a highlight of the user.
  - `payoff_lands` -- the user's kill / plant / clutch actually happens
    (killfeed shows them as killer, left side).
  - `good_buildup` -- peek prep, angle hold, ability setup that pays off.
  - `great_moment` -- ace, multi-kill, or clutch -- a keeper.
  - `abrupt_cut` -- the transition into the next clip is jarring.
  - `boring_pacing` -- nothing wrong but energy stays low too long.
  - `kill_anchor_misplaced` -- a killfeed pop is visible in the LAST
    ~1 second of one panel OR the FIRST ~1 second of the next panel.
    Kills are instant events; panels are 4-second buckets; a boundary
    kill means the sub-clip window was cut ~1s off the real anchor.
    Set `suggested_fix = "shift_left"` (kill visible at panel start ->
    push clip earlier) or `"shift_right"` (kill visible at panel end
    -> push clip later), with `fix_seconds` = 1.0-2.0.
  - `killfeed_pop_cut_off` -- a kill lands in the sub-clip but the
    killfeed row is cut off before it finishes animating in (~1s
    after the frag). Set `suggested_fix = "extend_after"` with
    `fix_seconds` = 1.0-1.5.
  - `cut_too_early` -- the payoff doesn't fully resolve before the
    next clip starts. `suggested_fix = "extend_after"`.
  - `cut_too_late` -- 2+ seconds of dead frames after the payoff.
    `suggested_fix = "trim_end"`.

MECHANICAL PRIORITY (v6 rubric): kills are the load-bearing content.
When a kill appears near a panel boundary, ALWAYS flag it as
`kill_anchor_misplaced` -- do not soft-flag it as `boring_pacing` or
`abrupt_cut`. The refine loop needs the mechanical category to know
how to fix the window; pacing categories only get a soft acknowledgement.

For actionable flags (kill_anchor_misplaced, killfeed_pop_cut_off,
cut_too_early, cut_too_late, boring_pacing at start/end), always set
`suggested_fix` + `fix_seconds`. For positive flags (great_moment,
payoff_lands, good_buildup) set `suggested_fix = "keep"`. Otherwise
leave both None.

Ground your timestamps in the panel labels -- cite the interval you
actually see. Do NOT invent times that fall outside the montage.

Valorant-specific reading tips:
- Killfeed is top-right. Killer is on the LEFT of each row, victim on
  the RIGHT. "Noodlz [weapon] X" means Noodlz killed X (good).
  "X [weapon] Noodlz" means X killed Noodlz (a death -- flag it).
- SPIKE PLANTED banner across mid-screen = objective moment.
- Scoreboard: leaderboard-style table with names + K/D/A columns.
- Buy phase: bright shop UI covering most of the frame.
"""


def _user_prompt(clip_count: int, total_seconds: float, panel_seconds: float) -> str:
    return (
        f"Here is a {total_seconds:.0f}-second Valorant highlight montage "
        f"({clip_count} panels, each panel = {panel_seconds:.0f}s). "
        "Watch the whole thing then emit the structured critique."
    )


# ---------------------------------------------------------------------
# Public entry -- one call, one critique
# ---------------------------------------------------------------------


def _encode(path: Path) -> str:
    with path.open("rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def _snap_to_panel(t: float, panels: list[Panel]) -> float:
    """Snap `t` to the nearest panel start boundary.

    Free correctness lift (see deep-research 2026-07-08): language
    models bias timestamps toward round numbers / priors; snapping to
    actually-sampled boundaries reclaims that bias.
    """
    if not panels:
        return t
    best = panels[0].start_s
    best_delta = abs(t - best)
    for p in panels:
        for candidate in (p.start_s, p.end_s):
            delta = abs(t - candidate)
            if delta < best_delta:
                best = candidate
                best_delta = delta
    return round(best, 2)


@dataclass(frozen=True)
class WatchResult:
    """Return shape of `watch_montage`. `error` is populated on failure
    (missing ffmpeg, VLM unavailable, video not found) -- otherwise
    `critique` is present."""

    critique: MontageCritique | None
    panel_count: int
    total_seconds: float
    error: str | None = None


def watch_montage(
    video_path: str | Path,
    *,
    fps: float = 1.0,
    grid: tuple[int, int] = (2, 2),
    max_panels: int = 30,
    api_key: str | None = None,
    model: str | None = None,
    spend_key: str | None = None,
) -> WatchResult:
    """Panel-pack `video_path` and return the VLM's structured critique.

    Cost model: at 1 fps + 2x2 packing, a 60s montage = 15 panels =
    one Haiku 4.5 call at ~$0.01-0.02. A 120s montage = 30 panels =
    ~$0.03. `max_panels` caps the input so a stray long input can't
    balloon the bill; over-cap montages get their tail dropped with a
    warning (single call is the whole point).

    `fps` sets the sampling rate (long-form story review uses 0.25).
    `spend_key` names the ledger row the call is recorded under (None =
    not recorded); callers check the budget for that key first.

    Never raises -- degrades to `WatchResult(critique=None, error=...)`.
    """
    path = Path(video_path)
    if not path.is_file():
        return WatchResult(
            critique=None,
            panel_count=0,
            total_seconds=0.0,
            error=f"video not found: {path}",
        )
    key = api_key if api_key is not None else settings.anthropic_api_key
    if not key:
        return WatchResult(
            critique=None,
            panel_count=0,
            total_seconds=0.0,
            error="ANTHROPIC_API_KEY is empty",
        )
    model_id = model or settings.vlm_anthropic_model
    total = get_duration_seconds(str(path))

    tmp = Path(tempfile.mkdtemp(prefix="watch_montage_"))
    try:
        panels = extract_panels(path, tmp, fps=fps, grid=grid)
        if not panels:
            return WatchResult(
                critique=None,
                panel_count=0,
                total_seconds=total,
                error="ffmpeg extracted 0 panels",
            )
        if len(panels) > max_panels:
            _log.info(
                "watch_montage: capping %d panels to %d (dropping tail)",
                len(panels),
                max_panels,
            )
            panels = panels[:max_panels]

        panel_seconds = (grid[0] * grid[1]) / fps

        # Interleave a text label with each panel image so the VLM has
        # unambiguous timestamps to anchor its critique on. Deep research
        # confirmed this is the transferable pattern from Qwen's
        # "Text-Timestamp Alignment" -- inject explicit timestamps
        # alongside frames rather than expecting the model to infer them.
        content: list[dict] = []
        for p in panels:
            content.append(
                {
                    "type": "text",
                    "text": f"[t={p.start_s:.0f}s-{p.end_s:.0f}s] panel {p.index}",
                }
            )
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": _encode(p.path),
                    },
                }
            )
        content.append({"type": "text", "text": _user_prompt(len(panels), total, panel_seconds)})

        client = anthropic.Anthropic(
            api_key=key,
            timeout=settings.vlm_call_timeout_seconds,
            max_retries=2,
        )
        try:
            resp = client.messages.parse(
                model=model_id,
                max_tokens=2048,
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": content}],
                output_format=MontageCritique,
            )
        except anthropic.APIStatusError as exc:
            _log.warning("watch_montage: vlm call failed: %s", exc)
            return WatchResult(
                critique=None,
                panel_count=len(panels),
                total_seconds=total,
                error=f"anthropic api error: {exc!s}",
            )

        record_spend(spend_key, "watch", model_id, getattr(resp, "usage", None))
        critique = resp.parsed_output
        if critique is None:
            return WatchResult(
                critique=None,
                panel_count=len(panels),
                total_seconds=total,
                error="parsed_output missing (parse failed)",
            )
        # Snap timestamps to panel boundaries. See _snap_to_panel above.
        snapped_flags = [
            MomentFlag(
                start_s=_snap_to_panel(f.start_s, panels),
                end_s=_snap_to_panel(f.end_s, panels),
                category=f.category,
                why=f.why,
            )
            for f in critique.flags
        ]
        critique = critique.model_copy(update={"flags": snapped_flags})

        return WatchResult(
            critique=critique,
            panel_count=len(panels),
            total_seconds=total,
        )
    finally:
        for f in tmp.glob("*"):
            with contextlib.suppress(OSError):
                f.unlink()
        with contextlib.suppress(OSError):
            tmp.rmdir()


# ---------------------------------------------------------------------
# Save-critique-as-markdown -- persist next to the montage
# ---------------------------------------------------------------------


def render_critique_markdown(result: WatchResult) -> str:
    """Render a `WatchResult` as human-readable markdown."""
    lines: list[str] = ["# Montage critique", ""]
    if result.error or result.critique is None:
        lines.append(f"**Error:** {result.error or 'unknown'}")
        return "\n".join(lines) + "\n"
    c = result.critique
    lines += [
        f"**Verdict:** `{c.overall_verdict}`  ",
        f"**Opener:** {c.opener_strength}/5  ",
        f"**Climax:** {c.climax_strength}/5  ",
        f"**Pacing:** {c.pacing}  ",
        f"**Panels:** {result.panel_count} @ {result.total_seconds:.0f}s",
        "",
        "## Top recommendation",
        "",
        f"{c.top_recommendation}",
        "",
        f"## Flags ({len(c.flags)})",
        "",
    ]
    if not c.flags:
        lines.append("_(none)_")
    else:
        lines.append("| t | category | why |")
        lines.append("|---|---|---|")
        for f in c.flags:
            why = f.why.replace("|", "\\|")
            lines.append(f"| {f.start_s:.0f}s-{f.end_s:.0f}s | `{f.category}` | {why} |")
    return "\n".join(lines) + "\n"


def save_critique(result: WatchResult, out_path: Path) -> None:
    """Write the critique markdown to `out_path`. Never raises."""
    with contextlib.suppress(OSError):
        out_path.write_text(render_critique_markdown(result), encoding="utf-8")
