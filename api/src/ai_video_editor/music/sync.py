"""Beat-sync — align clip cut points to the music's beat grid.

Open/closed: `_SYNC_STRATEGIES` maps a strategy name to a function.
Different montage vibes = different strategies:
  - `kill_on_beat`   — each clip's anchor lands on the nearest beat
  - `end_on_beat`    — each clip's END lands on the nearest beat; starts never move
  - `drop_on_hook`   — reserved (align the first clip's start to a drop)
  - `beat_length_cut`— reserved (trim each clip to an integer number of beats)

Pure logic; no ffmpeg, no I/O. Callers pass in beat times + clip cut
windows and get back adjusted windows.

Determinism: same inputs → same outputs. No randomness.
"""

from __future__ import annotations

import bisect
import logging
from collections.abc import Callable
from dataclasses import dataclass

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# I/O shape — deliberately generic (not tied to ShortClip so the
# music module stays reusable outside the shorts pipeline)
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class ClipWindow:
    """One clip's cut window — start/end seconds into the source, plus
    the "anchor" moment we want to land on a beat (typically the kill).

    `anchor_seconds` is measured from `start_seconds` — i.e. it's the
    offset INTO this clip, not into the source video.
    """

    file: str
    start_seconds: float
    end_seconds: float
    anchor_offset_seconds: float
    """Where in the clip (relative to start) the highlight moment sits.
    E.g. a 10s clip with the kill at t=6s from clip-start → 6.0."""

    @property
    def duration(self) -> float:
        return self.end_seconds - self.start_seconds


@dataclass(frozen=True)
class SnappedClip:
    """Result of snapping one clip. `shift_seconds` is how much the
    start moved relative to the original (negative = trim start,
    positive = pre-roll)."""

    original: ClipWindow
    new_start_seconds: float
    new_end_seconds: float
    shift_seconds: float
    beat_time_seconds: float
    """The absolute beat time (in the music's timeline) the clip's
    anchor got aligned to."""


@dataclass(frozen=True)
class SnappedClips:
    """Ordered snap results for a whole montage."""

    strategy: str
    tolerance_seconds: float
    clips: tuple[SnappedClip, ...]

    @property
    def any_shifted(self) -> bool:
        return any(abs(c.shift_seconds) > 1e-6 for c in self.clips)


# ---------------------------------------------------------------------
# Strategies (open/closed hook — register new alignments here)
# ---------------------------------------------------------------------


def _nearest_beat(beat_time: float, beats: tuple[float, ...]) -> float:
    """Find the beat time in `beats` closest to `beat_time`.
    O(log N) via bisect. `beats` MUST be sorted ascending."""
    if not beats:
        return beat_time
    idx = bisect.bisect_left(beats, beat_time)
    if idx == 0:
        return beats[0]
    if idx == len(beats):
        return beats[-1]
    before = beats[idx - 1]
    after = beats[idx]
    return before if (beat_time - before) <= (after - beat_time) else after


def _snap_kill_on_beat(
    clips: list[ClipWindow],
    beats: tuple[float, ...],
    *,
    tolerance_seconds: float,
) -> list[SnappedClip]:
    """Walk clips in order; align each clip's anchor to the next beat
    in the montage timeline. Adjacent clips share the beat grid — the
    Nth clip's anchor lands N-or-so beats after the (N-1)th's anchor.

    A clip is shifted by adjusting its start_seconds; end stays put.
    If the required shift exceeds `tolerance_seconds`, we clamp to the
    tolerance and the anchor doesn't land perfectly on beat (still
    closer than no shift).
    """
    if not clips:
        return []
    snapped: list[SnappedClip] = []
    # Running position in the *montage* timeline (i.e. the concatenated
    # output). The first clip starts at t=0 in the montage.
    montage_t = 0.0
    for clip in clips:
        # The anchor's target time in the montage timeline
        anchor_in_montage = montage_t + clip.anchor_offset_seconds
        # Snap to the nearest beat in the music timeline (assumes the
        # music starts at t=0 in the montage too — the montage's
        # audio track begins at frame 0)
        target_beat = _nearest_beat(anchor_in_montage, beats)
        raw_shift = target_beat - anchor_in_montage
        # Clamp to tolerance so we don't trim off huge chunks
        shift = max(-tolerance_seconds, min(tolerance_seconds, raw_shift))
        new_start = clip.start_seconds - shift
        new_end = clip.end_seconds
        # Don't allow negative start (would fail ffmpeg trim)
        if new_start < 0:
            _log.debug(
                "clip %s: shift would produce negative start (%.3f); clamping",
                clip.file,
                new_start,
            )
            shift = clip.start_seconds  # shift all the way back to 0
            new_start = 0.0
        new_duration = new_end - new_start
        snapped.append(
            SnappedClip(
                original=clip,
                new_start_seconds=new_start,
                new_end_seconds=new_end,
                shift_seconds=shift,
                beat_time_seconds=target_beat,
            )
        )
        montage_t += new_duration
    return snapped


