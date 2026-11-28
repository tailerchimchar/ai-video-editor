"""Target registry + StoryRoundTarget (AVE-69)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar

import pytest

from ai_video_editor.config import settings
from ai_video_editor.narration import targets
from ai_video_editor.narration.targets import (
    NarrationTarget,
    StoryRoundTarget,
    UnknownTargetError,
    fingerprint,
    get_target,
    register_target,
)


@pytest.fixture
def ws(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    return tmp_path


@pytest.fixture
def clean_registry(monkeypatch):
    """Isolate registry mutations from other tests."""
    monkeypatch.setattr(targets, "_REGISTRY", dict(targets._REGISTRY))


class FakeTarget:
    kind: ClassVar[str] = "fake"

    def video_path(self, key: str) -> Path:
        return Path("x.mp4")

    def work_dir(self, key: str) -> Path:
        return Path("w")

    def game(self, key: str) -> str:
        return "valorant"

    def video_url(self, key: str) -> str:
        return f"/fake/{key}.mp4"

    def list_keys(self) -> list[str]:
        return []

    def intro_s(self, key: str) -> float | None:
        return None

    def markers(self, key: str) -> list:
        return []

    def join_map(self, key: str):
        return None


def test_story_registered_by_default():
    assert get_target("story").kind == "story"


def test_register_and_get_fake_kind(clean_registry):
    register_target(FakeTarget())
    t = get_target("fake")
    assert isinstance(t, NarrationTarget)
    assert t.game("k") == "valorant"


def test_duplicate_kind_rejected(clean_registry):
    register_target(FakeTarget())
    with pytest.raises(ValueError):
        register_target(FakeTarget())


def test_unknown_kind():
    with pytest.raises(UnknownTargetError):
        get_target("nope")


def test_story_paths_and_url(ws):
    d = ws / "story" / "r1"
    d.mkdir(parents=True)
    (d / "story.mp4").write_bytes(b"x")
    t = StoryRoundTarget()
    assert t.video_path("r1") == d / "story.mp4"
    assert t.work_dir("r1") == d
    assert t.game("r1") == "league"
    assert t.video_url("r1") == "/workspace/story/r1/story.mp4"


def test_story_missing_video(ws):
    with pytest.raises(UnknownTargetError):
        StoryRoundTarget().video_path("ghost")


@pytest.mark.parametrize("key", ["..", "../x", "a/b", "a\\b", "", "a b"])
def test_story_bad_key(ws, key):
    t = StoryRoundTarget()
    for fn in (t.video_path, t.work_dir, t.video_url):
        with pytest.raises(UnknownTargetError):
            fn(key)


def test_fingerprint_changes_on_rewrite(tmp_path):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"aaaa")
    a = fingerprint(f)
    assert a == f"{f.stat().st_size}-{f.stat().st_mtime_ns}"
    f.write_bytes(b"bbbbbb")
    os.utime(f, ns=(1, 1))
    assert fingerprint(f) != a


def test_story_list_keys(ws):
    story = ws / "story"
    for name, has_video in [
        ("2026-11-20_2001_jhin_8-2-13", True),
        ("2026-11-23_2056_zeri_8-7-12", True),
        ("_demo", True),
        ("2026-11-25_0900_novideo_1-1-1", False),
    ]:
        (story / name).mkdir(parents=True)
        if has_video:
            (story / name / "story.mp4").write_bytes(b"x")
    (story / "stray.txt").write_text("x")
    keys = StoryRoundTarget().list_keys()
    assert keys == ["2026-11-23_2056_zeri_8-7-12", "2026-11-20_2001_jhin_8-2-13"]


def test_story_list_keys_no_story_folder(ws):
    assert StoryRoundTarget().list_keys() == []


def test_registered_targets_lists_registry(clean_registry):
    register_target(FakeTarget())
    assert "fake" in [t.kind for t in targets.registered_targets()]


def test_joined_target_keys_paths_and_backups(ws):
    from ai_video_editor.narration.targets import JoinedStoryTarget

    story = ws / "story"
    story.mkdir()
    for name in [
        "joined_2026-11-26_xayah-kaisa-yunara.mp4",
        "joined_2026-11-26_xayah-kaisa-yunara.v1.mp4",  # backup: not a key
        "joined_2026-11-20_jhin-zeri.mp4",
    ]:
        (story / name).write_bytes(b"x")
    t = get_target("joined")
    assert isinstance(t, JoinedStoryTarget)
    key = "joined_2026-11-26_xayah-kaisa-yunara"
    assert t.list_keys() == [key, "joined_2026-11-20_jhin-zeri"]
    assert t.video_path(key) == story / f"{key}.mp4"
    assert t.work_dir(key) == story / "_joined" / key
    assert t.video_url(key) == f"/workspace/story/{key}.mp4"
    with pytest.raises(UnknownTargetError):
        t.video_path("joined_missing")
    with pytest.raises(UnknownTargetError):
        t.video_path("../etc")


def test_joined_intro_and_markers_from_join_json(ws):
    import json

    story = ws / "story"
    story.mkdir()
    key = "joined_2026-11-26_x"
    (story / f"{key}.mp4").write_bytes(b"x")
    t = get_target("joined")
    assert t.intro_s(key) is None and t.markers(key) == []
    chapter = {
        "title": "Lane, first blood",
        "kind": "lane",
        "video_start_s": 20.0,
        "game_start_s": 0,
        "game_end_s": 30,
        "kills": 0,
        "deaths": 0,
    }
    game = {
        "round": "r",
        "label": "Xayah 5/0/2",
        "video_start_s": 13.8,
        "story_start_s": 16.0,
        "video_end_s": 90.0,
        "story_json": "s",
        "digest_json": None,
        "chapters": [chapter],
    }
    jm = {
        "video": "v",
        "title": "t",
        "duration_s": 100.0,
        "intro_s": 13.8,
        "games": [game],
        "inserts": [{"kind": "subscribe", "at_s": 30.0, "dur_s": 3.0, "asset": "a"}],
    }
    (story / f"{key}.join.json").write_text(json.dumps(jm), encoding="utf-8")
    assert t.intro_s(key) == 13.8
    got = [(m.at_s, m.label, m.kind) for m in t.markers(key)]
    assert got == [
        (13.8, "Xayah 5/0/2", "game"),
        (20.0, "Lane", "chapter"),
        (30.0, "subscribe", "insert"),
    ]


def test_story_round_markers_from_story_json(ws):
    import json

    d = ws / "story" / "r1"
    d.mkdir(parents=True)
    (d / "story.mp4").write_bytes(b"x")
    t = StoryRoundTarget()
    assert t.markers("r1") == [] and t.intro_s("r1") is None
    plan = {
        "chapters": [
            {"title": "Fight, 2 kills", "start_s": 100.0, "end_s": 110.0, "kind": "fight"},
            {"title": "Close", "start_s": 200.0, "end_s": 205.0, "kind": "close"},
        ],
        "total_s": 15.0,
        "mic_stream": None,
        "audio": "mixed",
    }
    (d / "story.json").write_text(json.dumps(plan), encoding="utf-8")
    marks = t.markers("r1")
    assert [(m.label, m.kind) for m in marks] == [("Fight", "chapter"), ("Close", "chapter")]
    assert marks[0].at_s == 2.5 and marks[1].at_s == 15.0
