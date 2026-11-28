"""Cut each camera take into the windows the export overlays: the take span
is split at every camera-region edge inside it, each piece is shown with its
region's box (or the session default), or hidden when that box is disabled.
Pure: box placement is injected (`box_for`)."""

from __future__ import annotations

from collections.abc import Callable
from itertools import pairwise
from typing import NamedTuple

from .mix import TakeInput
from .models import CameraBox, CameraRegion, PixelBox

BoxFor = Callable[[CameraBox, str, int, int], PixelBox]

# Pieces shorter than this are visual noise, not a camera window.
MIN_WINDOW_S = 0.05


class CameraWindow(NamedTuple):
    input_index: int  # ffmpeg input of the take (video is input 0)
    take: TakeInput
    start_s: float  # timeline seconds
    end_s: float
    box: PixelBox


def windows_for(
    takes: list[tuple[int, TakeInput]],
    regions: list[CameraRegion],
    default: CameraBox,
    game: str,
    frame: tuple[int, int],
    box_for: BoxFor,
) -> list[CameraWindow]:
    w, h = frame
    windows: list[CameraWindow] = []
    for k, take in takes:
        a, b = take.start_s, take.start_s + take.duration_s
        cuts = sorted({a, b, *(e for r in regions for e in (r.start_s, r.end_s) if a < e < b)})
        for lo, hi in pairwise(cuts):
            if hi - lo < MIN_WINDOW_S:
                continue
            region = next((r for r in regions if r.start_s <= lo and hi <= r.end_s), None)
            camera = region.box if region else default
            if camera.enabled:
                windows.append(CameraWindow(k, take, lo, hi, box_for(camera, game, w, h)))
    return windows
