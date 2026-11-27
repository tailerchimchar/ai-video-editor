"""Story render: dip fades, no cards for plays plans, kaching on owner kills."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_video_editor.story import render
from ai_video_editor.story.planner import Chapter, StoryPlan


@pytest.fixture(autouse=True)
def _no_audio_levelling(monkeypatch):
    """The final loudness pass is tested in test_story_render.py; skip it here."""
    monkeypatch.setattr(render, "level_audio", lambda story: None)


def test_chapter_cmd_has_fades_when_transition_set():
    cmd = render.chapter_cmd("a.mp4", 100.0, 120.0, "o.mp4", mic_stream=2, fade_s=0.25)
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "fade=t=in:st=0:d=0.25,fade=t=out:st=19.75:d=0.25[v]" in fc
    assert "afade=t=out:st=19.75:d=0.25[a]" in fc


def test_chapter_cmd_no_fades_by_default():
    cmd = render.chapter_cmd("a.mp4", 100.0, 120.0, "o.mp4", mic_stream=None)
    assert "fade" not in cmd[cmd.index("-filter_complex") + 1]


def test_chapter_cmd_fades_without_mic():
    cmd = render.chapter_cmd("a.mp4", 0.0, 20.0, "o.mp4", mic_stream=None, fade_s=0.25)
    assert "[0:a:0]afade=t=in" in cmd[cmd.index("-filter_complex") + 1]


@pytest.fixture
def seams(monkeypatch):
    calls = {"cards": [], "sfx": [], "ffmpeg": []}
    monkeypatch.setattr(render, "_probe_audio_count", lambda p: 3)
    monkeypatch.setattr(
        render, "_run_ffmpeg", lambda cmd, t: (calls["ffmpeg"].append(cmd), (True, None))[1]
    )
    monkeypatch.setattr(render, "_render_card", lambda title, out: calls["cards"].append(title))
    monkeypatch.setattr(
        render, "_concat", lambda parts, out: (calls.update(parts=parts), (True, None))[1]
    )
    monkeypatch.setattr(render, "kaching_path", lambda: Path("k.wav"))

    def fake_sfx(src, out, *, sfx):
        calls["sfx"].append((src, out, sfx))
        return True, None

    monkeypatch.setattr(render, "apply_sfx", fake_sfx)
    return calls


def _plan(cards: bool, kills: list[float]) -> StoryPlan:
    ch = Chapter(title="t", start_s=1395.0, end_s=1420.0, kind="fight", kill_times_s=kills)
    return StoryPlan(chapters=[ch], total_s=25, mic_stream=None, audio="mixed", cards=cards)


def test_render_story_skips_cards_when_plan_cards_false(seams, tmp_path):
    render.render_story("a.mp4", _plan(False, []), tmp_path)
    assert seams["cards"] == []
    assert "fade=t=in" in seams["ffmpeg"][0][seams["ffmpeg"][0].index("-filter_complex") + 1]


def test_render_story_cards_when_plan_cards_true(seams, tmp_path):
    render.render_story("a.mp4", _plan(True, []), tmp_path)
    assert seams["cards"] == ["t"]
    assert "fade" not in " ".join(seams["ffmpeg"][0])


def test_kaching_applied_at_kill_offsets(seams, tmp_path):
    render.render_story("a.mp4", _plan(False, [1407.4]), tmp_path)
    _src, out, sfx = seams["sfx"][0]
    assert [s["at_seconds"] for s in sfx] == [12.4]
    assert out.endswith("chapter_01_sfx.mp4")
    assert seams["parts"] == [out]


def test_kaching_failure_keeps_plain_part(seams, monkeypatch, tmp_path):
    monkeypatch.setattr(render, "apply_sfx", lambda *a, **k: (False, "boom"))
    render.render_story("a.mp4", _plan(False, [1407.4]), tmp_path)
    assert seams["parts"][0].endswith("chapter_01.mp4")
