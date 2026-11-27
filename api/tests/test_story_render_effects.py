"""Story render: owner-requested effects (letterbox, slow-mo, zoom, focus) over
a time range inside a chapter (AVE-66)."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from ai_video_editor import edits
from ai_video_editor.config import settings
from ai_video_editor.story import render
from ai_video_editor.story.planner import Chapter, ChapterEffect, StoryPlan


@pytest.fixture(autouse=True)
def _no_audio_levelling(monkeypatch):
    """The final loudness pass is tested in test_story_render.py; skip it here."""
    monkeypatch.setattr(render, "level_audio", lambda story: None)


def _fx(kind, start, end, **params):
    return ChapterEffect(kind=kind, start_s=start, end_s=end, params=params)


# --- step 1: filters per effect kind ---


def test_focus_filter_is_a_working_geq_over_frame_size():
    f = edits.focus_filter(0.4, 0.6, 0.2, 0.3)
    assert f.startswith("format=yuv420p,geq=lum='")
    assert "hypot(X-W*0.4,Y-H*0.6)" in f
    assert "(min(W,H)*0.2)" in f
    assert "lum(X,Y)*0.3" in f
    assert f.endswith(":cb='cb(X,Y)':cr='cr(X,Y)'")


def test_letterbox_filter_draws_two_black_bars_default_twelve_percent():
    f = render.effect_video_filter([_fx("letterbox", 0, 1)])
    bar = round(1080 * 0.12)
    assert f == (
        f"drawbox=x=0:y=0:w=1920:h={bar}:color=black:t=fill,"
        f"drawbox=x=0:y={1080 - bar}:w=1920:h={bar}:color=black:t=fill"
    )


def test_letterbox_bar_param():
    f = render.effect_video_filter([_fx("letterbox", 0, 1, bar=0.2)])
    assert "h=216:" in f and "y=864:" in f


def test_zoom_filter_crops_roi_and_scales_back():
    f = render.effect_video_filter([_fx("zoom", 0, 1)])
    assert f == ("crop=iw/1.3:ih/1.3:(iw-iw/1.3)/2:(ih-ih/1.3)/2,scale=1920:1080,setsar=1")
    g = render.effect_video_filter([_fx("zoom", 0, 1, factor=2.0, roi="minimap_lol")])
    assert g.startswith("crop=ih*0.22:ih*0.22:iw-ih*0.23:ih*0.77,scale=1920:1080")


def test_focus_effect_reuses_edits_focus_filter_with_router_defaults():
    assert render.effect_video_filter([_fx("focus", 0, 1)]) == edits.focus_filter(
        0.5, 0.5, 0.2, 0.3
    )
    assert render.effect_video_filter(
        [_fx("focus", 0, 1, x=0.3, y=0.7, radius=0.1, dim=0.5)]
    ) == edits.focus_filter(0.3, 0.7, 0.1, 0.5)


def test_slowmo_has_no_spatial_filter():
    assert render.effect_video_filter([_fx("slowmo", 0, 1)]) == ""


def test_stacked_filters_zoom_then_focus_then_letterbox():
    f = render.effect_video_filter([_fx("letterbox", 0, 1), _fx("focus", 0, 1), _fx("zoom", 0, 1)])
    assert f.index("crop=") < f.index("geq=") < f.index("drawbox=")


def test_zoom_out_shrinks_the_frame_into_black_borders():
    # Owner 2026-11-27: Isab-style comic zoom-out, black borders all round.
    f = render.effect_video_filter([_fx("zoom_out", 0, 1)])
    assert f == "scale=1152:648,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
    assert render.effect_video_filter([_fx("zoom_out", 0, 1, factor=0.5)]).startswith(
        "scale=960:540,"
    )


def test_zoom_out_factor_is_clamped(caplog):
    with caplog.at_level(logging.WARNING):
        f = render.effect_video_filter([_fx("zoom_out", 0, 1, factor=3)])
    assert f.startswith("scale=1920:1080,") and "clamped" in caplog.text
    assert render.effect_video_filter([_fx("zoom_out", 0, 1, factor=0.01)]).startswith(
        "scale=384:216,"
    )


def test_zoom_out_comes_after_focus_and_before_letterbox():
    f = render.effect_video_filter(
        [_fx("letterbox", 0, 1), _fx("zoom_out", 0, 1), _fx("focus", 0, 1)]
    )
    assert f.index("geq=") < f.index("pad=") < f.index("drawbox=")


def test_atempo_chain():
    assert render.atempo_chain(0.5) == "atempo=0.5"
    assert render.atempo_chain(0.25) == "atempo=0.5,atempo=0.5"
    assert render.atempo_chain(0.3) == "atempo=0.5,atempo=0.6"
    assert render.atempo_chain(0.8) == "atempo=0.8"


def test_slowmo_factor_default_and_clamp(caplog):
    assert render.slowmo_factor([_fx("slowmo", 0, 1)]) == 0.5
    assert render.slowmo_factor([_fx("zoom", 0, 1)]) is None
    with caplog.at_level(logging.WARNING):
        assert render.slowmo_factor([_fx("slowmo", 0, 1, factor=0.1)]) == 0.25
    assert "slowmo" in caplog.text


def test_segment_cmd_slowmo_stretches_video_and_audio(monkeypatch):
    monkeypatch.setattr(settings, "story_mic_loudnorm", True)
    cmd = render.effect_segment_cmd(
        "src.mp4", 12.0, 14.0, "seg.mp4", [_fx("slowmo", 12, 14)], mic_stream=2
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert cmd[cmd.index("-ss") + 1] == "12.0" and cmd[cmd.index("-to") + 1] == "14.0"
    assert fc.startswith("[0:v]setpts=PTS/0.5,scale=1920:1080,setsar=1,fps=30")
    # mic and game slow together: tempo after the mix
    assert "amix=inputs=2:duration=first:normalize=0,atempo=0.5[a]" in fc


def test_segment_cmd_effects_and_fades_on_output_duration():
    cmd = render.effect_segment_cmd(
        "src.mp4",
        10.0,
        12.0,
        "seg.mp4",
        [_fx("slowmo", 10, 12), _fx("letterbox", 10, 12)],
        mic_stream=None,
        fade_in_s=0.5,
        fade_out_s=0.5,
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    # 2 s at half speed renders 4 s: the fade-out starts at 3.5
    assert "drawbox=" in fc
    assert "fade=t=in:st=0:d=0.5,fade=t=out:st=3.5:d=0.5[v]" in fc
    assert fc.index("drawbox=") < fc.index("fade=t=in")
    assert "[0:a:0]atempo=0.5,afade=t=in:st=0:d=0.5,afade=t=out:st=3.5:d=0.5[a]" in fc


def test_segment_cmd_fade_only_on_requested_end():
    cmd = render.effect_segment_cmd(
        "src.mp4", 10.0, 12.0, "seg.mp4", [_fx("zoom", 10, 12)], mic_stream=None, fade_in_s=0.5
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "fade=t=in:st=0:d=0.5" in fc and "fade=t=out" not in fc


def test_segment_cmd_without_effects_or_audio():
    cmd = render.effect_segment_cmd(
        "src.mp4", 10.0, 12.0, "seg.mp4", [_fx("slowmo", 10, 12)], mic_stream=2, has_audio=False
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "[0:a" not in fc
    i = cmd.index(render.SILENT_AUDIO)
    assert cmd[i - 5 : i] == ["-f", "lavfi", "-t", "4.0", "-i"]
    maps = [cmd[j + 1] for j, a in enumerate(cmd) if a == "-map"]
    assert maps == ["[v]", "1:a"]


# --- step 2: segmenting a chapter at effect boundaries ---


def _kinds(segs):
    return [(a, b, [fx.kind for fx in active]) for a, b, active in segs]


def test_segments_without_effects_is_the_whole_chapter():
    assert _kinds(render.effect_segments([], 10.0, 20.0)) == [(10.0, 20.0, [])]


def test_segments_effect_in_the_middle():
    segs = render.effect_segments([_fx("zoom", 12, 14)], 10.0, 20.0)
    assert _kinds(segs) == [(10.0, 12.0, []), (12.0, 14.0, ["zoom"]), (14.0, 20.0, [])]


def test_segments_effect_at_the_start():
    segs = render.effect_segments([_fx("letterbox", 10, 13)], 10.0, 20.0)
    assert _kinds(segs) == [(10.0, 13.0, ["letterbox"]), (13.0, 20.0, [])]


def test_segments_effect_covers_whole_chapter_is_clamped():
    segs = render.effect_segments([_fx("slowmo", 5, 25)], 10.0, 20.0)
    assert _kinds(segs) == [(10.0, 20.0, ["slowmo"])]


def test_segments_effect_outside_chapter_ignored():
    segs = render.effect_segments([_fx("zoom", 30, 35), _fx("zoom", 1, 5)], 10.0, 20.0)
    assert _kinds(segs) == [(10.0, 20.0, [])]


def test_segments_stacked_kinds():
    fx = [_fx("letterbox", 12, 16), _fx("slowmo", 12, 16), _fx("focus", 14, 16)]
    segs = render.effect_segments(fx, 10.0, 20.0)
    assert _kinds(segs) == [
        (10.0, 12.0, []),
        (12.0, 14.0, ["letterbox", "slowmo"]),
        (14.0, 16.0, ["letterbox", "slowmo", "focus"]),
        (16.0, 20.0, []),
    ]


def test_segments_overlapping_slowmos_later_wins_with_warning(caplog):
    first, later = _fx("slowmo", 12, 16, factor=0.5), _fx("slowmo", 14, 18, factor=0.25)
    with caplog.at_level(logging.WARNING):
        segs = render.effect_segments([first, later], 10.0, 20.0)
    assert [(a, b) for a, b, _ in segs] == [(10, 12), (12, 14), (14, 16), (16, 18), (18, 20)]
    assert segs[1][2] == [first]
    assert segs[2][2] == [later]
    assert segs[3][2] == [later]
    assert "overlap" in caplog.text


def test_time_map_before_inside_after_half_speed_slowmo():
    tmap = render.effect_time_map([_fx("slowmo", 12, 14)], 10.0)
    assert tmap(11.0) == pytest.approx(1.0)
    assert tmap(12.0) == pytest.approx(2.0)
    assert tmap(13.0) == pytest.approx(4.0)
    assert tmap(14.0) == pytest.approx(6.0)
    assert tmap(15.0) == pytest.approx(7.0)


def test_time_map_identity_without_slowmo():
    tmap = render.effect_time_map([_fx("letterbox", 12, 14), _fx("zoom", 11, 13)], 10.0)
    assert [tmap(t) for t in (10.0, 12.5, 19.0)] == pytest.approx([0.0, 2.5, 9.0])
    assert render.effect_time_map([], 10.0)(13.0) == pytest.approx(3.0)


def test_time_map_uses_the_winning_slowmo_on_overlap():
    fx = [_fx("slowmo", 12, 16, factor=0.5), _fx("slowmo", 14, 18, factor=0.25)]
    tmap = render.effect_time_map(fx, 10.0)
    # 2 s normal, 2 s at 0.5 (4 s), then 15 s is 1 s into 0.25 (4 s)
    assert tmap(15.0) == pytest.approx(2.0 + 4.0 + 4.0)


@pytest.fixture
def fake_ffmpeg(monkeypatch, tmp_path):
    calls: dict[str, list] = {"ffmpeg": [], "concat": [], "sfx": []}

    def run(cmd, timeout):
        calls["ffmpeg"].append(cmd)
        Path(cmd[-1]).write_bytes(b"x")
        return True, None

    def concat(parts, out_path):
        calls["concat"].append(([Path(p).name for p in parts], Path(out_path).name))
        Path(out_path).write_bytes(b"x")
        return True, None

    def sfx(src, out, *, sfx):
        calls["sfx"].append(sfx)
        Path(out).write_bytes(b"x")
        return True, None

    monkeypatch.setattr(render, "_run_ffmpeg", run)
    monkeypatch.setattr(render, "_concat", concat)
    monkeypatch.setattr(render, "apply_sfx", sfx)
    monkeypatch.setattr(render, "_probe_audio_count", lambda p: 3)
    monkeypatch.setattr(render, "kaching_path", lambda: Path("k.wav"))
    monkeypatch.setattr(render, "sfx_hit_s", lambda p: 0.0)
    monkeypatch.setattr(settings, "story_transition_s", 0.25)
    return calls


def _story(*chapters):
    return StoryPlan(
        asset_id="a1",
        chapters=list(chapters),
        total_s=sum(c.end_s - c.start_s for c in chapters),
        mic_stream=2,
        audio="mic_over_game",
        cards=False,
    )


def _fc(cmd):
    return cmd[cmd.index("-filter_complex") + 1]


def test_render_splits_effect_chapter_into_segments_and_concats(fake_ffmpeg, tmp_path):
    ch = Chapter(
        title="t", start_s=10.0, end_s=20.0, kind="fight", effects=[_fx("letterbox", 12, 14)]
    )
    render.render_story("src.mp4", _story(ch), tmp_path / "out", cards=False)
    cmds = fake_ffmpeg["ffmpeg"]
    assert [Path(c[-1]).name for c in cmds] == [
        "chapter_01_seg01.mp4",
        "chapter_01_seg02.mp4",
        "chapter_01_seg03.mp4",
    ]
    assert [(c[c.index("-ss") + 1], c[c.index("-to") + 1]) for c in cmds] == [
        ("10.0", "12.0"),
        ("12.0", "14.0"),
        ("14.0", "20.0"),
    ]
    assert "drawbox" not in _fc(cmds[0]) and "drawbox" in _fc(cmds[1])
    # the fade only at the chapter's own ends
    assert "fade=t=in" in _fc(cmds[0]) and "fade=t=out" not in _fc(cmds[0])
    assert "fade=" not in _fc(cmds[1])
    assert "fade=t=out" in _fc(cmds[2]) and "fade=t=in" not in _fc(cmds[2])
    chapter_concat, story_concat = fake_ffmpeg["concat"]
    assert chapter_concat == (
        ["chapter_01_seg01.mp4", "chapter_01_seg02.mp4", "chapter_01_seg03.mp4"],
        "chapter_01.mp4",
    )
    assert story_concat == (["chapter_01.mp4"], "story.mp4")


def test_render_effect_over_whole_chapter_is_one_encode(fake_ffmpeg, tmp_path):
    ch = Chapter(title="t", start_s=10.0, end_s=20.0, kind="fight", effects=[_fx("zoom", 0, 99)])
    render.render_story("src.mp4", _story(ch), tmp_path / "out", cards=False)
    [cmd] = fake_ffmpeg["ffmpeg"]
    assert Path(cmd[-1]).name == "chapter_01.mp4"
    assert "crop=" in _fc(cmd)
    assert "fade=t=in" in _fc(cmd) and "fade=t=out" in _fc(cmd)
    assert fake_ffmpeg["concat"] == [(["chapter_01.mp4"], "story.mp4")]


def test_render_kill_sound_lands_on_the_slowed_frame(fake_ffmpeg, tmp_path):
    ch = Chapter(
        title="t",
        start_s=10.0,
        end_s=20.0,
        kind="fight",
        kill_times_s=[13.0, 16.0],
        effects=[_fx("slowmo", 12, 14)],
    )
    render.render_story("src.mp4", _story(ch), tmp_path / "out", cards=False)
    [sfx] = fake_ffmpeg["sfx"]
    assert [s["at_seconds"] for s in sfx] == [4.0, 8.0]


def test_render_broll_and_effects_renders_broll_and_skips_effects(fake_ffmpeg, tmp_path, caplog):
    from ai_video_editor.story.broll import BRollInsert

    ins = BRollInsert(
        item_id=3031,
        mic_start_s=12.0,
        mic_end_s=14.0,
        src_start_s=50.0,
        src_end_s=52.0,
        build_path=False,
    )
    ch = Chapter(
        title="t",
        start_s=10.0,
        end_s=20.0,
        kind="fight",
        kill_times_s=[16.0],
        broll=[ins],
        effects=[_fx("slowmo", 12, 14)],
    )
    with caplog.at_level(logging.WARNING):
        render.render_story("src.mp4", _story(ch), tmp_path / "out", cards=False)
    [cmd] = fake_ffmpeg["ffmpeg"]
    assert "setpts=PTS/" not in _fc(cmd)
    assert "effects" in caplog.text
    assert [s["at_seconds"] for s in fake_ffmpeg["sfx"][0]] == [6.0]


def _pixel(path: Path, t: float, x: int, y: int) -> tuple[int, int, int]:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(t), "-i", path, "-frames:v", "1",
         "-vf", f"format=rgb24,crop=1:1:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    return raw[0], raw[1], raw[2]


@pytest.mark.real_ffmpeg
@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg"
)
def test_real_ffmpeg_letterbox_and_slowmo(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ffmpeg_path", "ffmpeg")
    monkeypatch.setattr(settings, "ffmpeg_video_codec", "libx264")
    monkeypatch.setattr(settings, "ffmpeg_video_codec_opts", "-preset veryfast -crf 20 -threads 4")
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440",
         "-t", "6", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", src],
        check=True,
    )  # fmt: skip
    ch = Chapter(
        title="t",
        start_s=0.0,
        end_s=6.0,
        kind="fight",
        effects=[_fx("letterbox", 2, 4), _fx("slowmo", 2, 4)],
    )
    plan = StoryPlan(
        asset_id="a1", chapters=[ch], total_s=6.0, mic_stream=None, audio="mixed", cards=False
    )
    out = render.render_story(str(src), plan, tmp_path / "out", cards=False)
    dur = float(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", out],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    )  # fmt: skip
    assert dur == pytest.approx(8.0, abs=0.35)
    # 3 s in the output is inside the slowed, letterboxed range (2 s .. 6 s)
    top, bottom = _pixel(out, 3.0, 960, 20), _pixel(out, 3.0, 960, 1060)
    assert max(top) < 24 and max(bottom) < 24, (top, bottom)
    # outside the effect the same pixel is the test pattern, not a bar
    assert max(_pixel(out, 1.0, 960, 20)) > 60
    print(f"real render: duration={dur:.3f}s top={top} bottom={bottom}")


@pytest.mark.real_ffmpeg
@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg"
)
def test_real_ffmpeg_zoom_out_has_black_borders(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ffmpeg_path", "ffmpeg")
    monkeypatch.setattr(settings, "ffmpeg_video_codec", "libx264")
    monkeypatch.setattr(settings, "ffmpeg_video_codec_opts", "-preset veryfast -crf 20 -threads 4")
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "color=c=white:size=640x360:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440",
         "-t", "4", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", src],
        check=True,
    )  # fmt: skip
    ch = Chapter(title="t", start_s=0.0, end_s=4.0, kind="fight", effects=[_fx("zoom_out", 2, 4)])
    plan = StoryPlan(
        asset_id="a1", chapters=[ch], total_s=4.0, mic_stream=None, audio="mixed", cards=False
    )
    out = render.render_story(str(src), plan, tmp_path / "out", cards=False)
    # inside the effect: corner is a border, centre is still the picture
    assert max(_pixel(out, 3.0, 40, 40)) < 24
    assert min(_pixel(out, 3.0, 960, 540)) > 200
    # before it the corner is the picture
    assert min(_pixel(out, 1.0, 40, 40)) > 200


def test_segments_drop_slivers_shorter_than_a_frame():
    segs = render.effect_segments([_fx("zoom", 10.01, 14)], 10.0, 20.0)
    assert _kinds(segs) == [(10.0, 14.0, ["zoom"]), (14.0, 20.0, [])]


# --- mic mutes ---


def test_audio_filter_mutes_only_the_mic_leg():
    fg = render.audio_filter(2, dur=10.0, mic_mutes=[(1.5, 4.0), (6.0, 7.25)])
    mic_leg = fg.split("[m];")[0]
    assert "volume=0:enable='between(t,1.5,4)'" in mic_leg
    assert "volume=0:enable='between(t,6,7.25)'" in mic_leg
    assert fg.count("volume=0:") == 2  # none on the game leg
    assert fg.index("between") < fg.index("amix")


def test_audio_filter_without_mutes_is_unchanged():
    assert render.audio_filter(2, dur=10.0) == render.audio_filter(2, dur=10.0, mic_mutes=())
    assert "between" not in render.audio_filter(2, dur=10.0, fade_s=0.5)


def test_audio_filter_ignores_mutes_without_a_mic():
    assert render.audio_filter(None, mic_mutes=[(1.0, 2.0)]) == render.audio_filter(None)


def test_chapter_and_broll_and_effect_cmds_carry_mutes():
    cmd = render.chapter_cmd("s.mp4", 10, 20, "o.mp4", mic_stream=2, mic_mutes=[(1, 2)])
    assert "between(t,1,2)" in _fc(cmd)
    cmd = render.effect_segment_cmd(
        "s.mp4", 10, 20, "o.mp4", [_fx("letterbox", 10, 20)], mic_stream=2, mic_mutes=[(1, 2)]
    )
    assert "between(t,1,2)" in _fc(cmd)
    assert "between" not in _fc(render.chapter_cmd("s.mp4", 10, 20, "o.mp4", mic_stream=2))


def test_relative_mutes_clamp_and_drop():
    got = render.relative_mutes([(5.0, 12.0), (12.0, 13.0), (18.0, 30.0), (0.0, 3.0)], 10.0, 20.0)
    assert got == [(0.0, 2.0), (2.0, 3.0), (8.0, 10.0)]


def test_render_story_passes_chapter_mutes_relative(fake_ffmpeg, tmp_path):
    ch = Chapter(title="t", start_s=10.0, end_s=20.0, kind="fight", mic_mutes=[(12.0, 15.0)])
    render.render_story("src.mp4", _story(ch), tmp_path / "out", cards=False)
    [cmd] = fake_ffmpeg["ffmpeg"]
    assert "between(t,2,5)" in _fc(cmd)


def test_effect_chapter_shifts_mutes_per_segment(fake_ffmpeg, tmp_path):
    ch = Chapter(
        title="t", start_s=10.0, end_s=20.0, kind="fight",
        effects=[_fx("letterbox", 12, 14)], mic_mutes=[(13.0, 16.0)],
    )  # fmt: skip
    render.render_story("src.mp4", _story(ch), tmp_path / "out", cards=False)
    c1, c2, c3 = fake_ffmpeg["ffmpeg"]  # segments 10-12, 12-14, 14-20
    assert "between" not in _fc(c1)
    assert "between(t,1,2)" in _fc(c2)  # 13-14 relative to 12
    assert "between(t,0,2)" in _fc(c3)  # 14-16 relative to 14
