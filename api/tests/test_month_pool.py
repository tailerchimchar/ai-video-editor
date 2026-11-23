from __future__ import annotations

import json

import pytest
from _play_factory import make_play

from ai_video_editor.compile_cleanup import is_safe_compilation_folder
from ai_video_editor.config import settings
from ai_video_editor.montage import month_pool


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    calls = []

    def fake_trim(src, out, start, end, **_):
        calls.append((src, out, start, end))
        open(out, "wb").close()
        return True, None

    monkeypatch.setattr(month_pool, "trim_clip", fake_trim)
    return calls


def test_pool_writes_padded_clip_and_index(ws):
    play = make_play("p1", approach=95.0, outcome=120.0)
    play.impressive.rank = 1
    out = month_pool.pool_play("asset1", play, "src.mp4")
    assert out is not None and out.name == "asset1_p1.mp4"
    assert ws[0][2:] == (85.0, 130.0)
    idx = json.loads((out.parent / "index.json").read_text())
    assert idx == [
        {
            "asset_id": "asset1",
            "play_id": "p1",
            "type": "teamfight",
            "rank": 1,
            "start_s": 85.0,
            "end_s": 130.0,
            "file": "asset1_p1.mp4",
        }
    ]


def test_pool_skips_existing_and_uncertain(ws):
    play = make_play("p1")
    month_pool.pool_play("a", play, "src.mp4")
    month_pool.pool_play("a", play, "src.mp4")
    assert len(ws) == 1
    assert month_pool.pool_play("a", make_play("p2", flags=["offset_uncertain"]), "s") is None
    assert month_pool.pool_play("a", make_play("p3", type="trade"), "s") is None
    assert month_pool.pool_play("a", make_play("p4", type="skirmish", enemy_deaths=2), "s") is None
    assert month_pool.pool_play("a", make_play("p5", type="skirmish", enemy_deaths=3), "s")
    assert len(ws) == 2


def test_pool_dir_is_not_a_compilation_folder(ws):
    assert not is_safe_compilation_folder(month_pool.pool_dir())
    assert not is_safe_compilation_folder(month_pool.pool_dir("2026-11"))


def test_clear_pool(ws):
    month_pool.pool_play("a", make_play("p1"), "src.mp4")
    month_pool.clear_pool()
    assert not month_pool.pool_dir().exists()


def test_absent_plays_are_not_pooled(ws):
    assert not month_pool.is_eligible(make_play("p1", involved=False))
    assert month_pool.pool_play("a", make_play("p1", involved=False), "src.mp4") is None
    assert ws == []


def test_pool_month_folder_and_temp_rename(ws):
    out = month_pool.pool_play("a", make_play("p1"), "src.mp4", month="2026-10")
    assert out == month_pool.pool_dir("2026-10") / "a_p1.mp4" and out.is_file()
    assert ws[0][1].endswith("a_p1.part.mp4")
    assert not list(out.parent.glob("*.part.mp4"))


def test_failed_cut_leaves_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)

    def half_written(src, out, start, end, **_):
        open(out, "wb").close()
        return False, "ffmpeg died"

    monkeypatch.setattr(month_pool, "trim_clip", half_written)
    assert month_pool.pool_play("a", make_play("p1"), "src.mp4", month="2026-10") is None
    assert list(month_pool.pool_dir("2026-10").glob("*.mp4")) == []
    assert not (month_pool.pool_dir("2026-10") / "index.json").exists()


def test_pool_game_pools_only_eligible(ws):
    plays = [make_play("p1"), make_play("p2", involved=False), make_play("p3", type="trade")]
    outs = month_pool.pool_game("a", plays, "src.mp4", month="2026-09")
    assert [o.name for o in outs] == ["a_p1.mp4"]
