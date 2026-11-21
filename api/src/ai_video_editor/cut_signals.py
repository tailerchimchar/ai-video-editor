"""ffmpeg cut signals: quiet spans, short-step loudness, scene changes.

I/O shell around a pure stderr parser. Callers inject a runner so unit
tests never need a real ffmpeg binary; production uses subprocess.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from .config import settings

_log = logging.getLogger(__name__)

# --- public dataclasses ---------------------------------------------------


@dataclass(frozen=True)
class QuietSpan:
    start: float
    end: float


@dataclass(frozen=True)
class LoudnessStep:
    """Momentary loudness (LUFS M) at `time` seconds in the file."""

    time: float
    loudness: float


@dataclass(frozen=True)
class CutSignals:
    quiet_spans: list[QuietSpan] = field(default_factory=list)
    loudness: list[LoudnessStep] = field(default_factory=list)
    scene_changes: list[float] = field(default_factory=list)


# --- pure parser ----------------------------------------------------------

_SILENCE_START = re.compile(r"silence_start:\s*([-\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*([-\d.]+)")
# ebur128 momentary line, e.g. "t: 1.2  TARGET:-23 LUFS    M: -21.8 S:..."
# Match -inf/inf before a bare number so "M: -inf" does not capture "-".
_EBUR_M = re.compile(
    r"t:\s*([-\d.]+)\s+TARGET:.*?M:\s*(-?inf|-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
# scdet: "lavfi.scd.score: 30.467, lavfi.scd.time: 2"
_SCD_TIME = re.compile(r"lavfi\.scd\.time:\s*([-\d.]+)")
# select+metadata scene fallback
_SCENE_SCORE = re.compile(r"lavfi\.scene_score\s*=\s*([-\d.]+)")
_PTS_TIME = re.compile(r"pts_time:\s*([-\d.]+)")


def parse_ffmpeg_stderr(text: str, *, time_offset: float = 0.0) -> CutSignals:
    """Parse silencedetect / ebur128 / scdet (or scene select) lines.

    Times in ffmpeg's filter log are relative to the decoded window when
    `-ss` is applied before `-i`; pass `time_offset` (= window start) so
    returned values are absolute positions in the source file.

    Pure — no I/O.
    """
    quiet: list[QuietSpan] = []
    loudness: list[LoudnessStep] = []
    scenes: list[float] = []
    pending_start: float | None = None
    last_pts: float | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        m = _SILENCE_START.search(line)
        if m:
            pending_start = float(m.group(1)) + time_offset
            continue

        m = _SILENCE_END.search(line)
        if m and pending_start is not None:
            end = float(m.group(1)) + time_offset
            if end > pending_start:
                quiet.append(QuietSpan(round(pending_start, 3), round(end, 3)))
            pending_start = None
            continue

        m = _EBUR_M.search(line)
        if m:
            t = float(m.group(1)) + time_offset
            loud_s = m.group(2).lower()
            loud = -120.0 if loud_s in ("inf", "-inf") else float(loud_s)
            loudness.append(LoudnessStep(round(t, 3), round(loud, 2)))
            continue

        m = _SCD_TIME.search(line)
        if m:
            scenes.append(round(float(m.group(1)) + time_offset, 3))
            continue

        m = _PTS_TIME.search(line)
        if m:
            last_pts = float(m.group(1)) + time_offset
        m = _SCENE_SCORE.search(line)
        if m and last_pts is not None and float(m.group(1)) > 0:
            scenes.append(round(last_pts, 3))
            last_pts = None

    # Dedupe scene times while preserving order.
    seen: set[float] = set()
    uniq_scenes: list[float] = []
    for t in scenes:
        if t not in seen:
            seen.add(t)
            uniq_scenes.append(t)

    return CutSignals(quiet_spans=quiet, loudness=loudness, scene_changes=uniq_scenes)


def merge_signals(*parts: CutSignals) -> CutSignals:
    """Combine signals from multiple ffmpeg passes (pure)."""
    quiet: list[QuietSpan] = []
    loudness: list[LoudnessStep] = []
    scenes: list[float] = []
    for part in parts:
        quiet.extend(part.quiet_spans)
        loudness.extend(part.loudness)
        scenes.extend(part.scene_changes)
    quiet.sort(key=lambda s: s.start)
    loudness.sort(key=lambda s: s.time)
    scenes = sorted(set(scenes))
    return CutSignals(quiet_spans=quiet, loudness=loudness, scene_changes=scenes)


# --- I/O shell ------------------------------------------------------------


class FfmpegRunner(Protocol):
    """Return combined stdout+stderr text from one ffmpeg argv list."""

    def __call__(self, argv: list[str]) -> str: ...


def default_runner(argv: list[str]) -> str:
    """Real ffmpeg subprocess runner. Never uses shell=True."""
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        _log.warning("cut_signals: ffmpeg runner failed: %s", exc)
        return ""
    # Filters log to stderr; keep both in case a build differs.
    return (result.stderr or "") + (result.stdout or "")


def _audio_argv(path: str, start: float, duration: float) -> list[str]:
    return [
        settings.ffmpeg_path,
        "-hide_banner",
        "-ss",
        str(start),
        "-t",
        str(duration),
        "-i",
        path,
        "-vn",
        "-af",
        "silencedetect=noise=-30dB:d=0.25,ebur128=peak=true",
        "-f",
        "null",
        "-",
    ]


def _scene_argv(path: str, start: float, duration: float) -> list[str]:
    return [
        settings.ffmpeg_path,
        "-hide_banner",
        "-ss",
        str(start),
        "-t",
        str(duration),
        "-i",
        path,
        "-an",
        "-vf",
        "scdet=s=1:t=10",
        "-f",
        "null",
        "-",
    ]


def extract_cut_signals(
    path: str,
    window: tuple[float, float],
    *,
    runner: FfmpegRunner | None = None,
) -> tuple[CutSignals, float]:
    """Run ffmpeg (1-2 passes) over `[window_start, window_end)`.

    Returns `(signals, elapsed_ms)`. On any runner failure returns empty
    signals - optional enrichment, never fatal.
    """
    run = runner or default_runner
    start, end = window
    if end <= start:
        return CutSignals(), 0.0
    duration = end - start
    t0 = time.perf_counter()
    try:
        audio_text = run(_audio_argv(path, start, duration))
        scene_text = run(_scene_argv(path, start, duration))
    except Exception as exc:
        _log.warning("cut_signals: extract failed: %s", exc)
        return CutSignals(), (time.perf_counter() - t0) * 1000.0
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    audio = parse_ffmpeg_stderr(audio_text, time_offset=start)
    scene = parse_ffmpeg_stderr(scene_text, time_offset=start)
    return merge_signals(audio, scene), elapsed_ms


# Type alias for tests that want a simple callable.
RunnerFn = Callable[[list[str]], str]
