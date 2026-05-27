"""ffprobe helpers — read media metadata without touching source files."""

import subprocess
from pathlib import Path

from ..config import settings


def _ffprobe_path() -> str:
    """Derive the ffprobe binary path from the configured ffmpeg path."""
    ffmpeg = settings.ffmpeg_path
    if ffmpeg in ("ffmpeg", "ffmpeg.exe"):
        return "ffprobe"
    p = Path(ffmpeg)
    sibling = p.with_name("ffprobe.exe" if p.suffix == ".exe" else "ffprobe")
    return str(sibling) if sibling.exists() else "ffprobe"


def get_duration_seconds(video_path: str) -> float:
    """Return the duration of a media file in seconds (0.0 if unknown)."""
    cmd = [
        _ffprobe_path(),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "csv=p=0",
        video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return 0.0
    try:
        return float(result.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0
