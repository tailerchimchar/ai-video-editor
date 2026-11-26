"""Vision check of the early game (AVE-56).

Task A's `early_segments` come from per-minute Riot frames, so they miss bush
fights and trades that chunk someone to low HP between frames. This module
labels single 960-px frames (second burned in) from the owner's 0:45 to the
first back with one state each, turns runs of fight-like states into confirmed
segments, and confirms or trims the Riot-derived ones. Backend: whatever the
caller passes (`league.understand.default_backend`, honouring
`PLAY_CONFIRM_BACKEND`). Never raises; no prose or transcript text is stored.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ..candidates.probe import get_duration_seconds
from ..league.early_game import EarlyKind, EarlySegment
from .backends.base import VLMBackend, run_with_retry
from .play_confirm import _HUD_BASE, CONFIRM_MAX_TOKENS, sample_window

_log = logging.getLogger(__name__)

EARLY_STATES = ("laning", "trade", "bush_fight", "low_hp", "recall", "jungle", "dead")
EarlyState = Literal["laning", "trade", "bush_fight", "low_hp", "recall", "jungle", "dead"]

SAMPLE_START_S = 45.0
AFTER_BACK_S = 8.0
MAX_FRAMES = 120
RUN_PAD_S = 3.0
OPENING_BUSH_AFTER_START_S = 50.0  # a brawl opening starts at game 0:20; 1:10 is +50 s
NO_OPENING_BUSH_AFTER_SAMPLE_S = 25.0  # sampling starts at game 0:45; 1:10 is +25 s
_RUN_KINDS: dict[str, EarlyKind] = {
    "bush_fight": "opening",
    "trade": "level3_trade",
    "low_hp": "low_hp",
}


class _FrameState(BaseModel):
    second: int
    state: EarlyState


class _FrameStates(BaseModel):
    frames: list[_FrameState]


def _system(fps: float, n_frames: int) -> str:
    step = 1.0 / fps if fps > 0 else 0.0
    return (
        _HUD_BASE
        + f"You are given {n_frames} frames in time order, one every {step:g} seconds, from the "
        "first minutes of the game; each frame has its second burned in at the top-left.\n"
        "Label EVERY frame by its burned-in second with exactly one state:\n"
        "- 'laning': the owner farms or stands in lane, no real fight.\n"
        "- 'trade': the owner and an enemy are trading hits or abilities in lane.\n"
        "- 'bush_fight': a fight in or around a bush (tri-bush, river bush).\n"
        "- 'low_hp': the owner's health bar is very low (about a quarter or less).\n"
        "- 'recall': the owner is recalling or walking back to base.\n"
        "- 'jungle': the owner is in the jungle or river, not in lane.\n"
        "- 'dead': the owner is dead (grey screen).\n"
        'Return ONLY JSON: {"frames": [{"second": 0, "state": "laning"}, ...]}'
    )


def _schedule(
    segments: list[EarlySegment], first_back_s: float | None, fps: float, start_s: float
) -> tuple[float, float]:
    if first_back_s is not None:
        end = first_back_s + AFTER_BACK_S
    elif segments:
        end = max(s.end_s for s in segments)
    else:
        end = start_s + MAX_FRAMES / fps
    return start_s, min(end, start_s + MAX_FRAMES / fps)


def _runs(frames: list[_FrameState], start_s: float, step: float) -> list[tuple[str, float, float]]:
    """(state, first rec second, last rec second) of each contiguous run of a
    fight-like state."""
    runs: list[tuple[str, float, float]] = []
    prev: _FrameState | None = None
    for f in sorted(frames, key=lambda x: x.second):
        t = start_s + f.second
        if f.state in _RUN_KINDS:
            if prev and prev.state == f.state and f.second - prev.second <= step * 1.5:
                runs[-1] = (f.state, runs[-1][1], t)
            else:
                runs.append((f.state, t, t))
        prev = f
    return runs


MIN_VISION_SEGMENT_S = 10.0  # a vision-only run shorter than this reads as a flash cut


def _bush_is_opening(t: float, segments: list[EarlySegment], start_s: float) -> bool:
    openings = [s.start_s for s in segments if s.kind == "opening"]
    limit = (
        min(openings) + OPENING_BUSH_AFTER_START_S
        if openings
        else start_s + NO_OPENING_BUSH_AFTER_SAMPLE_S
    )
    return t < limit


def _merge(
    segments: list[EarlySegment], runs: list[tuple[str, float, float]], start_s: float
) -> list[EarlySegment]:
    out = [s.model_copy() for s in segments]
    for state, a, b in runs:
        lo, hi = a - RUN_PAD_S, b + RUN_PAD_S
        hits = [s for s in out if s.start_s <= b and s.end_s >= a]
        if hits:
            for s in hits:
                # Confirm and widen to the run, never shrink: the Riot-anchored
                # windows (opening 0:20/1:00-2:00, first back, recall) are the
                # owner's minimum early-game coverage (mentor 2026-11-25).
                s.confirmed = True
                s.start_s, s.end_s = max(0.0, min(s.start_s, lo)), max(s.end_s, hi)
            continue
        kind = _RUN_KINDS[state]
        if state == "bush_fight" and not _bush_is_opening(a, segments, start_s):
            kind = "level3_trade"
        out.append(
            EarlySegment(
                kind=kind,
                start_s=max(0.0, lo),
                end_s=max(hi, lo + MIN_VISION_SEGMENT_S),
                reason=f"vision: {state} frames {a:.0f}s-{b:.0f}s",
                confirmed=True,
            )
        )
    return sorted(out, key=lambda s: (s.start_s, s.end_s))


def confirm_early(
    video_path: str,
    segments: list[EarlySegment],
    *,
    first_back_s: float | None,
    backend: VLMBackend,
    fps: float = 0.5,
    start_s: float = SAMPLE_START_S,
) -> list[EarlySegment]:
    """Segments confirmed / trimmed / extended by one labelling call over frames
    from `start_s` (recording seconds, 0:45 by default) to first back + 8 s.
    Any failure returns `segments` unchanged with a warning."""
    start, end = _schedule(segments, first_back_s, fps, start_s)
    duration = get_duration_seconds(video_path)
    if duration > 0:
        end = min(end, duration)
    if end <= start:
        return segments
    try:
        with tempfile.TemporaryDirectory(prefix="early_confirm_") as tmp:
            frames = sample_window(video_path, Path(tmp), start_s=start, end_s=end, fps=fps)
            if not frames:
                _log.warning("early confirm skipped: no frames sampled")
                return segments
            labels = run_with_retry(
                backend,
                frames=frames,
                system_prompt=_system(fps, len(frames)),
                user_prompt=f"Label all {len(frames)} frames.",
                response_model=_FrameStates,
                max_tokens=CONFIRM_MAX_TOKENS,
            )
        return _merge(segments, _runs(labels.frames, start, 1.0 / fps), start)
    except Exception as exc:  # VLMUnavailableError, parse failure, spend cap: never fatal
        _log.warning("early confirm skipped: %s", exc)
        return segments
