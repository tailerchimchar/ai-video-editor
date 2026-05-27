"""Reusable ffmpeg editing primitives.

One place that knows how to invoke ffmpeg, so both the single-clip
endpoint and the highlights builder share identical, safe behavior.
Commands are built from validated inputs only — no shell, no raw args.
Source files are read-only; all writes go to caller-chosen paths under
WORKSPACE_DIR.
"""

import subprocess
from pathlib import Path

from .config import settings


def trim_clip(
    asset_path: str, output_path: str, start_seconds: float, end_seconds: float
) -> tuple[bool, str | None]:
    """Cut [start, end) from `asset_path` into `output_path` (stream copy).

    Returns (ok, error). Never raises for ffmpeg/`not found` failures —
    callers surface the error via the job row.
    """
    duration = end_seconds - start_seconds
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        settings.ffmpeg_path,
        "-y",
        "-ss",
        str(start_seconds),
        "-i",
        asset_path,
        "-t",
        str(duration),
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return False, "ffmpeg not found — install it: winget install Gyan.FFmpeg"
    if result.returncode != 0:
        return False, result.stderr[:2000]
    return True, None
