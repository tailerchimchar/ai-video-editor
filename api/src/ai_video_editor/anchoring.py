"""Ground-truth anchor snapping — pure math, no I/O.

Given a set of kill anchors from `candidates/overwolf_events.py` and
a proposed sub-clip window (either hand-picked or from a candidate),
snap the window so the kill lands where the viewer expects it. This
is the mechanical layer of the anchor+critic pipeline: deterministic,
free, always correct when the log has the file.

Two entry points:

* `snap_window_to_anchor` — take an existing window, snap to nearest
  anchor within `max_snap_distance`. Falls through unchanged when no
  anchor is close enough (e.g. quote-setup clip that isn't a kill).
* `auto_windows_from_anchors` — no input window, just anchors → one
  sub-clip window per anchor, merged when they overlap. This is the
  "auto-cut kills from source" path.

Per-payoff-type buffers: an ace warrants ~5s of buildup so the viewer
sees the enemies stacking up before the frag chain; a solo kill only
needs ~2-3s. Trailing buffer covers the killfeed pop + kill sfx tail.
"""

from __future__ import annotations

from dataclasses import dataclass

from .candidates.overwolf_events import EventAnchor


@dataclass(frozen=True)
class SnappedWindow:
    """One trim window with provenance for logging / UI."""

    start_seconds: float
    end_seconds: float
    anchor_seconds: float | None  # None when no anchor matched
    event_type: str | None
    snapped: bool  # True when we moved the window to hit a kill


# Per-payoff-type buffers. Ace/quad get more lead-in so buildup lands.
# Trailing buffer is universal >=1s so the killfeed pop is visible in
# the output — the whole point of ground-truth snapping.
_PRE_BY_TYPE: dict[str, float] = {
    "kill": 3.0,
    "multi_kill_2": 4.0,
    "multi_kill_3": 5.0,
    "multi_kill_4": 5.0,
    "ace": 6.0,
}
_POST_BY_TYPE: dict[str, float] = {
    "kill": 2.5,
    "multi_kill_2": 3.0,
    "multi_kill_3": 3.0,
    "multi_kill_4": 3.5,
    "ace": 4.0,
}
_DEFAULT_PRE = 3.0
_DEFAULT_POST = 2.5

# How close a kill anchor must be to the window's midpoint to count
# as "the kill this clip is about". Beyond this, we assume the clip
# was intentionally NOT about a kill (a quote, a plant, a defuse) and
# leave the window alone.
_DEFAULT_MAX_SNAP_DISTANCE = 5.0


def buffers_for(event_type: str) -> tuple[float, float]:
    """(pre, post) seconds for an anchor of this type. Public — the
    HTTP layer needs it for `list_kill_anchors` previews."""
    return (
        _PRE_BY_TYPE.get(event_type, _DEFAULT_PRE),
        _POST_BY_TYPE.get(event_type, _DEFAULT_POST),
    )


def snap_window_to_anchor(
    start_seconds: float,
    end_seconds: float,
    anchors: list[EventAnchor],
    *,
    duration_seconds: float,
    max_snap_distance: float = _DEFAULT_MAX_SNAP_DISTANCE,
) -> SnappedWindow:
    """Snap `[start, end]` to the nearest kill anchor within
    `max_snap_distance` of the window midpoint. Falls through
    unchanged (snapped=False, anchor=None) when nothing is close.

    Buffers pulled from `_PRE_BY_TYPE` / `_POST_BY_TYPE` per anchor's
    `event_type`. Window is clamped to `[0, duration_seconds]`.
    """
    if end_seconds <= start_seconds:
        raise ValueError("end_seconds must be > start_seconds")
    if not anchors:
        return SnappedWindow(start_seconds, end_seconds, None, None, snapped=False)

    midpoint = (start_seconds + end_seconds) / 2.0
    closest = min(anchors, key=lambda a: abs(a.anchor_seconds - midpoint))
    if abs(closest.anchor_seconds - midpoint) > max_snap_distance:
        return SnappedWindow(start_seconds, end_seconds, None, None, snapped=False)

    pre, post = buffers_for(closest.event_type)
    new_start = max(0.0, closest.anchor_seconds - pre)
    new_end = min(duration_seconds, closest.anchor_seconds + post)
    return SnappedWindow(
        start_seconds=round(new_start, 2),
        end_seconds=round(new_end, 2),
        anchor_seconds=round(closest.anchor_seconds, 2),
        event_type=closest.event_type,
        snapped=(new_start != start_seconds or new_end != end_seconds),
    )


def auto_windows_from_anchors(
    anchors: list[EventAnchor],
    *,
    duration_seconds: float,
    merge_gap_seconds: float = 1.0,
) -> list[SnappedWindow]:
    """One window per anchor, per-type buffers. Overlapping / touching
    windows (within `merge_gap_seconds`) merge into one bigger window
    that keeps the FIRST anchor's event_type + midpoint anchor.

    Merge matters because Overwolf emits both individual kills AND
    the multi-kill cluster row. Without merge you'd get 4 overlapping
    windows for one ace. With merge, one clean window covering the
    whole burst.
    """
    if not anchors:
        return []
    sorted_anchors = sorted(anchors, key=lambda a: a.anchor_seconds)
    windows: list[SnappedWindow] = []
    for a in sorted_anchors:
        pre, post = buffers_for(a.event_type)
        w = SnappedWindow(
            start_seconds=max(0.0, a.anchor_seconds - pre),
            end_seconds=min(duration_seconds, a.anchor_seconds + post),
            anchor_seconds=a.anchor_seconds,
            event_type=a.event_type,
            snapped=True,
        )
        if windows and w.start_seconds <= windows[-1].end_seconds + merge_gap_seconds:
            prev = windows[-1]
            windows[-1] = SnappedWindow(
                start_seconds=prev.start_seconds,
                end_seconds=max(prev.end_seconds, w.end_seconds),
                anchor_seconds=prev.anchor_seconds,
                event_type=prev.event_type,
                snapped=True,
            )
        else:
            windows.append(w)
    return [
        SnappedWindow(
            start_seconds=round(w.start_seconds, 2),
            end_seconds=round(w.end_seconds, 2),
            anchor_seconds=round(w.anchor_seconds, 2) if w.anchor_seconds else None,
            event_type=w.event_type,
            snapped=w.snapped,
        )
        for w in windows
    ]
