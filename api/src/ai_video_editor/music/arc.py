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

from ..edit_plan import EditPlan, PlanMusic, SourceClip
from ..intensity import ClipWithIntensity
from .analysis import SongAnalysis
from .structure import SongStructure

NOT_ON_THE_DROP = frozenset({"kill", "multi_kill_2"})
"""Topics for a 1K/2K; the mentor never lets these carry the drop."""

_TARGET_TOLERANCE = 0.05


@dataclass(frozen=True)
class Placement:
    ordered: list[ClipWithIntensity]
    peak_at_s: float
    """Where in the cut (seconds) the song's drop must land."""


def _length(clip: ClipWithIntensity) -> float:
    return clip.end - clip.start


def _by_intensity_desc(clips: list[ClipWithIntensity]) -> list[ClipWithIntensity]:
    return sorted(clips, key=lambda c: c.intensity, reverse=True)


def _drop_clip(clips: list[ClipWithIntensity]) -> ClipWithIntensity:
    eligible = [c for c in clips if c.topic not in NOT_ON_THE_DROP]
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
    clips: list[ClipWithIntensity], structure: SongStructure, target_seconds: float
) -> Placement:
    """Build up to the drop clip in ascending intensity; the rest fall away."""
    drop = _drop_clip(clips)
    rest = _fill_to_target(drop, [c for c in clips if c is not drop], target_seconds)
    budget = sum(map(_length, rest)) * _rising_share(structure)
    rising: list[ClipWithIntensity] = []
    used = 0.0
    for clip in _by_intensity_desc(rest):
        if used + _length(clip) <= budget:
            rising.append(clip)
            used += _length(clip)
    taken = {id(c) for c in rising}
    falling = [c for c in _by_intensity_desc(rest) if id(c) not in taken]
    rising.sort(key=lambda c: c.intensity)
    return Placement([*rising, drop, *falling], used + _length(drop) / 2)


def _place_story(
    clips: list[ClipWithIntensity], structure: SongStructure, target_seconds: float
) -> Placement:
    """Chronological order is the story; only the song moves to the best clip."""
    _ = structure, target_seconds
    ordered = sorted(clips, key=lambda c: c.start)
    best = max(ordered, key=lambda c: c.intensity)
    before = ordered[: ordered.index(best)]
    return Placement(ordered, sum(map(_length, before)) + _length(best) / 2)


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


def plan_arc(
    clips: list[ClipWithIntensity],
    analysis: SongAnalysis,
    format: Literal["story", "arc_montage"],
    target_seconds: float,
) -> EditPlan:
    """Plan the cut order and song trim. The song is identified by the
    sha256 of its bytes; callers map that to a library id if they need one."""
    plan_format, place = _PLACEMENTS[format]
    placement = place(clips, analysis.structure, target_seconds)
    trim_start, trim_end = trim_to_peak(analysis, placement.peak_at_s, target_seconds)
    return EditPlan(
        format=plan_format,
        source_clips=[
            SourceClip(clip_ref=c.clip_ref, start=c.start, end=c.end, intensity=c.intensity)
            for c in placement.ordered
        ],
        order=[c.clip_ref for c in placement.ordered],
        music=PlanMusic(
            song_id=analysis.file_sha256,
            trim_to_peak=True,
            trim_start_s=trim_start,
            trim_end_s=trim_end,
        ),
    )
