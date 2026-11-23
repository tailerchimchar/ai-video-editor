"""Montage v2 planning: jump-cut each play, fit plays into a slot (D8, R9).

Pure functions over `Play` and Riot kill times (recording seconds)."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field, model_validator

from ..league.play_models import Play
from ..league.plays import rank_plays

_AFTER_KILL_S = 1.0
_BEFORE_KILL_S = 1.5


class PlaySpec(BaseModel):
    """One authoritative play segment handed to `build_custom_montage`."""

    clip_path: str
    start_seconds: float
    end_seconds: float
    play_id: str
    is_last: bool = False
    kill_offsets_s: list[float] = Field(default_factory=list)

    @model_validator(mode="after")
    def _window_positive(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


@dataclass(frozen=True)
class Segment:
    source: str
    start_s: float
    end_s: float
    play_id: str
    is_last: bool
    kill_offsets_s: list[float] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


@dataclass(frozen=True)
class MontagePlan:
    segments: list[Segment]
    dropped: list[str]
    over_slot: bool


def jump_cut_segments(
    play: Play,
    kill_times_s: list[float],
    *,
    source: str,
    gap_s: float = 3.0,
    lead_s: float = 3.0,
) -> list[Segment]:
    """Cut one play into segments: engage - lead to outcome, dead time between
    kills longer than `gap_s` removed (each segment ends 1 s after a kill, the
    next starts 1.5 s before the next kill). Only the final segment is_last."""
    rt = play.recording_time
    engage = play.window.engage_s if play.window.engage_s is not None else rt.first_kill_s - 8.0
    outcome = play.window.outcome_s if play.window.outcome_s is not None else rt.last_kill_s + 3.0
    kills = sorted(kill_times_s)

    # Group kills into runs split wherever the gap is too long.
    groups: list[list[float]] = []
    for k in kills:
        if groups and k - groups[-1][-1] <= gap_s:
            groups[-1].append(k)
        else:
            groups.append([k])

    bounds: list[tuple[float, float, list[float]]] = []
    start = engage - lead_s
    for i, group in enumerate(groups):
        if i + 1 < len(groups):
            bounds.append((start, group[-1] + _AFTER_KILL_S, group))
            start = groups[i + 1][0] - _BEFORE_KILL_S
        else:
            bounds.append((start, outcome, group))
    if not groups:
        bounds.append((start, outcome, []))

    last = len(bounds) - 1
    return [
        Segment(
            source=source,
            start_s=s,
            end_s=e,
            play_id=play.id,
            is_last=i == last,
            kill_offsets_s=[k - s for k in ks],
        )
        for i, (s, e, ks) in enumerate(bounds)
    ]


def fit_plays(
    plays: list[Play],
    *,
    slot_s: float,
    kill_times: dict[str, list[float]],
    sources: dict[str, str],
    gap_s: float,
) -> MontagePlan:
    """Rank plays, drop the lowest-ranked until the cut fits `slot_s` (never
    below two plays), then order rising -> best -> falling."""
    ranked = rank_plays(list(plays))
    built = {
        p.id: jump_cut_segments(p, kill_times.get(p.id, []), source=sources[p.id], gap_s=gap_s)
        for p in ranked
    }

    def total(ps: list[Play]) -> float:
        return sum(s.duration_s for p in ps for s in built[p.id])

    kept = list(ranked)
    dropped: list[str] = []
    while total(kept) > slot_s and len(kept) > 2:
        dropped.append(kept.pop().id)

    before = [p for i, p in enumerate(kept[1:]) if i % 2 == 0]
    after = [p for i, p in enumerate(kept[1:]) if i % 2 == 1]
    ordered = list(reversed(before)) + kept[:1] + after
    return MontagePlan(
        segments=[s for p in ordered for s in built[p.id]],
        dropped=dropped,
        over_slot=total(kept) > slot_s,
    )
