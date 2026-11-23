"""Montage v2 planning: jump-cut each play, fit plays into a slot (D8, R9).

Pure functions over `Play` and Riot kill times (recording seconds). Plays
from several games are keyed by `(asset_id, play.id)`: ids like `p01`
repeat across games."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
    asset_id: str = ""

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
    asset_id: str = "",
) -> list[Segment]:
    """Cut one play into segments: engage - lead to outcome, dead time between
    kills longer than `gap_s` removed (each segment ends 1 s after a kill, the
    next starts 1.5 s before the next kill). Only the final segment is_last.
    Kill times move by the play's banner `offset_correction_s` when set (D17)."""
    rt = play.recording_time
    engage = play.window.engage_s if play.window.engage_s is not None else rt.first_kill_s - 8.0
    outcome = play.window.outcome_s if play.window.outcome_s is not None else rt.last_kill_s + 3.0
    shift = play.window.offset_correction_s or 0.0
    kills = sorted(k + shift for k in kill_times_s)

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
            asset_id=asset_id,
        )
        for i, (s, e, ks) in enumerate(bounds)
    ]


Key = tuple[str, str]


def _lookup(table: Mapping, key: Key, default=None):
    """`table[(asset_id, play_id)]`, else `table[play_id]` (single-game callers)."""
    if key in table:
        return table[key]
    return table.get(key[1], default)


def montage_eligible(play: Play) -> bool:
    """Owner-involved plays whose kill times are trusted (D16)."""
    return play.owner.involved and "offset_uncertain" not in play.window.flags


def fit_plays(
    plays: Sequence[Play] | Sequence[tuple[str, Play]],
    *,
    slot_s: float,
    kill_times: Mapping,
    sources: Mapping,
    gap_s: float,
) -> MontagePlan:
    """Rank plays, drop the lowest-ranked until the cut fits `slot_s` (never
    below two plays), then order rising -> best -> falling.

    `plays` is a list of `Play` (one game) or of `(asset_id, Play)` pairs
    (several games); `kill_times` and `sources` are keyed by
    `(asset_id, play.id)`, or by `play.id` for one game. Only owner-involved
    plays without `offset_uncertain` are considered (D16). Ranking works on
    copies: the caller's plays are never mutated. `dropped` holds play ids
    (`asset_id:play_id` when an asset id is given)."""
    pairs = [item if isinstance(item, tuple) else ("", item) for item in plays]
    copies = [(aid, p.model_copy(deep=True)) for aid, p in pairs if montage_eligible(p)]
    key_of = {id(p): (aid, p.id) for aid, p in copies}
    ranked = rank_plays([p for _, p in copies])

    def label(p: Play) -> str:
        aid, pid = key_of[id(p)]
        return f"{aid}:{pid}" if aid else pid

    built: dict[Key, list[Segment]] = {}
    for p in ranked:
        key = key_of[id(p)]
        built[key] = jump_cut_segments(
            p,
            _lookup(kill_times, key, []),
            source=_lookup(sources, key),
            gap_s=gap_s,
            asset_id=key[0],
        )

    def total(ps: list[Play]) -> float:
        return sum(s.duration_s for p in ps for s in built[key_of[id(p)]])

    kept = list(ranked)
    dropped: list[str] = []
    while total(kept) > slot_s and len(kept) > 2:
        dropped.append(label(kept.pop()))

    before = [p for i, p in enumerate(kept[1:]) if i % 2 == 0]
    after = [p for i, p in enumerate(kept[1:]) if i % 2 == 1]
    ordered = list(reversed(before)) + kept[:1] + after
    return MontagePlan(
        segments=[s for p in ordered for s in built[key_of[id(p)]]],
        dropped=dropped,
        over_slot=total(kept) > slot_s,
    )


def segment_to_spec(seg: Segment, *, pool_start_s: float = 0.0) -> PlaySpec:
    """The `PlaySpec` for one segment. `pool_start_s` is where the segment's
    source file starts in the recording (a month-pool cut starts at its
    padded window start), so times shift into that file's timebase. The
    play id carries the asset (`asset_id:play_id`) so plays from two games
    never merge."""
    start = seg.start_s - pool_start_s
    clamp = max(0.0, -start)
    return PlaySpec(
        clip_path=seg.source,
        start_seconds=start + clamp,
        end_seconds=seg.end_s - pool_start_s,
        play_id=f"{seg.asset_id}:{seg.play_id}" if seg.asset_id else seg.play_id,
        is_last=seg.is_last,
        kill_offsets_s=[k - clamp for k in seg.kill_offsets_s if k >= clamp],
    )
