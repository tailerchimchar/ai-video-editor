"""Arc planner: place scored clips on a song's arc and trim the song to match.

Pure (no I/O, no ffmpeg, no LLM). `plan_arc` returns an `EditPlan` that the
panel can override before anything renders.

Placement is a registry (`_PLACEMENTS`): a new way of laying clips on the
song is one function plus one entry. Each rule returns the clip order and
where, in cut time, the song's drop should land.

Mentor hard rule: a 1K/2K is never the payoff of an arc montage, so it is
never placed on the drop.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from ..edit_plan import EditPlan, PlanMusic, Rejected, SourceClip
from ..intensity import ClipWithIntensity
from ..taxonomy import kills_for_event
from .analysis import SongAnalysis
from .structure import SongStructure

MIN_DROP_KILLS = 3
"""A kill clip carries the drop only from a 3K up (an ace counts as 5).
Clips with no kill event (a clutch, an objective) are not kill clips."""

_TARGET_TOLERANCE = 0.05


@dataclass(frozen=True)
class Placement:
    ordered: list[ClipWithIntensity]
    peak_clip: ClipWithIntensity
    """The clip the song's drop should land inside."""
    peak_at_s: float
    """Where in the cut (seconds) the middle of `peak_clip` plays."""


def _length(clip: ClipWithIntensity) -> float:
    return clip.end - clip.start


def _by_intensity_desc(clips: list[ClipWithIntensity]) -> list[ClipWithIntensity]:
    return sorted(clips, key=lambda c: c.intensity, reverse=True)


def _can_carry_drop(clip: ClipWithIntensity) -> bool:
    kills = kills_for_event(clip.event_type)
    return kills == 0 or kills >= MIN_DROP_KILLS


def _drop_clip(clips: list[ClipWithIntensity]) -> ClipWithIntensity:
    eligible = [c for c in clips if _can_carry_drop(c)]
    if not eligible:
        raise ValueError("no clip is eligible for the drop (1K/2K never carry it)")
    return max(eligible, key=lambda c: c.intensity)


def _fill_to_target(
    drop: ClipWithIntensity, others: list[ClipWithIntensity], target_seconds: float
) -> list[ClipWithIntensity]:
    """Strongest clips first, until the cut reaches the target's tolerance band."""
    total = _length(drop)
    chosen: list[ClipWithIntensity] = []
    for clip in _by_intensity_desc(others):
        if total >= target_seconds * (1 - _TARGET_TOLERANCE):
            break
        if total + _length(clip) <= target_seconds * (1 + _TARGET_TOLERANCE):
            chosen.append(clip)
            total += _length(clip)
    return chosen


def _rising_share(structure: SongStructure) -> float:
    rising = structure.rising[1] - structure.rising[0]
    falling = structure.falling[1] - structure.falling[0]
    return rising / (rising + falling) if rising + falling > 0 else 0.5


def _place_arc_montage(
    clips: list[ClipWithIntensity],
    structure: SongStructure,
    target_seconds: float,
    rising_budget_s: float | None,
) -> Placement:
    """Build up to the drop clip in ascending intensity; the rest fall away.

    `rising_budget_s` pins how long the build-up may run (used when the song
    trim clamps); by default it follows the song's rising/falling proportions.
    """
    drop = _drop_clip(clips)
    rest = _fill_to_target(drop, [c for c in clips if c is not drop], target_seconds)
    budget = (
        sum(map(_length, rest)) * _rising_share(structure)
        if rising_budget_s is None
        else rising_budget_s
    )
    rising: list[ClipWithIntensity] = []
    used = 0.0
    for clip in _by_intensity_desc(rest):
        if used + _length(clip) <= budget:
            rising.append(clip)
            used += _length(clip)
    taken = {id(c) for c in rising}
    falling = [c for c in _by_intensity_desc(rest) if id(c) not in taken]
    rising.sort(key=lambda c: c.intensity)
    return Placement([*rising, drop, *falling], drop, used + _length(drop) / 2)


