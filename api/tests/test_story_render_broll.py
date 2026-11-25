"""Story render with item B-roll: argv only (no real ffmpeg)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_video_editor.config import settings
from ai_video_editor.league.items import ItemInfo
from ai_video_editor.story import render
from ai_video_editor.story.broll import BRollInsert
from ai_video_editor.story.planner import Chapter, StoryPlan

YUN_TAL, BF, DAGGER = 3032, 1038, 1042


def _ins(mic_start: float, dur: float = 4.0, build_path: bool = True) -> BRollInsert:
    return BRollInsert(
        item_id=YUN_TAL,
        mic_start_s=mic_start,
        mic_end_s=mic_start + dur,
        src_start_s=538.0,
        src_end_s=538.0 + dur,
        build_path=build_path,
    )


ICONS = {YUN_TAL: ("ic/3032.png", ["ic/1038.png", "ic/1042.png"])}


def _inputs(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]


def test_inside_chapter_insert_cuts_video_keeps_original_audio():
    cmd = render.broll_chapter_cmd(
        "src.mp4", 600.0, 630.0, "out.mp4", [_ins(610.0)], ICONS, mic_stream=2
    )
    # chapter range, purchase footage, main icon, two component icons
    assert _inputs(cmd) == ["src.mp4", "src.mp4", "ic/3032.png", "ic/1038.png", "ic/1042.png"]
    first = cmd.index("-i")
    assert cmd[first - 4 : first] == ["-ss", "600.0", "-to", "630.0"]
    second = cmd.index("-i", first + 1)
    assert cmd[second - 4 : second] == ["-ss", "538.0", "-to", "542.0"]
    fc = cmd[cmd.index("-filter_complex") + 1]
    # video: [0 -> 10] + purchase (4 s) + [14 -> 30], concatenated video-only
    assert "trim=start=0.0:end=10.0" in fc
    assert "trim=start=14.0:end=30.0" in fc
    assert "concat=n=3:v=1:a=0" in fc
    assert "[2:v]scale=96:96" in fc and "[3:v]scale=48:48" in fc and "[4:v]scale=48:48" in fc
    assert fc.count("overlay=") == 3
    assert "drawbox" in fc  # the build-path arrow
    # audio: the whole chapter's original audio (mic over game), untouched by the cutaway
    assert "[0:a:2]loudnorm" in fc and "[0:a:0]volume=0.35" in fc
    assert "[1:a" not in fc
    maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
    assert maps == ["[v]", "[a]"]
    assert cmd[-1] == "out.mp4"


def test_icons_loop_for_the_insert_length():
    cmd = render.broll_chapter_cmd(
        "src.mp4", 600.0, 630.0, "out.mp4", [_ins(610.0)], ICONS, mic_stream=2
    )
    i = cmd.index("ic/3032.png")
    assert cmd[i - 5 : i - 1] == ["-loop", "1", "-t", "4.0"]


def test_no_build_path_only_main_icon():
    cmd = render.broll_chapter_cmd(
        "src.mp4", 600.0, 630.0, "out.mp4", [_ins(610.0, build_path=False)], ICONS, mic_stream=2
    )
    assert _inputs(cmd) == ["src.mp4", "src.mp4", "ic/3032.png"]
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert fc.count("overlay=") == 1 and "drawbox" not in fc


def test_missing_icon_still_renders_footage():
    cmd = render.broll_chapter_cmd(
        "src.mp4", 600.0, 630.0, "out.mp4", [_ins(610.0)], {}, mic_stream=None
    )
    assert _inputs(cmd) == ["src.mp4", "src.mp4"]
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "overlay" not in fc and "concat=n=3:v=1:a=0" in fc
    assert "[0:a:0]anull[a]" in fc


def test_broll_chapter_is_all_cutaway():
    cmd = render.broll_chapter_cmd(
        "src.mp4", 100.0, 104.0, "out.mp4", [_ins(100.0)], ICONS, mic_stream=2
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "concat=n=1:v=1:a=0" in fc
    assert "trim=start" not in fc


def test_fades_apply_to_the_joined_video():
    cmd = render.broll_chapter_cmd(
        "src.mp4", 600.0, 630.0, "out.mp4", [_ins(610.0)], ICONS, mic_stream=2, fade_s=0.25
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "fade=t=in:st=0:d=0.25,fade=t=out:st=29.75:d=0.25[v]" in fc


def test_no_audio_source_gets_silent_track():
    cmd = render.broll_chapter_cmd(
        "src.mp4", 600.0, 630.0, "out.mp4", [_ins(610.0)], {}, mic_stream=None, has_audio=False
    )
    assert _inputs(cmd)[-1] == render.SILENT_AUDIO
    maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
    assert maps == ["[v]", "2:a"]


@pytest.fixture
def seams(monkeypatch):
    calls: dict[str, list] = {"ffmpeg": [], "sfx": []}
    monkeypatch.setattr(render, "_probe_audio_count", lambda p: 3)

    def run(cmd, t):
        calls["ffmpeg"].append(cmd)
        return True, None

    monkeypatch.setattr(render, "_run_ffmpeg", run)
    monkeypatch.setattr(render, "_concat", lambda parts, out: (True, None))
    monkeypatch.setattr(render, "kaching_path", lambda: Path("k.wav"))

    def fake_sfx(src, out, sfx):
        calls["sfx"].append(sfx)
        return True, None

    monkeypatch.setattr(render, "apply_sfx", fake_sfx)
    return calls


def test_render_story_uses_broll_cmd_and_keeps_kill_sfx(seams, tmp_path):
    for i in (YUN_TAL, BF, DAGGER):
        (tmp_path / f"{i}.png").write_bytes(b"png")
    items = {
        YUN_TAL: ItemInfo(
            id=YUN_TAL, name="Yun Tal", icon_path=tmp_path / "3032.png", from_ids=[BF, DAGGER]
        ),
        BF: ItemInfo(id=BF, name="B. F. Sword", icon_path=tmp_path / "1038.png"),
        DAGGER: ItemInfo(id=DAGGER, name="Dagger", icon_path=tmp_path / "missing.png"),
    }
    chapters = [
        Chapter(title="a", start_s=10.0, end_s=20.0, kind="early"),
        Chapter(
            title="b",
            start_s=600.0,
            end_s=630.0,
            kind="fight",
            kill_times_s=[620.0],
            broll=[_ins(610.0)],
        ),
    ]
    plan = StoryPlan(
        asset_id="a1",
        chapters=chapters,
        total_s=40.0,
        mic_stream=2,
        audio="mic_over_game",
        cards=False,
    )
    assert settings.story_kill_sfx is True
    render.render_story("src.mp4", plan, tmp_path / "out", items=items)
    plain, broll = seams["ffmpeg"]
    assert "overlay" not in plain[plain.index("-filter_complex") + 1]
    ins = _inputs(broll)
    assert (tmp_path / "3032.png").as_posix() in ins
    assert (tmp_path / "1038.png").as_posix() in ins
    assert (tmp_path / "missing.png").as_posix() not in ins  # absent icon skipped
    assert seams["sfx"][0][0]["at_seconds"] == 20.0
    story = (tmp_path / "out" / "story.json").read_text(encoding="utf-8")
    assert '"item_id": 3032' in story
