"""story-join bookends: hook clips cut before the outcome, outro end card."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from ai_video_editor.config import settings
from ai_video_editor.story import bookends
from ai_video_editor.story.bookends import (
    HookClip,
    HookPick,
    hook_window,
    hook_window_rec,
    parse_pick,
)
from ai_video_editor.story.planner import Chapter, StoryPlan
from ai_video_editor.story.render import StoryRenderError


def _plan(*chapters: Chapter) -> StoryPlan:
    return StoryPlan(
        chapters=list(chapters), total_s=0.0, mic_stream=None, audio="mixed", cards=False
    )


def _ch(start, end, kills=(), deaths=()) -> Chapter:
    return Chapter(
        title="c",
        start_s=start,
        end_s=end,
        kind="fight",
        kill_times_s=list(kills),
        death_times_s=list(deaths),
    )


def test_parse_pick():
    assert parse_pick("3:2") == HookPick(3, 2, "")
    assert parse_pick("3:2:2v3? no way") == HookPick(3, 2, "2v3? no way")
    with pytest.raises(ValueError):
        parse_pick("3")


def test_hook_ends_before_the_first_kill():
    # chapter 2 starts at video 100 s (chapter 1 is 100 s long), first kill 20 s in
    plan = _plan(_ch(0, 100), _ch(500, 560, kills=[530, 520]))
    start, end = hook_window(plan, 1, 2.5)
    assert end == pytest.approx(120 - bookends.HOOK_CUT_BEFORE_S)
    assert start == pytest.approx(end - bookends.HOOK_LEAD_S)


def test_hook_hides_a_death_too_and_borrows_lead_in():
    plan = _plan(_ch(0, 100), _ch(500, 560, deaths=[501]))
    start, end = hook_window(plan, 1, 2.5)
    assert end == pytest.approx(101 - bookends.HOOK_CUT_BEFORE_S)
    assert start == pytest.approx(end - bookends.HOOK_LEAD_S)  # reaches into chapter 1


def test_hook_without_events_keeps_the_chapter_start():
    plan = _plan(_ch(0, 30))
    assert hook_window(plan, 0, 2.5) == (0.0, bookends.HOOK_LEAD_S)


def test_hook_too_short_raises():
    with pytest.raises(StoryRenderError):
        hook_window(_plan(_ch(0, 30, kills=[1])), 0, 2.5)


def test_hook_cmd_freezes_blurs_and_pops_text():
    clip = HookClip(Path("s.mp4"), 10.0, 16.0, "2v3?")
    cmd = bookends.hook_cmd(clip, "o.mp4", title="Want to know?", font="C:/f.ttf")
    g = cmd[cmd.index("-filter_complex") + 1]
    assert cmd[cmd.index("-ss") + 1] == "10.0" and cmd[cmd.index("-t") + 1] == "6.0"
    assert f"tpad=stop_mode=clone:stop_duration={bookends.FREEZE_S}" in g
    assert "gblur" in g and "blend=all_expr" in g
    assert "text='2v3?'" in g and "text='Want to know?'" in g
    assert "fontfile='C\\:/f.ttf'" in g
    assert f"apad=whole_dur={6.0 + bookends.FREEZE_S}" in g


def test_hook_cmd_without_text_has_no_teaser():
    clip = HookClip(Path("s.mp4"), 10.0, 16.0, "")
    g = bookends.hook_cmd(clip, "o.mp4", title=None, font="f")
    g = g[g.index("-filter_complex") + 1]
    assert "drawtext" not in g and "[f]null[v]" in g and "gblur" in g


def test_outro_with_and_without_music():
    silent = bookends.outro_cmd(Path("s.mp4"), "o.mp4", subscribe=None, music=None, font="f")
    g = silent[silent.index("-filter_complex") + 1]
    assert "anullsrc" in g and "overlay" not in g and "THANKS FOR WATCHING" in g
    full = bookends.outro_cmd(
        Path("s.mp4"),
        "o.mp4",
        subscribe=Path("sub.mov"),
        music=Path("m.mp3"),
        music_start_s=12.5,
        font="f",
    )
    g = full[full.index("-filter_complex") + 1]
    assert "eof_action=repeat" in g and f"trim=end={bookends.OUTRO_SUB_HOLD_S}" in g
    assert "[2:a]" in g and "afade=t=out" in g
    i = full.index("m.mp3")
    assert full[i - 5 : i] == ["-ss", "12.5", "-t", f"{bookends.OUTRO_S}", "-i"]


def test_music_asset_is_the_owner_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    assert bookends.music_asset() is None
    (tmp_path / "music").mkdir()
    (tmp_path / "music" / "outro.mp3").write_bytes(b"x")
    assert bookends.music_asset() == tmp_path / "music" / "outro.mp3"


def test_play_mode_runs_through_the_last_event():
    plan = _plan(_ch(0, 100), _ch(500, 560, kills=[520, 530]))
    assert hook_window(plan, 1, 2.5, "play") == (118.0, 132.0)


def test_play_mode_caps_at_14_s():
    plan = _plan(_ch(0, 100), _ch(500, 560, kills=[505, 540]))
    start, end = hook_window(plan, 1, 2.5, "play")
    assert (start, end) == (128.0, 142.0)
    assert end - start == bookends.HOOK_PLAY_MAX_S


def test_play_mode_cmd_has_no_freeze():
    clip = HookClip(Path("s.mp4"), 10.0, 16.0, "2v3?", freeze=False)
    cmd = bookends.hook_cmd(clip, "o.mp4", title="Want to know?", font="f")
    g = cmd[cmd.index("-filter_complex") + 1]
    assert "tpad" not in g and "gblur" not in g and "blend" not in g
    assert "text='2v3?'" not in g and "text='Want to know?'" in g
    assert cmd[cmd.index("-t") + 1] == "6.0"
    assert cmd[len(cmd) - cmd[::-1].index("-t") :][0] == "6.0"
    assert "afade=t=out:st=5.75:d=0.25" in g and "apad" not in g


def test_hook_window_rec_play_mode_matches_video_window():
    plan = _plan(_ch(100, 140, kills=[120, 128]))
    assert hook_window_rec(plan, 0, "play") == (116.0, 130.0)
    assert hook_window(plan, 0, 0.0, "play") == (16.0, 30.0)


def test_hook_pick_carries_offsets():
    p = parse_pick("3:2")
    assert (p.start_s, p.end_s, p.id) == (0.0, 0.0, None)
    assert dataclasses.replace(p, end_s=2.0).end_s == 2.0
