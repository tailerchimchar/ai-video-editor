"""Story render: _with_sfx mixes kaching on kills and a death sound on deaths."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_video_editor.config import settings
from ai_video_editor.story import render
from ai_video_editor.story.planner import Chapter


@pytest.fixture
def sfx_seams(monkeypatch):
    calls = []
    monkeypatch.setattr(render, "kaching_path", lambda: Path("k.wav"))
    monkeypatch.setattr(render, "death_path", lambda: Path("d.wav"))
    monkeypatch.setattr(render, "sfx_hit_s", lambda p: 0.0)

    def fake_sfx(src, out, *, sfx):
        calls.append((src, out, sfx))
        return True, None

    monkeypatch.setattr(render, "apply_sfx", fake_sfx)
    return calls


def _ch(kills=(), deaths=()):
    return Chapter(
        title="t",
        start_s=100.0,
        end_s=130.0,
        kind="fight",
        kill_times_s=list(kills),
        death_times_s=list(deaths),
    )


def test_kills_and_deaths_both_mixed(sfx_seams, tmp_path):
    out = render._with_sfx(tmp_path / "chapter_01.mp4", _ch([110.0], [120.0]), 1)
    assert out.name == "chapter_01_sfx.mp4"
    sfx = sfx_seams[0][2]
    assert [(s["sfx_path"], s["at_seconds"], s["volume"]) for s in sfx] == [
        ("k.wav", 10.0, 0.8),
        ("d.wav", 20.0, 0.9),
    ]


def test_death_gated_off_by_config(sfx_seams, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "story_death_sfx", False)
    render._with_sfx(tmp_path / "chapter_01.mp4", _ch([110.0], [120.0]), 1)
    assert [s["sfx_path"] for s in sfx_seams[0][2]] == ["k.wav"]


def test_kill_gated_off_by_config(sfx_seams, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "story_kill_sfx", False)
    render._with_sfx(tmp_path / "chapter_01.mp4", _ch([110.0], [120.0]), 1)
    assert [s["sfx_path"] for s in sfx_seams[0][2]] == ["d.wav"]


def test_onset_subtracted_and_clamped(sfx_seams, monkeypatch, tmp_path):
    monkeypatch.setattr(render, "sfx_hit_s", lambda p: 0.25 if p.name == "k.wav" else 0.0)
    render._with_sfx(tmp_path / "chapter_01.mp4", _ch([110.0, 100.1], []), 1)
    assert [s["at_seconds"] for s in sfx_seams[0][2]] == [9.75, 0.0]


def test_no_entries_returns_part_unchanged(sfx_seams, tmp_path):
    part = tmp_path / "chapter_01.mp4"
    assert render._with_sfx(part, _ch(), 1) is part
    assert sfx_seams == []


def test_death_default_settings():
    assert settings.story_death_sfx is True
    assert settings.story_death_sfx_volume == 0.9
