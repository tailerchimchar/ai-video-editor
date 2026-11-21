"""Beat-tied effects tests. Pure — asserts on filter fragment shape."""

from __future__ import annotations

import pytest

from ai_video_editor.music.effects import build_beat_effect_filter


def test_unknown_effect_raises() -> None:
    with pytest.raises(ValueError, match="unknown beat effect"):
        build_beat_effect_filter("cinema", "in", "out", ())


def test_flash_with_no_beats_is_passthrough() -> None:
    """Zero beats → null passthrough (nothing to flash on)."""
    out = build_beat_effect_filter("flash", "in", "out", ())
    assert "null" in out
    assert "eq=" not in out


def test_flash_produces_enable_expr_per_beat() -> None:
    """Each beat should show up in the `between(t,B,B+D)` expression."""
    beats = (1.0, 2.5, 4.0)
    out = build_beat_effect_filter("flash", "in", "out", beats, intensity=0.4, duration_seconds=0.1)
    for beat in beats:
        assert f"{beat:.4f}" in out
    assert "eq=brightness=0.400" in out
    assert "enable=" in out


def test_flash_zero_intensity_is_passthrough() -> None:
    """Intensity 0 = no visible effect → return null passthrough."""
    out = build_beat_effect_filter("flash", "in", "out", (1.0, 2.0), intensity=0.0)
    assert "null" in out
    assert "eq=" not in out


def test_flash_intensity_clamped_to_1() -> None:
    """Intensity > 1 should clamp — no NaN / weird ffmpeg errors."""
    out = build_beat_effect_filter("flash", "in", "out", (1.0,), intensity=5.0)
    assert "brightness=1.000" in out


def test_flash_writes_to_out_label() -> None:
    out = build_beat_effect_filter("flash", "vin", "vout", (1.0,))
    assert "[vin]" in out
    assert "[vout]" in out


def test_pulse_zoom_registered_but_currently_passthrough() -> None:
    """pulse_zoom is registered (open/closed proof) — MVP is passthrough."""
    out = build_beat_effect_filter("pulse_zoom", "in", "out", (1.0, 2.0))
    assert "[in]" in out and "[out]" in out
    assert "null" in out
