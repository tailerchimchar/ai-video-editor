"""story-join: one video from several games' story.mp4, a swipe between games."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_video_editor.config import settings
from ai_video_editor.story import join
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
    calls: dict = {"ffmpeg": [], "join_files": []}

    def write_files(rounds, out, *, transition_s, title=None, subscribe=None):
        calls["join_files"].append((list(rounds), transition_s, subscribe))
        return Path(out).with_suffix(".join.json")

    def run(cmd, timeout):
        calls["ffmpeg"].append(cmd)
        return True, None

    monkeypatch.setattr(join, "_duration", lambda path: 100.0)
    monkeypatch.setattr(join, "write_join_files", write_files)
    monkeypatch.setattr(join, "_run_ffmpeg", run)
    return calls


def _graph(cmd: list[str]) -> str:
    return cmd[cmd.index("-filter_complex") + 1]


def test_round_label():
    assert join.round_label(R1) == "Zeri 8/7/12"
    assert join.round_label("weird_name") == "weird_name"


def test_swipe_offsets_overlap_each_game():
    assert join.swipe_offsets([100.0, 50.0, 30.0], 0.5) == [99.5, 149.0]


def test_join_swipes_right_to_left_without_cards(ws, fakes, tmp_path):
    s1, s2 = _story(ws, R1), _story(ws, R2)
    out = tmp_path / "o.mp4"
    assert join.join_stories([R1, R2], out) == out
    [cmd] = fakes["ffmpeg"]
    assert cmd[-1] == out.as_posix()
    ins = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-i"]
    assert ins == [s1.as_posix(), s2.as_posix()]  # no cards, no whoosh/asset on disk
    g = _graph(cmd)
    assert "xfade=transition=slideleft:duration=0.5:offset=99.5" in g
    assert "acrossfade=d=0.5" in g and "amix" not in g and "overlay" not in g
    assert fakes["join_files"] == [([R1, R2], 0.5, None)]


def test_whoosh_peak_lands_mid_swipe(ws, fakes, tmp_path):
    for r in (R3, R1, R2):
        _story(ws, r)
    wh = _asset(ws, join.WHOOSH_ASSET)
    wh.with_suffix(".json").write_text('{"peak_s": 0.25}')
    join.join_stories([R3, R1, R2], tmp_path / "j.mp4", subscribe_after=None)
    g = _graph(fakes["ffmpeg"][0])
    assert "asplit=2" in g
    assert "adelay=99500|99500" in g  # 99.5 + 0.25 - 0.25
    assert "adelay=199000|199000" in g
    assert "amix=inputs=3:duration=first:normalize=0" in g


def test_missing_story_names_it(ws, fakes, tmp_path):
    _story(ws, R1)
    with pytest.raises(StoryRenderError, match=R2):
        join.join_stories([R1, R2], tmp_path / "o.mp4")


def test_cli_default_out(ws, fakes):
    _story(ws, R1)
    _story(ws, R2)
    assert join.main([R1, R2]) == 0
    expected = ws / "story" / "joined_2026-11-23_zeri-jhin.mp4"
    assert fakes["ffmpeg"][0][-1] == expected.as_posix()


def test_cli_needs_two_rounds(ws, fakes):
    with pytest.raises(SystemExit):
        join.main([R1])


def test_subscribe_overlay_after_game_one_bottom_centre(ws, fakes, tmp_path):
    # Owner 2026-11-26: after game 1, bottom centre above the ability bar.
    for r in (R3, R1, R2):
        _story(ws, r)
    asset = _asset(ws, join.SUBSCRIBE_ASSET)
    join.join_stories([R3, R1, R2], tmp_path / "j.mp4")
    [cmd] = fakes["ffmpeg"]
    assert asset.as_posix() in cmd
    g = _graph(cmd)
    at = 100.0 - 0.5 + join.SUBSCRIBE_LEAD_S
    assert f"setpts=PTS-STARTPTS+{at}/TB" in g
    assert "overlay=x=(main_w-overlay_w)/2:y=main_h*0.84-overlay_h" in g
    assert "eof_action=pass" in g
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
