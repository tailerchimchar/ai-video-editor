"""Read the owner's HP (green) and mana (blue) bars off the League HUD.

The bars sit at the bottom centre of the HUD, at the same place relative to
the frame at 1280x720 and 1920x1080 (the HUD scales with the frame).
Calibrated 2026-11-26 on Yunara (720p, 867/1090 HP at recording 400 s ->
0.80), Jhin (1080p, 665/870 HP and 244/300 mana at 30 s -> 0.76 / 0.81;
dead at 400 s -> unreadable) and Zeri (720p, full on both at 400 s).

The fill is the rightmost column, in the top rows of a bar, whose pixels are
clearly green (HP) or blue (mana); white numbers over the middle and the dark
regen area ("+1.2") right of the fill are neither. A dead champion's bars are
dark and empty: that reads as None (unreadable), never as a drop.

`sample_bars` does one ffmpeg seek per time and crops only the bar strip;
`read_bars` and `drops` are pure.
"""

from __future__ import annotations

import itertools
import logging
import math
import subprocess
from collections.abc import Callable, Sequence
from typing import Literal

import numpy as np
from pydantic import BaseModel

from ..config import settings

_log = logging.getLogger(__name__)

# x0, y0, x1, y1 as fractions of the frame. 720p: x 496-710, HP rows 694-701,
# mana rows 704-711. 1080p: x 744-1066, HP rows 1041-1052, mana 1056-1067.
HP_BOX: tuple[float, float, float, float] = (0.3875, 0.964, 0.5555, 0.975)
MANA_BOX: tuple[float, float, float, float] = (0.3875, 0.978, 0.5555, 0.989)

# Fill pixels seen: HP green (0-15, 64-190, 0-17); mana blue (0-61, 59-153, 119-255).
# The dark empty part is about (2, 12, 8); white text is about (240, 240, 240).
COLOUR_MARGIN = 40  # the bar colour channel beats both others by this much
GREEN_MIN = 50
BLUE_MIN = 90
LEFT_EDGE_FRAC = 0.03  # a real bar's fill always starts at the left edge
LEFT_EDGE_FILLED = 0.8
MIN_DENSITY = 0.6  # columns left of the fill edge that must read as filled
SEEK_TIMEOUT_S = 60.0


class BarSample(BaseModel):
    t_rec_s: float  # recording seconds
    hp: float | None = None  # fill 0..1; None = unreadable (dead, no HUD, no frame)
    mana: float | None = None


def strip_box() -> tuple[float, float, float, float]:
    """The one crop that holds both bars."""
    return (HP_BOX[0], HP_BOX[1], MANA_BOX[2], MANA_BOX[3])


def _rows(strip_h: int, box: tuple[float, float, float, float]) -> tuple[int, int]:
    s = strip_box()
    span = s[3] - s[1]
    return round(strip_h * (box[1] - s[1]) / span), round(strip_h * (box[3] - s[1]) / span)


def _fill(bar: np.ndarray, channel: int, minimum: int) -> float | None:
    """Fill fraction of one bar (rows x cols x RGB), or None when it is not a bar."""
    h, w = bar.shape[:2]
    if h == 0 or w < 10:
        return None
    top = bar[: max(1, math.ceil(h / 2))].astype(np.int16)
    main = top[..., channel]
    others = np.delete(top, channel, axis=2).max(axis=2)
    coloured = (main - others >= COLOUR_MARGIN) & (main >= minimum)
    cols = coloured.sum(axis=0) >= max(1, math.ceil(top.shape[0] / 2))
    if not cols.any():
        return None
    edge = max(2, round(w * LEFT_EDGE_FRAC))
    if cols[:edge].mean() < LEFT_EDGE_FILLED:
        return None
    right = int(np.flatnonzero(cols)[-1])
    if cols[: right + 1].mean() < MIN_DENSITY:
        return None
    return round((right + 1) / w, 4)


def read_bars(strip: np.ndarray | None) -> tuple[float | None, float | None]:
    """(hp, mana) fill fractions from an RGB crop of `strip_box()`."""
    if strip is None or strip.ndim != 3 or strip.shape[0] < 4 or strip.shape[2] != 3:
        return None, None
    h = strip.shape[0]
    hp0, hp1 = _rows(h, HP_BOX)
    mp0, mp1 = _rows(h, MANA_BOX)
    return _fill(strip[hp0:hp1], 1, GREEN_MIN), _fill(strip[mp0:mp1], 2, BLUE_MIN)


def _crop_px(width: int, height: int) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = strip_box()
    cx, cy = round(width * x0), round(height * y0)
    return round(width * x1) - cx, round(height * y1) - cy, cx, cy


def probe_size(video_path: str, *, run=subprocess.run) -> tuple[int, int] | None:
    try:
        proc = run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", video_path],
            capture_output=True, text=True, timeout=SEEK_TIMEOUT_S,
        )  # fmt: skip
        width, height = (int(v) for v in proc.stdout.strip().split("x")[:2])
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return width, height


def strip_frame(
    video_path: str, t_rec_s: float, size: tuple[int, int], *, run=subprocess.run
) -> np.ndarray | None:
    """RGB crop of the bar strip at `t_rec_s` (one fast seek), None on failure."""
    w, h, cx, cy = _crop_px(*size)
    try:
        proc = run(
            [settings.ffmpeg_path, "-v", "error", "-ss", f"{max(0.0, t_rec_s):.3f}",
             "-i", video_path, "-frames:v", "1",
             "-vf", f"format=rgb24,crop={w}:{h}:{cx}:{cy}", "-f", "rawvideo", "pipe:"],
            capture_output=True, timeout=SEEK_TIMEOUT_S,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    data = proc.stdout or b""
    if proc.returncode != 0 or len(data) < w * h * 3:
        return None
    return np.frombuffer(data[: w * h * 3], np.uint8).reshape(h, w, 3)


def sample_bars(
    video_path: str,
    times_rec_s: Sequence[float],
    *,
    reader: Callable[[str, float, tuple[int, int]], np.ndarray | None] = strip_frame,
    probe: Callable[[str], tuple[int, int] | None] = probe_size,
) -> list[BarSample]:
    """One `BarSample` per time (recording seconds); all None when the video
    cannot be probed."""
    size = probe(video_path) if times_rec_s else None
    out = []
    for t in times_rec_s:
        hp, mana = read_bars(reader(video_path, t, size)) if size else (None, None)
        out.append(BarSample(t_rec_s=t, hp=hp, mana=mana))
    if times_rec_s:
        readable = sum(s.hp is not None for s in out)
        _log.info("hud bars: %d/%d samples readable", readable, len(out))
    return out


def drops(
    samples: Sequence[BarSample], key: Literal["hp", "mana"], min_drop: float
) -> list[tuple[float, float]]:
    """(t_rec_s, drop) for each consecutive readable pair whose value fell by
    at least `min_drop`; the time is the later sample's. A rise (recall,
    heal, level up) is never a drop; unreadable samples are skipped."""
    pts = [(s.t_rec_s, v) for s in samples if (v := getattr(s, key)) is not None]
    return [
        (t1, round(v0 - v1, 4))
        for (_, v0), (t1, v1) in itertools.pairwise(pts)
        if v0 - v1 >= min_drop - 1e-9
    ]
