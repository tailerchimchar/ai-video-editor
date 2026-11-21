"""Zoom post-crop upscale: pure filter choice + ffmpeg height check.

Factor != 2.0 with the legacy `scale=iw*2:ih*2` leaves non-1080p frames
(e.g. 1.5x → 2560x1440), so captions burned at a fixed fontsize look tiny.
The opt-in flag scales to height 1080 instead; 9:16 skips the upscale
because aspect_filter already targets 720x1280.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ai_video_editor.config import settings
from ai_video_editor.edits import apply_zoom, zoom_upscale_filter

# ----- pure unit tests -----


def test_zoom_upscale_flag_off_keeps_legacy_2x():
    assert zoom_upscale_filter(scale_to_1080p=False, aspect="16:9") == "scale=iw*2:ih*2"
    assert zoom_upscale_filter(scale_to_1080p=False, aspect="9:16") == "scale=iw*2:ih*2"


def test_zoom_upscale_flag_on_gives_1080_lanczos():
    assert zoom_upscale_filter(scale_to_1080p=True, aspect="16:9") == "scale=-2:1080:flags=lanczos"


def test_zoom_upscale_flag_on_9x16_skips_upscale():
    assert zoom_upscale_filter(scale_to_1080p=True, aspect="9:16") == ""


# ----- integration: real ffmpeg + ffprobe -----


def _make_1080p_clip(path: Path, seconds: float = 0.5) -> None:
    """Tiny synthetic 1920x1080 lavfi clip (placeholder footage, no real play)."""
    cmd = [
        settings.ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s=1920x1080:r=10:d={seconds}",
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r=8000:cl=mono:d={seconds}",
        "-shortest",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _probe_height(path: Path) -> int:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=height",
        "-of",
        "json",
        str(path),
    ]
    raw = subprocess.run(cmd, check=True, capture_output=True, text=True)
    data = json.loads(raw.stdout)
    return int(data["streams"][0]["height"])


@pytest.mark.parametrize(
    "scale_to_1080p,expected_height",
    [
        (False, 1440),  # crop iw/1.5 then legacy 2x → 2560x1440
        (True, 1080),  # crop then scale=-2:1080
    ],
)
def test_apply_zoom_factor_1_5_output_height(tmp_path, scale_to_1080p, expected_height):
    src = tmp_path / "src.mp4"
    out = tmp_path / "zoomed.mp4"
    _make_1080p_clip(src)

    ok, err = apply_zoom(
        str(src),
        str(out),
        start=0.0,
        end=0.4,
        factor=1.5,
        roi="center",
        aspect="16:9",
        scale_to_1080p=scale_to_1080p,
    )
    assert ok is True, err
    assert out.is_file()
    assert _probe_height(out) == expected_height
