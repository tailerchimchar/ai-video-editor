"""Frame strip (one JPEG sprite) and game-audio peaks for the narration timeline.

Both are computed once per video fingerprint and cached. The planning and
argv functions are pure; the `ensure_*` helpers take the process runners as
arguments so tests never spawn ffmpeg.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..config import settings
from .models import Peaks

FRAME_TILE_W = 160
MAX_TILES = 240
PEAKS_RATE_HZ = 10
PEAKS_SAMPLE_RATE = 8000
LOCK_WAIT_S = 60.0
_POLL_S = 0.2

Run = Callable[[list[str]], tuple[bool, str | None]]
RunCapture = Callable[[list[str]], bytes]
ProbeSize = Callable[[Path], tuple[int, int]]


@dataclass
class FrameStripFiles:
    sprite: Path
    meta: Path
    every_s: float
    tile_w: int
    tile_h: int
    cols: int
    count: int


# -- pure ---------------------------------------------------------------
def strip_plan(duration_s: float) -> tuple[float, int]:
    """(seconds between tiles, tile count) keeping the strip under MAX_TILES."""
    every_s = float(max(1, math.ceil(duration_s / MAX_TILES)))
    return every_s, math.ceil(duration_s / every_s)


def sprite_cmd(video: Path, out: Path, every_s: float, count: int) -> list[str]:
    return [
        settings.ffmpeg_path,
        "-y",
        "-v",
        "error",
        "-i",
        video.as_posix(),
        "-vf",
        f"fps=1/{every_s},scale={FRAME_TILE_W}:-2,tile={count}x1",
        "-frames:v",
        "1",
        "-q:v",
        "4",
        out.as_posix(),
    ]


def peaks_cmd(video: Path) -> list[str]:
    return [
        settings.ffmpeg_path,
        "-v",
        "error",
        "-i",
        video.as_posix(),
        "-vn",
        "-f",
        "s16le",
        "-ac",
        "1",
        "-ar",
        str(PEAKS_SAMPLE_RATE),
        "pipe:1",
    ]


def peaks_from_pcm(
    raw: bytes, rate_hz: int = PEAKS_RATE_HZ, sample_rate: int = PEAKS_SAMPLE_RATE
) -> list[float]:
    """Max-abs per window, normalised to the loudest window, 3 decimals."""
    samples = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2")
    window = sample_rate // rate_hz
    n = math.ceil(len(samples) / window)
    if n == 0:
        return []
    mags = np.abs(samples.astype(np.int32))
    maxes = np.array([mags[i * window : (i + 1) * window].max() for i in range(n)], dtype=float)
    top = maxes.max()
    if top == 0:
        return [0.0] * n
    return [round(float(v), 3) for v in maxes / top]


# -- process runners (real defaults) ------------------------------------
def run_capture(cmd: list[str]) -> bytes:
    return subprocess.run(cmd, capture_output=True, check=True).stdout


def probe_duration(video: Path) -> float:
    r = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            video.as_posix(),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(r.stdout.strip())


# -- idempotent, locked rendering ----------------------------------------
def _locked_render(out: Path, cache_dir: Path, fp: str, render: Callable[[Path], None]) -> None:
    """Run `render(part_path)` once per `out`; concurrent callers wait for it."""
    if out.is_file():
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock = cache_dir / f".lock-{fp}"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        deadline = time.monotonic() + LOCK_WAIT_S
        while time.monotonic() < deadline:
            if out.is_file():
                return
            time.sleep(_POLL_S)
        if out.is_file():
            return
        raise RuntimeError(f"timed out waiting for {out.name}") from None
    os.close(fd)
    part = out.with_name(out.name + ".part")
    try:
        if out.is_file():  # finished by another process between check and lock
            return
        render(part)
        os.replace(part, out)
    finally:
        part.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


def ensure_frames(
    video: Path,
    cache_dir: Path,
    fp: str,
    *,
    duration_s: float,
    run: Run,
    probe_size: ProbeSize,
) -> FrameStripFiles:
    sprite = cache_dir / f"frames-{fp}.jpg"
    meta = cache_dir / f"frames-{fp}.json"
    every_s, count = strip_plan(duration_s)

    def render(part: Path) -> None:
        tmp = part.with_name(part.stem + ".part.jpg")  # keep a real image extension
        try:
            ok, err = run(sprite_cmd(video, tmp, every_s, count))
            if not ok:
                raise RuntimeError(f"frame strip failed: {err}")
            tile_h = probe_size(tmp)[1]
            os.replace(tmp, sprite)
        finally:
            tmp.unlink(missing_ok=True)
        part.write_text(
            json.dumps(
                {
                    "every_s": every_s,
                    "tile_w": FRAME_TILE_W,
                    "tile_h": tile_h,
                    "cols": count,
                    "count": count,
                }
            ),
            encoding="utf-8",
        )

    # `meta` is written last, so its existence means sprite + meta are complete.
    _locked_render(meta, cache_dir, fp, render)
    data = json.loads(meta.read_text(encoding="utf-8"))
    return FrameStripFiles(sprite=sprite, meta=meta, **data)


def ensure_peaks(video: Path, cache_dir: Path, fp: str, *, run_capture: RunCapture) -> Peaks:
    out = cache_dir / f"peaks-{fp}.json"

    def render(part: Path) -> None:
        peaks = peaks_from_pcm(run_capture(peaks_cmd(video)))
        part.write_text(
            Peaks(rate_hz=PEAKS_RATE_HZ, peaks=peaks).model_dump_json(), encoding="utf-8"
        )

    _locked_render(out, cache_dir, fp, render)
    return Peaks.model_validate_json(out.read_text(encoding="utf-8"))