_MIN_CLIP_SECONDS = 0.5


def _snap_end_on_beat(
    clips: list[ClipWindow],
    beats: tuple[float, ...],
    *,
    tolerance_seconds: float,
) -> list[SnappedClip]:
    """Move only each clip's END onto the nearest beat of the montage timeline.

    Starts are never touched (a fight window's start is fixed). A clip whose
    nearest beat is further than `tolerance_seconds` away, or whose snap
    would leave it shorter than `_MIN_CLIP_SECONDS`, is left unchanged.
    """
    snapped: list[SnappedClip] = []
    montage_t = 0.0
    for clip in clips:
        end_in_montage = montage_t + clip.duration
        target_beat = _nearest_beat(end_in_montage, beats)
        shift = target_beat - end_in_montage
        if abs(shift) > tolerance_seconds or clip.duration + shift < _MIN_CLIP_SECONDS:
            shift = 0.0
        snapped.append(
            SnappedClip(
                original=clip,
                new_start_seconds=clip.start_seconds,
                new_end_seconds=clip.end_seconds + shift,
                shift_seconds=shift,
                beat_time_seconds=target_beat,
            )
        )
        montage_t += clip.duration + shift
    return snapped


def _snap_beat_length_cut(
    clips: list[ClipWindow],
    beats: tuple[float, ...],
    *,
    tolerance_seconds: float,
) -> list[SnappedClip]:
    """Placeholder for future strategy: trim each clip so its duration
    is an integer number of beats. Currently no-op passthrough."""
    _ = beats, tolerance_seconds
    return [
        SnappedClip(
            original=c,
            new_start_seconds=c.start_seconds,
            new_end_seconds=c.end_seconds,
            shift_seconds=0.0,
            beat_time_seconds=0.0,
        )
        for c in clips
    ]


_SYNC_STRATEGIES: dict[
    str,
    Callable[[list[ClipWindow], tuple[float, ...]], list[SnappedClip]],
] = {
    "kill_on_beat": _snap_kill_on_beat,
    "end_on_beat": _snap_end_on_beat,
    "beat_length_cut": _snap_beat_length_cut,
}


# ---------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------


def snap_clips_to_beats(
    clips: list[ClipWindow],
    beats: tuple[float, ...],
    *,
    strategy: str = "kill_on_beat",
    tolerance_seconds: float = 0.5,
) -> SnappedClips:
    """Align a montage's clip cut windows to a music track's beats.

    `tolerance_seconds` bounds how much any single clip can shift —
    protects against snapping a clip so hard it loses the payoff. 0.5s
    is a good default (half a second of tightening still lands cleanly
    on beats at 120bpm).

    Pure — no ffmpeg, no I/O. `clips` are not mutated.
    """
    if strategy not in _SYNC_STRATEGIES:
        raise ValueError(
            f"unknown sync strategy {strategy!r}; expected one of {sorted(_SYNC_STRATEGIES)}"
        )
    sorted_beats = tuple(sorted(beats))
    fn = _SYNC_STRATEGIES[strategy]
    # All strategies share the same tolerance kwarg for uniformity.
    results = fn(clips, sorted_beats, tolerance_seconds=tolerance_seconds)  # type: ignore[call-arg]
    return SnappedClips(
        strategy=strategy,
        tolerance_seconds=tolerance_seconds,
        clips=tuple(results),
    )
