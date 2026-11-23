"""Generated sound effects the owner may replace with their own file."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)

_KACHING_EXPR = (
    "aevalsrc=0.6*sin(2*PI*1760*t)*exp(-8*t)+0.4*sin(2*PI*2637*t)*exp(-6*t):s=48000:d=0.35"
)


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
