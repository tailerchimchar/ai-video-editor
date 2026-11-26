"""story-join: one video from several games' story.mp4 with a card between."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_video_editor.config import settings
from ai_video_editor.story import join
from ai_video_editor.story.render import StoryRenderError

R1 = "2026-11-23_2056_zeri_8-7-12"
R2 = "2026-11-24_1010_jhin_3-1-9"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", tmp_path / "ws")
    return tmp_path / "ws"


def _story(ws: Path, name: str) -> Path:
    p = ws / "story" / name / "story.mp4"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


@pytest.fixture
def fakes(monkeypatch):
    calls = {"cards": [], "concat": []}

    def card(title, out_path):
        calls["cards"].append(title)
        Path(out_path).write_bytes(b"c")

    def concat(parts, out):
        calls["concat"].append((list(parts), out))
        return True, None

    monkeypatch.setattr(join, "_render_card", card)
    monkeypatch.setattr(join, "_concat", concat)
    return calls


def test_round_label():
    assert join.round_label(R1) == "Zeri 8/7/12"
    assert join.round_label("weird_name") == "weird_name"


def test_join_order_and_titles(ws, fakes, tmp_path):
    s1, s2 = _story(ws, R1), _story(ws, R2)
    out = tmp_path / "o.mp4"
    assert join.join_stories([R1, R2], out) == out
    assert fakes["cards"] == ["Game 1 · Zeri 8/7/12", "Game 2 · Jhin 3/1/9"]
    parts, dest = fakes["concat"][0]
    assert dest == out.as_posix()
    assert len(parts) == 4
    assert Path(parts[1]) == s1 and Path(parts[3]) == s2
    assert Path(parts[0]).name != "story.mp4"


def test_missing_story_names_it(ws, fakes, tmp_path):
    _story(ws, R1)
    with pytest.raises(StoryRenderError, match=R2):
        join.join_stories([R1, R2], tmp_path / "o.mp4")


def test_no_cards(ws, fakes, tmp_path):
    s1, s2 = _story(ws, R1), _story(ws, R2)
    join.join_stories([R1, R2], tmp_path / "o.mp4", cards=False)
    assert fakes["cards"] == []
    assert [Path(p) for p in fakes["concat"][0][0]] == [s1, s2]


def test_cli_default_out(ws, fakes):
    _story(ws, R1)
    _story(ws, R2)
    assert join.main([R1, R2]) == 0
    expected = ws / "story" / "joined_2026-11-23_zeri-jhin.mp4"
    assert fakes["concat"][0][1] == expected.as_posix()


def test_cli_needs_two_rounds(ws, fakes):
    with pytest.raises(SystemExit):
        join.main([R1])


def test_cli_no_cards_and_out(ws, fakes, tmp_path):
    _story(ws, R1)
    _story(ws, R2)
    o = tmp_path / "x.mp4"
    assert join.main([R1, R2, "--no-cards", "--out", str(o)]) == 0
    assert fakes["cards"] == [] and fakes["concat"][0][1] == o.as_posix()
