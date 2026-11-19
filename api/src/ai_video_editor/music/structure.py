"""Song structure — rising / climax / falling spans plus the drop.

Heuristic, deliberately simple (a manual override in the analysis sidecar
is the correction path, not a smarter detector):

1. Cut the song into 0.25s windows; per window take RMS loudness and the
   mean onset strength (how much new sound arrives).
2. Blend the two (normalised), then smooth with a ~2s moving average.
3. The climax is the contiguous run of windows around the global peak
   that stays above `_PLATEAU_FRACTION` of the peak.
4. Rising is everything before it, falling everything after, so the three
   spans tile [0, duration]. The drop is the strongest onset inside the
   climax.

`structure_from_signal` is pure (numpy in, dataclass out); `detect_structure`
is the thin file-decode shell around it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np

STRUCTURE_ANALYZER_VERSION = "v1"
"""Bump when the heuristic changes; auto sidecars re-detect, manual ones stay."""

_WINDOW_S = 0.25
_SMOOTH_WINDOWS = 8
_PLATEAU_FRACTION = 0.75
_RMS_WEIGHT = 0.7
_ONSET_HOP = 512

Span = tuple[float, float]


@dataclass(frozen=True)
class SongStructure:
    """Where a song builds, peaks and fades, in seconds from file start."""

    rising: Span
    climax: Span
    falling: Span
    drop_s: float
    source: Literal["auto", "manual"]
    analyzer_version: str


def _normalised(values: np.ndarray) -> np.ndarray:
    peak = float(values.max()) if values.size else 0.0
    return values / peak if peak > 0 else np.zeros_like(values)


def _window_features(y: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """Per-window (RMS, onset strength). Whole windows only."""
    import librosa

    win = int(sr * _WINDOW_S)
    count = y.size // win
    framed = y[: count * win].reshape(count, win)
    rms = np.sqrt(np.mean(framed**2, axis=1))

    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=_ONSET_HOP)
    window_of_frame = (np.arange(onset.size) * _ONSET_HOP) // win
    keep = window_of_frame < count
    totals = np.bincount(window_of_frame[keep], weights=onset[keep], minlength=count)
    frames = np.bincount(window_of_frame[keep], minlength=count)
    return rms, totals / np.maximum(frames, 1)


def _plateau(score: np.ndarray) -> tuple[int, int]:
    """Inclusive window range around the global peak that stays high."""
    peak = int(score.argmax())
    floor = score[peak] * _PLATEAU_FRACTION
    lo = hi = peak
    while lo > 0 and score[lo - 1] >= floor:
        lo -= 1
    while hi < score.size - 1 and score[hi + 1] >= floor:
        hi += 1
    return lo, hi


def structure_from_signal(y: np.ndarray, sr: int) -> SongStructure:
    """Detect structure from a mono signal. Silence/too-short -> all climax."""
    duration = y.size / sr
    rms, onset = (
        _window_features(y, sr)
        if y.size >= sr * _WINDOW_S
        else (
            np.zeros(0),
            np.zeros(0),
        )
    )
    score = _RMS_WEIGHT * _normalised(rms) + (1 - _RMS_WEIGHT) * _normalised(onset)
    if score.size == 0 or score.max() <= 0:
        return SongStructure(
            (0.0, 0.0),
            (0.0, duration),
            (duration, duration),
            0.0,
            "auto",
            STRUCTURE_ANALYZER_VERSION,
        )

    kernel = np.ones(_SMOOTH_WINDOWS) / _SMOOTH_WINDOWS
    smoothed = np.convolve(score, kernel, mode="same")
    lo, hi = _plateau(smoothed)

    climax_start = lo * _WINDOW_S
    climax_end = duration if hi == score.size - 1 else (hi + 1) * _WINDOW_S
    strongest = lo + int(onset[lo : hi + 1].argmax())
    return SongStructure(
        rising=(0.0, climax_start),
        climax=(climax_start, climax_end),
        falling=(climax_end, duration),
        drop_s=strongest * _WINDOW_S,
        source="auto",
        analyzer_version=STRUCTURE_ANALYZER_VERSION,
    )


def detect_structure(path: Path) -> SongStructure:
    """Decode `path` (any format librosa/ffmpeg reads) and detect its structure."""
    import librosa

    y, sr = librosa.load(str(path), sr=22050, mono=True)
    return structure_from_signal(y, int(sr))
