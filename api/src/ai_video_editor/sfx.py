"""SFX extraction (Sprint #2).

One I/O primitive: cut an audio span out of an asset into a clean wav
under `WORKSPACE/media_library/<game>/sfx/`. Used to source per-game
announcer/cue templates that sprint #3's `add_sfx` overlays and
sprint #4's `audio_event` detector match against.

The ffmpeg invocation normalizes to mono 44.1 kHz PCM so all templates
share a shape — important for sprint #4's mel-spectrogram matching.
"""

import subprocess
from pathlib import Path

from .config import settings


def extract_sfx(
    asset_path: str,
    output_path: str,
    start_seconds: float,
    end_seconds: float,
) -> tuple[bool, str | None]:
    """Cut [start, end) of `asset_path`'s audio into a clean mono wav.

    Returns (ok, error). Never raises for ffmpeg / `not found`; callers
    surface errors via the job row. Output format is mono 44.1 kHz PCM
    s16 so all extracted templates share a shape.
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
        "-vn",  # drop video
        "-ac",
        "1",  # mono
        "-ar",
        "44100",  # 44.1 kHz
        "-acodec",
        "pcm_s16le",
        output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return False, "ffmpeg not found — install it: winget install Gyan.FFmpeg"
    if result.returncode != 0:
        # ffmpeg banner is at the head; the actual error sits at the tail.
        return False, result.stderr[-1500:]
    return True, None
