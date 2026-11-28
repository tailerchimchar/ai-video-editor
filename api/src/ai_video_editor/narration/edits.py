"""Pure take math for the narration editor (no I/O).

Takes live on the video timeline; "effective" time is `start_s + offset_s`.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence

from ai_video_editor.narration.models import (
    MIN_TAKE_S,
    CameraRegion,
    GameAudioRegion,
    NarrationSession,
    Take,
    TakeEdit,
)


class TakeTooShortError(ValueError):
    """A take would end up shorter than MIN_TAKE_S."""


class OverlapError(ValueError):
    """Two regions overlap."""


def apply_edit(take: Take, edit: TakeEdit) -> Take:
    """Return a copy of `take` with the edit's set fields applied."""
    changes = edit.model_dump(exclude_none=True)
    data = take.model_dump() | changes
    # Only edits that change the span are held to the minimum, so a short
    # take (from before the upload check) can still be muted or re-gained.
    touches_span = bool({"duration_s", "start_s", "media_in_s"} & changes.keys())
    if touches_span and data["duration_s"] < MIN_TAKE_S:
        raise TakeTooShortError(f"take must be at least {MIN_TAKE_S}s")
    return Take.model_validate(data)


def split_take(take: Take, at_s: float, new_id: str) -> tuple[Take, Take]:
    """Split a take at `at_s` (effective time) into two takes sharing media."""
    start = take.start_effective_s
    end = start + take.duration_s
    if not (start + MIN_TAKE_S <= at_s <= end - MIN_TAKE_S):
        raise ValueError("split point too close to the take's ends")
    first = take.model_copy(update={"duration_s": at_s - start})
    second = take.model_copy(
        update={
            "id": new_id,
            "start_s": at_s - take.offset_s,
            "media_in_s": take.media_in_s + (at_s - start),
            "duration_s": end - at_s,
        }
    )
    return first, second


def _trim_tail(take: Take, until: float) -> Take | None:
    dur = until - take.start_effective_s
    return take.model_copy(update={"duration_s": dur}) if dur >= MIN_TAKE_S else None


def _trim_head(take: Take, since: float) -> Take | None:
    cut = since - take.start_effective_s
    dur = take.duration_s - cut
    if dur < MIN_TAKE_S:
        return None
    return take.model_copy(
        update={
            "start_s": take.start_s + cut,
            "media_in_s": take.media_in_s + cut,
            "duration_s": dur,
        }
    )


def punch_in(takes: list[Take], new: Take, new_id: Callable[[], str]) -> list[Take]:
    """Append `new`, trimming, dropping or splitting the takes it covers."""
    n_start = new.start_effective_s
    n_end = n_start + new.duration_s
    out: list[Take] = []
    for old in takes:
        o_start = old.start_effective_s
        o_end = o_start + old.duration_s
        if o_end <= n_start or o_start >= n_end:
            out.append(old)
            continue
        before = o_start < n_start
        after = o_end > n_end
        if before and after:
            tail = _trim_head(old.model_copy(update={"id": new_id()}), n_end)
            head = _trim_tail(old, n_start)
            out.extend(t for t in (head, tail) if t)
        elif before:
            if trimmed := _trim_tail(old, n_start):
                out.append(trimmed)
        elif after:
            if trimmed := _trim_head(old, n_end):
                out.append(trimmed)
        # else: fully covered, dropped
    out.append(new)
    return out


def shift_session(session: NarrationSession, shift_s: float) -> NarrationSession:
    """Move every take and region by `shift_s`, clamping at 0 (relock).

    A take's effective start (`start_s + offset_s`) is clamped too: when a
    negative shift would put it before 0, `start_s` is raised so it lands on 0
    (the duration and the nudge are kept).
    """
    takes = [
        t.model_copy(update={"start_s": max(0.0, t.start_s + shift_s, -t.offset_s)})
        for t in session.takes
    ]

    def _shift(regions):
        out = []
        for r in regions:
            if r.end_s + shift_s <= 0:
                continue
            out.append(
                r.model_copy(
                    update={"start_s": max(0.0, r.start_s + shift_s), "end_s": r.end_s + shift_s}
                )
            )
        return out

    return session.model_copy(
        update={
            "takes": takes,
            "game_regions": _shift(session.game_regions),
            "camera_regions": _shift(session.camera_regions),
        }
    )


def check_no_overlap(regions: Sequence[GameAudioRegion | CameraRegion]) -> None:
    """Raise OverlapError if any two regions overlap (touching is fine)."""
    ordered = sorted(regions, key=lambda r: r.start_s)
    for a, b in itertools.pairwise(ordered):
        if a.end_s > b.start_s:
            raise OverlapError(f"regions {a.id} and {b.id} overlap")


def media_refcount(takes: list[Take], media: str) -> int:
    """How many takes play from this media file."""
    return sum(1 for t in takes if t.media == media)
