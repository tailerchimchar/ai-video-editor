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
