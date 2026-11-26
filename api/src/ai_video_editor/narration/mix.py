"""Pure ffmpeg argv for a narration export. Contract for AVE-68 Task B."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .models import AudioMix, PixelBox


@dataclass(frozen=True)
class TakeInput:
    path: Path
    start_s: float
    duration_s: float
    has_camera: bool


def export_cmd(
    video: Path,
    takes: list[TakeInput],
    out: Path,
    *,
    mix: AudioMix,
    camera_box: PixelBox | None,
) -> list[str]:
    """argv mixing `takes` over `video` (ducked original audio; camera
    overlay at `camera_box` for takes with camera when the box is given;
    video stream copied when nothing is overlaid)."""
    raise NotImplementedError  # Task B
