"""Flags default off; shadow keeps legacy; enabled uses the pick."""

from __future__ import annotations

from ai_video_editor.config import settings
from ai_video_editor.cut_picker import CutPick, CutRunStats
from ai_video_editor.highlights import _apply_jev_clip_cut, cut_window


def test_flags_default_off():
    assert settings.grokbot_jev_clip_cut_enabled is False
    assert settings.grokbot_jev_clip_cut_shadow is False


def test_apply_noop_when_flags_off(monkeypatch):
    monkeypatch.setattr(settings, "grokbot_jev_clip_cut_enabled", False)
    monkeypatch.setattr(settings, "grokbot_jev_clip_cut_shadow", False)
    stats = CutRunStats()
    s, e = _apply_jev_clip_cut(
        path="x.mp4",
        cand={"event_type": "kill", "metadata": {"anchor_seconds": 10.0}},
        legacy_start=7.0,
        legacy_end=18.0,
        duration=60.0,
        stats=stats,
    )
    assert (s, e) == (7.0, 18.0)
    assert stats.clips == 0


def test_shadow_keeps_legacy_and_logs(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(settings, "grokbot_jev_clip_cut_enabled", False)
    monkeypatch.setattr(settings, "grokbot_jev_clip_cut_shadow", True)

    fake = CutPick(
        start=8.0,
        end=19.0,
        flagged=False,
        criteria_passed=3,
        option_index=1,
        jev_ms=11.0,
        input_tokens=50,
        label="quiet_snap",
    )
    monkeypatch.setattr(
        "ai_video_editor.highlights.resolve_clip_cut",
        lambda *a, **k: (fake, 5.0, object()),
    )
    stats = CutRunStats()
    with caplog.at_level(logging.INFO):
        s, e = _apply_jev_clip_cut(
            path="x.mp4",
            cand={"event_type": "kill", "metadata": {"anchor_seconds": 10.0}},
            legacy_start=7.0,
            legacy_end=18.0,
            duration=60.0,
            stats=stats,
        )
    assert (s, e) == (7.0, 18.0)  # legacy kept
    assert stats.clips == 1
    assert any("jev_clip_cut shadow" in r.message for r in caplog.records)


def test_enabled_uses_pick(monkeypatch):
    monkeypatch.setattr(settings, "grokbot_jev_clip_cut_enabled", True)
    monkeypatch.setattr(settings, "grokbot_jev_clip_cut_shadow", False)
    fake = CutPick(
        start=8.5,
        end=19.5,
        flagged=False,
        criteria_passed=4,
        option_index=0,
        jev_ms=9.0,
        input_tokens=40,
        label="padding",
    )
    monkeypatch.setattr(
        "ai_video_editor.highlights.resolve_clip_cut",
        lambda *a, **k: (fake, 3.0, object()),
    )
    stats = CutRunStats()
    s, e = _apply_jev_clip_cut(
        path="x.mp4",
        cand={"event_type": "assist", "metadata": {"anchor_seconds": 12.0}},
        legacy_start=9.0,
        legacy_end=18.0,
        duration=60.0,
        stats=stats,
    )
    assert (s, e) == (8.5, 19.5)


def test_cut_window_unchanged_for_anchor(monkeypatch):
    """Baseline strategy still owns the legacy window when flags are off."""
    monkeypatch.setattr(settings, "grokbot_jev_clip_cut_enabled", False)
    cand = {
        "source": "overwolf_game_event",
        "event_type": "kill",
        "metadata": {"anchor_seconds": 50.0},
    }
    s, e = cut_window(cand, {}, 200.0)
    assert s == 47.0 and e == 58.0
