"""Calibrate the recording offset from the HUD's K/D/A counter.

The owner's K/D/A text (top right of the League HUD) changes the instant a
kill, death or assist lands. For each such Riot event we decode a short
window of that small crop, find the frame where the counter flips, and
compare with the event's game time. The median over several events is the
recording offset, good to about 0.1 s (the sample rate).

No OCR: we only detect *when* the near-white digits change, not what they say.
"""

from __future__ import annotations

import statistics
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from ..config import settings
from .riot_timeline import RiotTimeline, TimelineEvent

# x0, y0, x1, y1 as fractions of the frame (Zeri VOD, 1280x720: x 1085-1155, y 0-20).
KDA_BOX: tuple[float, float, float, float] = (0.845, 0.0, 0.905, 0.03)

OWNER_KINDS = frozenset({"kill", "death", "assist"})
BINARIZE_ABOVE = 170
MIN_STEP_PIXELS = 8
STEP_VS_MEDIAN = 3.0
CLOSE_EVENT_S = 3.0
MAX_SPREAD_S = 0.5
MIN_SAMPLES = 3


@dataclass
class HudSync:
    offset_s: float
    n: int
    spread_s: float
    samples: list[tuple[float, float]]  # (t_game_s, measured offset)


def _crop_px(width: int, height: int) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = KDA_BOX
    cx, cy = int(width * x0), int(height * y0)
    return int(width * (x1 - x0)), int(height * (y1 - y0)), cx, cy


def kda_frames(
    video_path: str, start_s: float, dur_s: float, *, fps: float = 10.0, run=subprocess.run
) -> np.ndarray:
    """Grayscale crops of the K/D/A box, shape (n, h, w) uint8; empty on failure."""
    probe = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=s=x:p=0",
            video_path,
        ],
        capture_output=True,
        text=True,
    )
    try:
        width, height = (int(v) for v in probe.stdout.strip().split("x")[:2])
    except ValueError:
        return np.zeros((0, 0, 0), np.uint8)
    w, h, cx, cy = _crop_px(width, height)
    proc = run(
        [
            settings.ffmpeg_path,
            "-v",
            "error",
            "-ss",
            str(start_s),
            "-t",
            str(dur_s),
            "-i",
            video_path,
            "-vf",
            f"fps={fps},crop={w}:{h}:{cx}:{cy},format=gray",
            "-f",
            "rawvideo",
            "pipe:",
        ],
        capture_output=True,
    )
    data = proc.stdout or b""
    n = len(data) // (w * h)
    if proc.returncode != 0 or n == 0:
        return np.zeros((0, h, w), np.uint8)
    return np.frombuffer(data[: n * w * h], np.uint8).reshape(n, h, w)


def change_time(frames: np.ndarray, start_s: float, fps: float) -> float | None:
    """Time of the first frame showing the new counter value, or None when no
    single step clearly dominates (>= 3x the median step and >= 8 pixels)."""
    if frames.shape[0] < 2:
        return None
    binary = frames > BINARIZE_ABOVE
    steps = (binary[1:] != binary[:-1]).reshape(frames.shape[0] - 1, -1).sum(axis=1)
    i = int(np.argmax(steps))
    peak = int(steps[i])
    if peak < MIN_STEP_PIXELS or peak < STEP_VS_MEDIAN * float(np.median(steps)):
        return None
    return start_s + (i + 1) / fps


def _isolated_owner_events(timeline: RiotTimeline) -> list[TimelineEvent]:
    owner = sorted((e for e in timeline.events if e.kind in OWNER_KINDS), key=lambda e: e.t_game_s)
    out = []
    for i, e in enumerate(owner):
        near_prev = i > 0 and e.t_game_s - owner[i - 1].t_game_s < CLOSE_EVENT_S
        near_next = i + 1 < len(owner) and owner[i + 1].t_game_s - e.t_game_s < CLOSE_EVENT_S
        if not (near_prev or near_next):
            out.append(e)
    return out


def _spread_pick(items: list, k: int) -> list:
    if len(items) <= k:
        return items
    idx = np.linspace(0, len(items) - 1, k).round().astype(int)
    return [items[i] for i in dict.fromkeys(idx.tolist())]


def calibrate_from_hud(
    video_path: str,
    timeline: RiotTimeline,
    *,
    window_s: float = 8.0,
    max_events: int = 12,
    fps: float = 10.0,
    reader: Callable = kda_frames,
) -> HudSync | None:
    samples: list[tuple[float, float]] = []
    for ev in _spread_pick(_isolated_owner_events(timeline), max_events):
        start = max(0.0, ev.t_game_s + timeline.offset_s - window_s)
        frames = reader(video_path, start, 2 * window_s, fps=fps)
        t = change_time(frames, start, fps)
        if t is not None:
            samples.append((ev.t_game_s, t - ev.t_game_s))
    if len(samples) < MIN_SAMPLES:
        return None
    offsets = [m for _, m in samples]
    med = statistics.median(offsets)
    spread = statistics.median(abs(m - med) for m in offsets)
    if spread > MAX_SPREAD_S:
        return None
    return HudSync(offset_s=med, n=len(samples), spread_s=spread, samples=samples)
