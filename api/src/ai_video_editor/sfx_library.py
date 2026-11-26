"""Generated sound effects the owner may replace with their own file."""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

import numpy as np

from .config import settings

logger = logging.getLogger(__name__)

_KACHING_EXPR = (
    "aevalsrc=0.6*sin(2*PI*1760*t)*exp(-8*t)+0.4*sin(2*PI*2637*t)*exp(-6*t):s=48000:d=0.35"
)

# Glass-break-ish hit, 0.5 s, full level at t=0: a white-noise burst high-passed
# at ~3 kHz with a fast decay, plus three short high pings.
_DEATH_EXPR = (
    "aevalsrc=0.18*sin(2*PI*2800*t)*exp(-22*t)+0.14*sin(2*PI*4100*t)*exp(-26*t)"
    "+0.1*sin(2*PI*5300*t)*exp(-32*t):s=48000:d=0.5[p];"
    "anoisesrc=c=white:r=48000:d=0.5:a=0.9,highpass=f=3000,"
    "volume='exp(-24*t)':eval=frame[n];"
    "[p][n]amix=inputs=2:duration=longest:normalize=0"
)

_ONSET_DB = -30.0


def kaching_cmd(out: Path) -> list[str]:
    """ffmpeg argv that synthesises the 0.35 s two-tone chime into `out`."""
    return [
        settings.ffmpeg_path,
        "-y",
        "-f",
        "lavfi",
        "-i",
        _KACHING_EXPR,
        "-ac",
        "2",
        str(out),
    ]


def kaching_path(*, run: Callable = subprocess.run) -> Path:
    """`<workspace>/sfx/kaching.wav`, synthesised once when absent. Returned
    even if synthesis failed so callers degrade gracefully."""
    out = settings.workspace_dir / "sfx" / "kaching.wav"
    if out.is_file():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        res = run(kaching_cmd(out), capture_output=True, text=True, timeout=30, check=False)
        if getattr(res, "returncode", 0) != 0:
            logger.warning("kaching synthesis failed: %s", getattr(res, "stderr", ""))
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("kaching synthesis failed: %s", e)
    return out


def death_cmd(out: Path) -> list[str]:
    """ffmpeg argv that synthesises the 0.5 s glass-break death hit into `out`."""
    return [
        settings.ffmpeg_path,
        "-y",
        "-f",
        "lavfi",
        "-i",
        _DEATH_EXPR,
        "-t",
        "0.5",
        "-ac",
        "2",
        str(out),
    ]


def death_path(*, run: Callable = subprocess.run) -> Path:
    """`<workspace>/sfx/death.wav`, synthesised once when absent. An existing
    file is the owner's replacement and is never overwritten."""
    out = settings.workspace_dir / "sfx" / "death.wav"
    if out.is_file():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        res = run(death_cmd(out), capture_output=True, text=True, timeout=30, check=False)
        if getattr(res, "returncode", 0) != 0:
            logger.warning("death sfx synthesis failed: %s", getattr(res, "stderr", ""))
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("death sfx synthesis failed: %s", e)
    return out


_onset_cache: dict[tuple[str, float], float] = {}


def sfx_hit_s(path: Path, *, run: Callable = subprocess.run) -> float:
    """Seconds into the file where the sound should land on its moment.
    A sidecar `<stem>.json` with `hit_s` wins (e.g. a "cha-ching" whose bell,
    not the lead-in click, belongs on the kill); otherwise the leading silence
    (`sfx_onset_s`)."""
    side = Path(path).with_suffix(".json")
    try:
        hit = json.loads(side.read_text(encoding="utf-8")).get("hit_s")
        if isinstance(hit, int | float) and hit >= 0:
            return float(hit)
    except (OSError, ValueError, AttributeError):
        pass
    return sfx_onset_s(path, run=run)


def sfx_onset_s(path: Path, *, run: Callable = subprocess.run) -> float:
    """Seconds of leading silence (first sample above -30 dBFS); 0.0 on any error.
    Cached per (path, mtime)."""
    try:
        key = (str(path), Path(path).stat().st_mtime)
    except OSError:
        return 0.0
    if key in _onset_cache:
        return _onset_cache[key]
    onset = 0.0
    try:
        res = run(
            [
                settings.ffmpeg_path,
                "-v",
                "error",
                "-i",
                str(path),
                "-f",
                "s16le",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-",
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if getattr(res, "returncode", 1) == 0 and res.stdout:
            pcm = np.frombuffer(res.stdout[: len(res.stdout) // 2 * 2], dtype="<i2")
            loud = np.flatnonzero(np.abs(pcm.astype(np.int32)) > 32768 * 10 ** (_ONSET_DB / 20))
            onset = float(loud[0]) / 48000 if loud.size else 0.0
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        logger.warning("sfx onset probe failed for %s: %s", path, e)
    _onset_cache[key] = onset
    return onset
