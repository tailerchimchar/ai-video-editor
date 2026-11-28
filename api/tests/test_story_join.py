"""story-join: one video from several games' story.mp4, a swipe between games.

Rendered in segments (bodies + swipes, video only), one soundtrack pass and a
copy concat, so no ffmpeg pass holds more than two videos."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_video_editor.config import settings
from ai_video_editor.story import join
from ai_video_editor.story.bookends import HookPick
from ai_video_editor.story.render import StoryRenderError

R1 = "2026-11-23_2056_zeri_8-7-12"
R2 = "2026-11-24_1010_jhin_3-1-9"
R3 = "2026-11-26_0235_xayah_5-0-2"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", tmp_path / "ws")
    return tmp_path / "ws"


def _story(ws: Path, name: str) -> Path:
    p = ws / "story" / name / "story.mp4"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def _asset(ws: Path, parts: tuple[str, ...]) -> Path:
    a = ws.joinpath(*parts)
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_bytes(b"a")
    return a


@pytest.fixture
def fakes(monkeypatch):
    calls: dict = {"ffmpeg": [], "join_files": [], "bookends": []}

    def write_files(
        rounds, out, *, transition_s, title=None, subscribe=None, lead_s=0.0, outro=None
    ):
        calls["join_files"].append((list(rounds), transition_s, subscribe))
        calls["bookends"].append((lead_s, outro))
        return Path(out).with_suffix(".join.json")

    def run(cmd, timeout):
        calls["ffmpeg"].append(cmd)
        return True, None

    monkeypatch.setattr(join, "_duration", lambda path: 100.0)
    monkeypatch.setattr(join, "write_join_files", write_files)
    monkeypatch.setattr(join, "_run_ffmpeg", run)
    return calls


def _graph(cmd: list[str]) -> str:
    return cmd[cmd.index("-filter_complex") + 1] if "-filter_complex" in cmd else ""


def _segments(calls) -> list[list[str]]:
    return [c for c in calls["ffmpeg"] if "-an" in c]


def _audio(calls) -> list[str]:
    [a] = [c for c in calls["ffmpeg"] if "-vn" in c]
    return a


def _inputs(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, x in enumerate(cmd) if x == "-i"]


def test_round_label():
    assert join.round_label(R1) == "Zeri 8/7/12"
    assert join.round_label("weird_name") == "weird_name"


def test_swipe_offsets_overlap_each_game():
    assert join.swipe_offsets([100.0, 50.0, 30.0], 0.5) == [99.5, 149.0]


def test_frames_s_snaps_down_to_the_frame_grid():
    assert join.frames_s(100.0) == 100.0
    assert join.frames_s(250.9) == pytest.approx(7527 / 30)
    assert join.frames_s(250.99) == pytest.approx(7529 / 30)


def test_plan_segments_cover_the_joined_length():
    segs = join.plan_segments([10.0, 20.0, 30.0], 0.5, 0.5)
    assert [(s.kind, s.piece) for s in segs] == [
        ("body", 0),
        ("swipe", 0),
        ("body", 1),
        ("swipe", 1),
        ("body", 2),
    ]
    assert [s.start_s for s in segs] == [0.0, 9.0, 1.0, 19.0, 1.0]
    assert [s.dur_s for s in segs] == [9.0, 1.5, 18.0, 1.5, 29.0]
    assert sum(s.dur_s for s in segs) == 60.0 - 2 * 0.5
    # piece 2 starts mid-swipe, where swipe_offsets puts it
    assert segs[1].at_s + 0.5 == join.swipe_offsets([10.0, 20.0, 30.0])[0]
    assert segs[2].frames == 540


def test_plan_segments_rejects_a_piece_shorter_than_two_swipes():
    with pytest.raises(StoryRenderError):
        join.plan_segments([10.0, 1.5, 10.0])


def test_join_swipes_right_to_left_in_segments(ws, fakes, tmp_path):
    s1, s2 = _story(ws, R1), _story(ws, R2)
    out = tmp_path / "o.mp4"
    assert join.join_stories([R1, R2], out) == out
    segs = _segments(fakes)
    assert len(segs) == 3  # body, swipe, body
    assert all(len(_inputs(c)) <= 2 for c in segs)
    body0, swipe, body1 = segs
    assert _inputs(body0) == [s1.as_posix()] and body0[body0.index("-frames:v") + 1] == "2970"
    assert _inputs(swipe) == [s1.as_posix(), s2.as_posix()]
    assert swipe[swipe.index("-ss") + 1] == "99.0"
    assert "xfade=transition=slideleft:duration=0.5:offset=0.5" in _graph(swipe)
    assert body1[body1.index("-ss") + 1] == "1.0"
    g = _graph(_audio(fakes))
    # sample-exact placement: piece 2 delayed to 99.5 s, faded over the swipe
    assert "afade=t=out:st=99.5:d=0.5" in g and "afade=t=in:d=0.5" in g
    assert "adelay=0S|0S" in g and f"adelay={99.5 * 48000:.0f}S|{99.5 * 48000:.0f}S" in g
    assert "amix=inputs=2:duration=longest:normalize=0" in g
    assert "apad=whole_dur=199.5,atrim=duration=199.5" in g
    concat = fakes["ffmpeg"][-1]
    assert concat[:3] == [settings.ffmpeg_path, "-y", "-f"] and concat[-1] == out.as_posix()
    assert "copy" in concat
    assert all("overlay" not in _graph(c) for c in fakes["ffmpeg"])
    assert fakes["join_files"] == [([R1, R2], 0.5, None)]


def test_whoosh_peak_lands_mid_swipe(ws, fakes, tmp_path):
    for r in (R3, R1, R2):
        _story(ws, r)
    wh = _asset(ws, join.WHOOSH_ASSET)
    wh.with_suffix(".json").write_text('{"peak_s": 0.25}')
    join.join_stories([R3, R1, R2], tmp_path / "j.mp4", subscribe_after=None)
    g = _graph(_audio(fakes))
    assert "asplit=2" in g
    assert "adelay=99500|99500" in g  # 99.5 + 0.25 - 0.25
    assert "adelay=199000|199000" in g
    assert "amix=inputs=5:duration=longest:normalize=0" in g  # 3 games + 2 whooshes


def test_missing_story_names_it(ws, fakes, tmp_path):
    _story(ws, R1)
    with pytest.raises(StoryRenderError, match=R2):
        join.join_stories([R1, R2], tmp_path / "o.mp4")


def test_cli_default_out(ws, fakes):
    _story(ws, R1)
    _story(ws, R2)
    assert join.main([R1, R2]) == 0
    expected = ws / "story" / "joined_2026-11-23_zeri-jhin.mp4"
    assert fakes["ffmpeg"][-1][-1] == expected.as_posix()


def test_cli_hook_mode_sets_pick_mode(ws, monkeypatch):
    seen = {}
    monkeypatch.setattr(join, "join_stories", lambda *a, **k: seen.update(k) or Path("o.mp4"))
    assert join.main(["r1", "r2", "--hook", "1:1", "--hook-mode", "play"]) == 0
    assert seen["hook"] == [HookPick(1, 1, "", mode="play")]
    join.main(["r1", "r2", "--hook", "1:1"])
    assert seen["hook"][0].mode == "tease"


def test_cli_needs_two_rounds(ws, fakes):
    with pytest.raises(SystemExit):
        join.main([R1])


def test_subscribe_overlay_after_game_one_bottom_centre(ws, fakes, tmp_path):
    # Owner 2026-11-26: after game 1, bottom centre above the ability bar.
    for r in (R3, R1, R2):
        _story(ws, r)
    asset = _asset(ws, join.SUBSCRIBE_ASSET)
    join.join_stories([R3, R1, R2], tmp_path / "j.mp4")
    with_sub = [c for c in _segments(fakes) if asset.as_posix() in c]
    [seg] = with_sub
    # game 2 starts at 99.5; its body segment starts 1.0 s in, at 100.5
    g = _graph(seg)
    assert f"setpts=PTS-STARTPTS+{99.5 + join.SUBSCRIBE_LEAD_S - 100.5}/TB" in g
    assert "overlay=x=(main_w-overlay_w)/2:y=main_h*0.84-overlay_h" in g
    assert "eof_action=pass" in g
    assert _inputs(seg)[0] == join.story_path(R1).as_posix()
    [(rounds, swipe, sub)] = fakes["join_files"]
    assert rounds == [R3, R1, R2] and swipe == 0.5 and sub[0] == 1


def test_no_subscribe_without_asset_or_when_disabled(ws, fakes, tmp_path):
    _story(ws, R1)
    _story(ws, R2)
    join.join_stories([R1, R2], tmp_path / "a.mp4")  # no asset on disk
    _asset(ws, join.SUBSCRIBE_ASSET)
    join.join_stories([R1, R2], tmp_path / "b.mp4", subscribe_after=None)
    join.join_stories([R1, R2], tmp_path / "c.mp4", subscribe_after=2)  # after the last game
    assert all("overlay" not in _graph(c) for c in fakes["ffmpeg"])
    assert all(sub is None for _, _, sub in fakes["join_files"])


def test_hook_and_outro_wrap_the_games(ws, fakes, tmp_path, monkeypatch):
    from ai_video_editor.story import bookends

    for r in (R3, R1, R2):
        _story(ws, r)
    asset = _asset(ws, join.SUBSCRIBE_ASSET)
    seen: dict = {}

    def fake_hook(clips, work, *, title):
        seen["hook"] = (clips, title)
        return [work / "hook_1.mp4", work / "hook_2.mp4"]

    def fake_outro(last, work, *, subscribe, music, music_start_s):
        seen["outro"] = last
        return work / "outro.mp4"

    monkeypatch.setattr(join, "hook_clips", lambda rounds, picks: list(picks))
    monkeypatch.setattr(bookends, "render_hook", fake_hook)
    monkeypatch.setattr(bookends, "render_outro", fake_outro)
    out = tmp_path / "j.mp4"
    picks = [bookends.HookPick(3, 2), bookends.HookPick(2, 2)]
    join.join_stories([R3, R1, R2], out, hook=picks, hook_title="Want to know?", outro=True)
    work = join.parts_dir(out)
    assert seen == {"hook": (picks, "Want to know?"), "outro": join.story_path(R2)}
    audio_in = _inputs(_audio(fakes))
    assert audio_in[:2] == [(work / "hook_1.mp4").as_posix(), (work / "hook_2.mp4").as_posix()]
    assert audio_in[5] == (work / "outro.mp4").as_posix()
    segs = _segments(fakes)
    assert sum("slideleft" in _graph(c) for c in segs) == 5
    # 6 pieces of 100 s: game 1 starts at 199.0, game 2 at 298.5 -> overlay in game 2's body
    [sub_seg] = [c for c in segs if asset.as_posix() in c]
    assert _inputs(sub_seg)[0] == join.story_path(R1).as_posix()
    [(lead, outro)] = fakes["bookends"]
    assert lead == 199.0 and outro == (100.0, work / "outro.mp4")


def test_parts_dir_is_hidden_under_story(ws, tmp_path):
    assert join.parts_dir(tmp_path / "joined_x.mp4") == ws / "story" / "_joined" / "joined_x"
