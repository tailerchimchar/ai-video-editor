"""Pure tests for cutting camera takes into per-region windows."""

from __future__ import annotations

from pathlib import Path

from ai_video_editor.narration.camera_windows import CameraWindow, windows_for
from ai_video_editor.narration.mix import TakeInput
from ai_video_editor.narration.models import CameraBox, CameraRegion, PixelBox

DEFAULT_PX = PixelBox(x=1, y=1, w=10, h=10)
BIG_PX = PixelBox(x=2, y=2, w=99, h=99)
TAKE = TakeInput(Path("a.webm"), 10.0, 30.0, True)


def box_for(camera: CameraBox, game: str, w: int, h: int) -> PixelBox:
    return BIG_PX if camera.width_frac == 0.5 else DEFAULT_PX


def _region(rid: str, a: float, b: float, **box) -> CameraRegion:
    return CameraRegion(id=rid, start_s=a, end_s=b, box=CameraBox(**box))


def _spans(wins: list[CameraWindow]) -> list[tuple]:
    return [(w.input_index, w.start_s, w.end_s, w.box) for w in wins]


def test_windows_for_cuts_take_at_region_edges_and_hides():
    regions = [
        _region("big", 15, 20, enabled=True, width_frac=0.5),
        _region("hide", 25, 30, enabled=False),
    ]
    wins = windows_for(
        [(1, TAKE)], regions, CameraBox(enabled=True), "league", (1920, 1080), box_for
    )
    assert _spans(wins) == [
        (1, 10.0, 15.0, DEFAULT_PX),
        (1, 15.0, 20.0, BIG_PX),
        (1, 20.0, 25.0, DEFAULT_PX),
        (1, 30.0, 40.0, DEFAULT_PX),
    ]
    assert all(w.take is TAKE for w in wins)


def test_windows_for_default_disabled_only_regions_show():
    regions = [_region("on", 15, 20, enabled=True, width_frac=0.5)]
    wins = windows_for([(1, TAKE)], regions, CameraBox(enabled=False), "g", (1, 1), box_for)
    assert _spans(wins) == [(1, 15.0, 20.0, BIG_PX)]


def test_no_regions_one_window_per_take_and_none_when_disabled():
    t2 = TakeInput(Path("b.webm"), 50.0, 2.0, True)
    on = windows_for([(1, TAKE), (2, t2)], [], CameraBox(enabled=True), "g", (1, 1), box_for)
    assert _spans(on) == [(1, 10.0, 40.0, DEFAULT_PX), (2, 50.0, 52.0, DEFAULT_PX)]
    assert windows_for([(1, TAKE)], [], CameraBox(enabled=False), "g", (1, 1), box_for) == []


def test_region_overlapping_take_start_is_clipped_and_tiny_pieces_dropped():
    regions = [_region("r", 0, 10.02, enabled=True, width_frac=0.5)]
    wins = windows_for([(1, TAKE)], regions, CameraBox(enabled=True), "g", (1, 1), box_for)
    assert _spans(wins) == [(1, 10.02, 40.0, DEFAULT_PX)]
