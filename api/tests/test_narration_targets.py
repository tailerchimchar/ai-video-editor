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
