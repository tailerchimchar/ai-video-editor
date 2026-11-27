"""Early-game vision-check cache (AVE-73): key, read/write, load_early wiring, flag."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ai_video_editor.config import settings
from ai_video_editor.league.early_game import EarlySegment
from ai_video_editor.league.riot_timeline import cache_dir_for
from ai_video_editor.story import cli
from ai_video_editor.story import early_cache as ec
from ai_video_editor.vlm import early_confirm


def _seg(start=28.5, end=112.5, kind="opening", confirmed=False) -> EarlySegment:
    return EarlySegment(kind=kind, start_s=start, end_s=end, reason="r", confirmed=confirmed)


def test_key_changes_with_every_input():
    base = ec.early_cache_key([_seg()], 200.0, "gemini:m", 0.0)
    assert base == ec.early_cache_key([_seg()], 200.0, "gemini:m", 0.0)
    assert base != ec.early_cache_key([_seg(end=99.0)], 200.0, "gemini:m", 0.0)
    assert base != ec.early_cache_key([_seg()], 201.0, "gemini:m", 0.0)
    assert base != ec.early_cache_key([_seg()], None, "gemini:m", 0.0)
    assert base != ec.early_cache_key([_seg()], 200.0, "other", 0.0)
    assert base != ec.early_cache_key([_seg()], 200.0, "gemini:m", 1.5)


def test_roundtrip_and_misses(tmp_path):
    path = tmp_path / "early_confirm.json"
    segs = [_seg(confirmed=True), _seg(130.0, 140.0, "level3_trade", True)]
    assert ec.read_early_cache(path, "k") is None  # no file
    ec.write_early_cache(path, "k", segs)
    assert ec.read_early_cache(path, "k") == segs
    assert ec.read_early_cache(path, "other") is None  # key changed


@pytest.mark.parametrize(
    "body", ["not json", "[]", '{"key": "k"}', '{"key":"k","segments":[{"x":1}]}']
)
def test_corrupt_cache_is_a_miss(tmp_path, body):
    path = tmp_path / "early_confirm.json"
    path.write_text(body, encoding="utf-8")
    assert ec.read_early_cache(path, "k") is None


# --- load_early wiring ---------------------------------------------------


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", tmp_path / "ws")
    monkeypatch.setattr(settings, "story_early_confirm", True)
    d = cache_dir_for("a1")
    d.mkdir(parents=True, exist_ok=True)
    (d / "timeline.json").write_text(json.dumps({"info": {"frames": []}}), encoding="utf-8")
    base = [_seg()]
    monkeypatch.setattr(cli, "early_segments", lambda tl, raw, **kw: list(base))
    monkeypatch.setattr(cli, "first_back_s", lambda *a, **k: 200.0)
    monkeypatch.setattr(cli, "_early_backend", lambda aid: SimpleNamespace(name="fake", model="m"))
    calls = []

    def fake_confirm(src, segments, **kw):
        calls.append(1)
        return [_seg(end=100.0 + len(calls), confirmed=True)]

    monkeypatch.setattr(early_confirm, "confirm_early", fake_confirm)
    tl = SimpleNamespace(owner_pid=1, offset_s=0.0)
    return SimpleNamespace(calls=calls, tl=tl, base=base, mp=monkeypatch)


def test_second_load_hits_cache(wired):
    first = cli.load_early("a1", wired.tl, "v.mp4")
    second = cli.load_early("a1", wired.tl, "v.mp4")
    assert len(wired.calls) == 1
    assert second == first
    assert (cache_dir_for("a1") / "early_confirm.json").is_file()


def test_changed_offset_or_segments_recall(wired):
    cli.load_early("a1", wired.tl, "v.mp4")
    cli.load_early("a1", SimpleNamespace(owner_pid=1, offset_s=2.0), "v.mp4")
    assert len(wired.calls) == 2
    wired.base[:] = [_seg(end=50.0)]
    cli.load_early("a1", SimpleNamespace(owner_pid=1, offset_s=2.0), "v.mp4")
    assert len(wired.calls) == 3


def test_fresh_vision_forces_call_and_refreshes(wired):
    cli.load_early("a1", wired.tl, "v.mp4")
    fresh = cli.load_early("a1", wired.tl, "v.mp4", fresh_vision=True)
    assert len(wired.calls) == 2
    assert cli.load_early("a1", wired.tl, "v.mp4") == fresh  # cache now holds refresh
    assert len(wired.calls) == 2


def test_corrupt_cache_file_recalls(wired):
    cli.load_early("a1", wired.tl, "v.mp4")
    (cache_dir_for("a1") / "early_confirm.json").write_text("{{{", encoding="utf-8")
    cli.load_early("a1", wired.tl, "v.mp4")
    assert len(wired.calls) == 2


def test_render_round_passes_fresh_vision(wired):
    import inspect

    assert inspect.signature(cli.render_round).parameters["fresh_vision"].default is False
    wired.mp.setattr(cli, "load_timeline", lambda aid: wired.tl)
    seen = {}

    def fake_load_early(aid, tl, src, *, fresh_vision=False):
        seen["f"] = fresh_vision
        raise RuntimeError("stop after load_early")

    wired.mp.setattr(cli, "load_early", fake_load_early)
    wired.mp.setattr(cli, "round_dir", lambda r: cache_dir_for("a1"))
    wired.mp.setattr(settings, "story_early_game", True)
    plays = settings.workspace_dir / "plays" / "a1.json"
    plays.parent.mkdir(parents=True, exist_ok=True)
    plays.write_text("{}", encoding="utf-8")
    wired.mp.setattr(cli.GameModel, "model_validate_json", lambda s: None)
    with pytest.raises(RuntimeError, match="stop after"):
        cli.render_round("a1", "r", plays=True, source="v.mp4", fresh_vision=True)
    assert seen == {"f": True}


def test_cli_flag_parsed(wired):
    got = {}
    wired.mp.setattr(cli, "render_round", lambda *a, **k: got.update(k))
    cli.main(["--asset", "a1", "--round", "r", "--fresh-vision"])
    assert got["fresh_vision"] is True
