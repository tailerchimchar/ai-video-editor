"""Videos hub catalog (narration/catalog.py) and GET /api/v1/videos."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_video_editor.config import settings
from ai_video_editor.narration import catalog, targets
from ai_video_editor.narration.models import TargetRef

KEY = "2026-11-20_2001_jhin_8-2-13"


def test_title_from_key():
    assert catalog.title_from_key(KEY) == "Jhin 8/2/13"
    assert catalog.title_from_key("2026-11-23_2056_zeri_8-7-12") == "Zeri 8/7/12"


@pytest.mark.parametrize("key", ["weird", "2026-11-20_jhin", "r1"])
def test_title_falls_back_to_key(key):
    assert catalog.title_from_key(key) == key


def test_date_from_key():
    assert catalog.date_from_key(KEY) == "2026-11-20"
    assert catalog.date_from_key("2026-13-45_x") is None
    assert catalog.date_from_key("r1") is None


def test_suggestion_status(tmp_path):
    assert catalog.suggestion_status(tmp_path) == "none"
    f = tmp_path / "suggestions.json"
    f.write_text("{not json")
    assert catalog.suggestion_status(tmp_path) == "none"
    f.write_text(json.dumps({"status": "ready", "items": []}))
    assert catalog.suggestion_status(tmp_path) == "ready"
    f.write_text(json.dumps({"status": "bogus"}))
    assert catalog.suggestion_status(tmp_path) == "none"
    f.write_text(json.dumps([1, 2]))
    assert catalog.suggestion_status(tmp_path) == "none"


@pytest.fixture
def story_ws(sandbox, monkeypatch):
    root = Path(settings.workspace_dir) / "story"
    d = root / KEY
    d.mkdir(parents=True)
    (d / "story.mp4").write_bytes(b"v")
    (d / "suggestions.json").write_text(json.dumps({"status": "ready"}))
    catalog._DURATIONS.clear()
    monkeypatch.setattr(catalog, "probe_duration", lambda p: 83.5)
    return root


def _story(videos):
    return [v for v in videos if v.kind == "story"]


def test_summary_for_story_round(story_ws):
    (v,) = _story(catalog.list_videos())
    assert v.key == KEY
    assert v.title == "Jhin 8/2/13"
    assert v.game == "league"
    assert v.date == "2026-11-20"
    assert v.duration_s == 83.5
    assert v.video_url == f"/workspace/story/{KEY}/story.mp4"
    assert (v.take_count, v.stale, v.exported) == (0, False, False)
    assert v.suggestion_status == "ready"


def test_duration_cached_by_fingerprint(story_ws, monkeypatch):
    calls = []
    monkeypatch.setattr(catalog, "probe_duration", lambda p: calls.append(p) or 10.0)
    catalog.list_videos()
    catalog.list_videos()
    assert len(calls) == 1
    (story_ws / KEY / "story.mp4").write_bytes(b"longer content")
    catalog.list_videos()
    assert len(calls) == 2


def test_duration_none_when_probe_fails(story_ws, monkeypatch):
    monkeypatch.setattr(catalog, "probe_duration", lambda p: None)
    assert _story(catalog.list_videos())[0].duration_s is None


def test_takes_stale_exported(story_ws):
    from ai_video_editor.narration import service
    from ai_video_editor.narration.store import store_for

    target = targets.get_target("story")
    store = store_for(target)
    ref = TargetRef(kind="story", key=KEY)
    service.add_take(
        ref, target, store, data=b"a", suffix=".webm", start_s=0, duration_s=1, has_camera=False
    )
    store.export_path(ref).parent.mkdir(parents=True, exist_ok=True)
    store.export_path(ref).write_bytes(b"x")
    (story_ws / KEY / "story.mp4").write_bytes(b"re-rendered")
    (v,) = _story(catalog.list_videos())
    assert (v.take_count, v.stale, v.exported) == (1, True, True)


class FakeKind:
    kind = "fake"

    def __init__(self, root: Path) -> None:
        self.root = root

    def work_dir(self, key):
        return self.root / key

    def video_path(self, key):
        return self.work_dir(key) / "v.mp4"

    def game(self, key):
        return "valorant"

    def video_url(self, key):
        return f"/workspace/fake/{key}/v.mp4"

    def list_keys(self):
        return ["f1"]


def test_videos_endpoint_loops_registry(client, story_ws, sandbox, monkeypatch):
    (sandbox / "fake" / "f1").mkdir(parents=True)
    (sandbox / "fake" / "f1" / "v.mp4").write_bytes(b"x")
    monkeypatch.setitem(targets._REGISTRY, "fake", FakeKind(sandbox / "fake"))
    body = client.get("/api/v1/videos").json()
    assert {v["kind"] for v in body} == {"story", "fake"}
    f = next(v for v in body if v["kind"] == "fake")
    assert f["title"] == "f1" and f["game"] == "valorant" and f["date"] is None
    assert f["suggestion_status"] == "none"
    assert set(f) == {
        "kind",
        "key",
        "title",
        "game",
        "date",
        "duration_s",
        "video_url",
        "take_count",
        "stale",
        "exported",
        "suggestion_status",
    }
