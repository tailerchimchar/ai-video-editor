"""Camera box layout math (AVE-69)."""

from __future__ import annotations

import pytest

from ai_video_editor.narration.layout import camera_box
from ai_video_editor.narration.models import CameraBox

W, H = 1920, 1080


def test_league_default_bottom_right_above_minimap():
    b = camera_box(CameraBox(), "league", W, H)
    assert b.x + b.w <= round(0.99 * W) + 1
    assert b.x + b.w > W * 0.9  # hugs the right edge
    assert b.y + b.h <= 0.70 * H  # sits above the minimap
    assert b.y + b.h > 0.60 * H


def test_default_game_top_left():
    b = camera_box(CameraBox(), "default", W, H)
    assert (b.x, b.y) == (38, 64)  # 0.02*1920=38.4, 0.06*1080=64.8 -> even floor


def test_game_without_block_falls_back_to_default():
    assert camera_box(CameraBox(), "valorant", W, H) == camera_box(CameraBox(), "default", W, H)


def test_override_wins_and_keeps_aspect():
    b = camera_box(CameraBox(x=0.5, y=0.25, width_frac=0.25), "league", W, H)
    assert (b.x, b.y) == (960, 270)
    assert b.w == 480
    assert b.h == 270  # 16:9


def test_resize_keeps_aspect():
    small = camera_box(CameraBox(x=0.1, y=0.1, width_frac=0.1), "league", W, H)
    big = camera_box(CameraBox(x=0.1, y=0.1, width_frac=0.4), "league", W, H)
    assert big.w > small.w
    assert abs(big.w / big.h - 16 / 9) < 0.05
    assert abs(small.w / small.h - 16 / 9) < 0.05


def test_custom_aspect():
    b = camera_box(CameraBox(x=0.1, y=0.1, width_frac=0.25), "league", W, H, cam_aspect=4 / 3)
    assert abs(b.w / b.h - 4 / 3) < 0.02


def test_clamps_inside_frame():
    b = camera_box(CameraBox(x=1.0, y=1.0, width_frac=0.5), "league", W, H)
    assert b.x >= 0 and b.y >= 0
    assert b.x + b.w <= W
    assert b.y + b.h <= H


@pytest.mark.parametrize("game", ["league", "default", "nope"])
@pytest.mark.parametrize("cam", [CameraBox(), CameraBox(x=0.333, y=0.777, width_frac=0.137)])
@pytest.mark.parametrize("size", [(1920, 1080), (1280, 720), (2560, 1440)])
def test_even_pixels(game, cam, size):
    b = camera_box(cam, game, *size)
    assert all(v % 2 == 0 for v in (b.x, b.y, b.w, b.h))


def test_partial_override_mixes_with_default():
    # only width overridden: position still comes from the profile anchor
    b = camera_box(CameraBox(width_frac=0.3), "league", W, H)
    assert b.w == 576
    assert b.y + b.h <= 0.70 * H
    assert b.x + b.w > W * 0.9
