"""Profile `[camera]` block (AVE-69)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai_video_editor import profiles
from ai_video_editor.profiles import CameraDefaults, load_profile


@pytest.fixture(autouse=True)
def _fresh_registry():
    profiles._registry.cache_clear()
    yield
    profiles._registry.cache_clear()


def test_league_camera_above_minimap():
    cam = load_profile("league").camera
    assert cam == CameraDefaults(anchor="bottom_right", x=0.99, y=0.70, width_frac=0.18)


def test_default_camera():
    cam = load_profile(None).camera
    assert cam == CameraDefaults(anchor="top_left", x=0.02, y=0.06, width_frac=0.18)


def test_game_without_block_has_none():
    # valorant ships without a [camera] block; callers fall back to default
    assert load_profile("valorant").camera is None


def test_new_game_needs_only_a_toml(tmp_path, monkeypatch):
    (tmp_path / "bowling.toml").write_text(
        '[meta]\ngame = "bowling"\n[camera]\nanchor = "top_right"\n'
        "x = 0.97\ny = 0.03\nwidth_frac = 0.25\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(profiles, "PROFILES_DIR", tmp_path)
    profiles._registry.cache_clear()
    assert load_profile("bowling").camera == CameraDefaults(
        anchor="top_right", x=0.97, y=0.03, width_frac=0.25
    )


@pytest.mark.parametrize(
    "kw",
    [
        {"anchor": "middle", "x": 0.5, "y": 0.5, "width_frac": 0.2},
        {"anchor": "top_left", "x": 1.5, "y": 0.5, "width_frac": 0.2},
        {"anchor": "top_left", "x": 0.5, "y": -0.1, "width_frac": 0.2},
        {"anchor": "top_left", "x": 0.5, "y": 0.5, "width_frac": 0.05},
        {"anchor": "top_left", "x": 0.5, "y": 0.5, "width_frac": 0.6},
    ],
)
def test_camera_validation(kw):
    with pytest.raises(ValidationError):
        CameraDefaults(**kw)
