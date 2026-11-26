"""Camera box placement: owner overrides + the game profile's `[camera]` default.

Pure math (the only I/O is the cached profile lookup); no ffmpeg, no files.
"""

from __future__ import annotations

from ..profiles import CameraDefaults, load_profile
from .models import CameraBox, PixelBox

_LAST_RESORT = CameraDefaults(anchor="top_left", x=0.02, y=0.06, width_frac=0.18)


def _defaults_for(game: str) -> CameraDefaults:
    """Game's `[camera]` block, else the `default` profile's, else a constant."""
    return load_profile(game).camera or load_profile(None).camera or _LAST_RESORT


def _even(value: float) -> int:
    """Floor to an even pixel count (video encoders want even dimensions)."""
    return int(value // 2) * 2


def _anchored_origin(
    d: CameraDefaults, frame_w: int, frame_h: int, w: int, h: int
) -> tuple[float, float]:
    """Top-left of a w*h box whose `d.anchor` corner sits at (d.x, d.y)."""
    left = d.x * frame_w - (w if d.anchor.endswith("right") else 0)
    top = d.y * frame_h - (h if d.anchor.startswith("bottom") else 0)
    return left, top


def camera_box(
    camera: CameraBox,
    game: str,
    frame_w: int,
    frame_h: int,
    cam_aspect: float = 16 / 9,
) -> PixelBox:
    """Resolve the camera box to pixels.

    Owner fields (`x`, `y` top-left fractions; `width_frac`) win per field;
    anything left None comes from the game profile default. The box keeps
    `cam_aspect`, is clamped inside the frame and rounded to even pixels.
    """
    defaults = _defaults_for(game)
    width_frac = camera.width_frac if camera.width_frac is not None else defaults.width_frac
    w = max(2, _even(width_frac * frame_w))
    h = max(2, _even(w / cam_aspect))
    w, h = min(w, _even(frame_w)), min(h, _even(frame_h))

    left, top = _anchored_origin(defaults, frame_w, frame_h, w, h)
    if camera.x is not None:
        left = camera.x * frame_w
    if camera.y is not None:
        top = camera.y * frame_h

    left = min(max(left, 0), frame_w - w)
    top = min(max(top, 0), frame_h - h)
    return PixelBox(x=_even(left), y=_even(top), w=w, h=h)