def _place_story(
    clips: list[ClipWithIntensity],
    structure: SongStructure,
    target_seconds: float,
    rising_budget_s: float | None,
) -> Placement:
    """Chronological order is the story; only the song moves to the best clip."""
    _ = structure, target_seconds, rising_budget_s
    ordered = sorted(clips, key=lambda c: c.start)
    best = max(ordered, key=lambda c: c.intensity)
    before = ordered[: ordered.index(best)]
    return Placement(ordered, best, sum(map(_length, before)) + _length(best) / 2)


_PLACEMENTS: dict[str, tuple[str, Callable[..., Placement]]] = {
    "arc_montage": ("arc_montage", _place_arc_montage),
    "story": ("story_short", _place_story),
}
"""Planner mode -> (plan format, placement rule)."""


def trim_to_peak(
    analysis: SongAnalysis, peak_at_s: float, target_seconds: float
) -> tuple[float, float]:
    """Song (start, end) so its drop plays `peak_at_s` into the cut.

    Clamped to the song: when it is too short to hold the drop at that
    position the drop lands as close as the song allows.
    """
    duration = analysis.structure.falling[1]
    start = analysis.structure.drop_s - peak_at_s
    start = max(min(start, duration - target_seconds), 0.0)
    return start, min(start + target_seconds, duration)


def _place_and_trim(
    place: Callable[..., Placement],
    clips: list[ClipWithIntensity],
    analysis: SongAnalysis,
    target_seconds: float,
) -> tuple[Placement, tuple[float, float]]:
    """Place, trim, and when the trim clamped re-place once so the drop still
    lands on the peak clip (the build-up is re-sized to the drop's real
    position in the trimmed song)."""
    structure = analysis.structure
    placement = place(clips, structure, target_seconds, None)
    trim = trim_to_peak(analysis, placement.peak_at_s, target_seconds)
    drop_in_cut = structure.drop_s - trim[0]
    if abs(drop_in_cut - placement.peak_at_s) > 1e-9:
        budget = max(drop_in_cut - _length(placement.peak_clip) / 2, 0.0)
        placement = place(clips, structure, target_seconds, budget)
        trim = trim_to_peak(analysis, placement.peak_at_s, target_seconds)
    return placement, trim


def _shortfalls(
    placement: Placement, trim: tuple[float, float], analysis: SongAnalysis, target_seconds: float
) -> list[Rejected]:
    """What the plan could not honour, for the panel to see."""
    found: list[Rejected] = []
    total = sum(map(_length, placement.ordered))
    if abs(total - target_seconds) > target_seconds * _TARGET_TOLERANCE:
        found.append(
            Rejected(
                wanted="target_seconds ±5%",
                why=f"{total:.1f} s from {len(placement.ordered)} clips",
            )
        )
    drop_in_cut = analysis.structure.drop_s - trim[0]
    if abs(drop_in_cut - placement.peak_at_s) > _length(placement.peak_clip) / 2:
        found.append(
            Rejected(
                wanted="drop inside the peak clip",
                why=(
                    f"drop plays at {drop_in_cut:.1f} s but peak clip "
                    f"{placement.peak_clip.clip_ref} is centred at {placement.peak_at_s:.1f} s"
                ),
            )
        )
    return found


def plan_arc(
    clips: list[ClipWithIntensity],
    analysis: SongAnalysis,
    format: Literal["story", "arc_montage"],
    target_seconds: float,
    *,
    song_id: str,
) -> EditPlan:
    """Plan the cut order and song trim.

    `song_id` is written verbatim to `music.song_id`: a library id or an
    existing audio path, whatever the executor can resolve. Anything the
    plan cannot honour (duration band, drop alignment) is recorded in
    `rejected`; only an empty pool or one with no drop-eligible clip raises.
    """
    if not clips:
        raise ValueError("no clips to plan")
    plan_format, place = _PLACEMENTS[format]
    placement, trim = _place_and_trim(place, clips, analysis, target_seconds)
    return EditPlan(
        format=plan_format,
        source_clips=[
            SourceClip(clip_ref=c.clip_ref, start=c.start, end=c.end, intensity=c.intensity)
            for c in placement.ordered
        ],
        order=[c.clip_ref for c in placement.ordered],
        music=PlanMusic(
            song_id=song_id, trim_to_peak=True, trim_start_s=trim[0], trim_end_s=trim[1]
        ),
        rejected=_shortfalls(placement, trim, analysis, target_seconds),
    )
