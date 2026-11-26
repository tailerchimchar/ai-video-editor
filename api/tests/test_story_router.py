"""Story requests + render job endpoints (api/routers/story.py)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ai_video_editor.config import settings
from ai_video_editor.routers import story as story_router
from ai_video_editor.story.cli import StoryInputError


@pytest.fixture
def rnd(sandbox):
    """A panel round folder `r1` under the temp workspace."""
    d = Path(settings.workspace_dir) / "panel" / "r1"
    d.mkdir(parents=True)
    return "r1"


def _requests_file(name="r1") -> Path:
    return Path(settings.workspace_dir) / "story" / name / "requests.json"


def test_list_empty(client, rnd):
    r = client.get("/api/v1/story/r1/requests")
    assert r.status_code == 200
    assert r.json() == {"round": "r1", "requests": []}


def test_add_validates_appends_and_indexes(client, rnd):
    r = client.post("/api/v1/story/r1/requests", json={"kind": "add_chapter", "at": "20:05"})
    assert r.status_code == 200
    body = r.json()
    assert [x["index"] for x in body["requests"]] == [0]
    assert body["requests"][0]["kind"] == "add_chapter"
    r = client.post(
        "/api/v1/story/r1/requests", json={"kind": "effect", "at": 1500, "effect": "slowmo"}
    )
    assert [x["index"] for x in r.json()["requests"]] == [0, 1]
    saved = json.loads(_requests_file().read_text(encoding="utf-8"))
    assert [q["kind"] for q in saved["requests"]] == ["add_chapter", "effect"]
    assert client.get("/api/v1/story/r1/requests").json() == r.json()


def test_add_accepts_trim_and_mute(client, rnd):
    r = client.post(
        "/api/v1/story/r1/requests", json={"kind": "trim", "at": "31:10", "start": "31:20"}
    )
    assert r.status_code == 200
    r = client.post(
        "/api/v1/story/r1/requests",
        json={"kind": "mute", "at": "12:10", "start": "12:00", "end": "12:30"},
    )
    assert [x["kind"] for x in r.json()["requests"]] == ["trim", "mute"]
    saved = json.loads(_requests_file().read_text(encoding="utf-8"))
    assert [q["kind"] for q in saved["requests"]] == ["trim", "mute"]
    bad = client.post(
        "/api/v1/story/r1/requests", json={"kind": "mute", "at": "12:10", "end": "x:y"}
    )
    assert bad.status_code == 422


def test_add_invalid_is_422_and_writes_nothing(client, rnd):
    bad = client.post("/api/v1/story/r1/requests", json={"kind": "add_chapter", "at": "99:99:99"})
    assert bad.status_code == 422
    assert client.post("/api/v1/story/r1/requests", json={"kind": "nope"}).status_code == 422
    assert not _requests_file().exists()


def test_remove_by_index(client, rnd):
    for at in ("1:00", "2:00", "3:00"):
        client.post("/api/v1/story/r1/requests", json={"kind": "drop", "at": at})
    r = client.delete("/api/v1/story/r1/requests/1")
    assert r.status_code == 200
    assert [(x["index"], x["at"]) for x in r.json()["requests"]] == [(0, "1:00"), (1, "3:00")]
    assert client.delete("/api/v1/story/r1/requests/5").status_code == 404
    assert client.delete("/api/v1/story/r1/requests/-1").status_code == 404


def test_unknown_and_invalid_round(client, rnd):
    assert client.get("/api/v1/story/nope/requests").status_code == 404
    drop = {"kind": "drop", "at": 1}
    assert client.post("/api/v1/story/nope/requests", json=drop).status_code == 404
    assert client.get("/api/v1/story/bad.name/requests").status_code == 422


def test_round_known_from_story_dir_only(client, sandbox):
    (Path(settings.workspace_dir) / "story" / "r2").mkdir(parents=True)
    assert client.get("/api/v1/story/r2/requests").status_code == 200


def test_corrupt_requests_file_is_422(client, rnd):
    f = _requests_file()
    f.parent.mkdir(parents=True)
    f.write_text("{not json", encoding="utf-8")
    assert client.get("/api/v1/story/r1/requests").status_code == 422


def _wait(client, job_id):
    for _ in range(100):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in ("completed", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_render_creates_job_running_render_round(client, rnd, monkeypatch):
    seen = {}

    def fake(asset_id, round, **kw):
        seen.update(asset_id=asset_id, round=round, **kw)
        return Path("story.mp4")

    monkeypatch.setattr(story_router, "render_round", fake)
    r = client.post("/api/v1/story/r1/render", json={"asset_id": "a1"})
    assert r.status_code == 200
    job = _wait(client, r.json()["job_id"])
    assert job["status"] == "completed"
    assert job["output_path"] == "story.mp4"
    assert seen["asset_id"] == "a1" and seen["round"] == "r1" and seen["plays"] is True


def test_render_failure_marks_job_failed(client, rnd, monkeypatch):
    def boom(*a, **k):
        raise StoryInputError("no usable Riot timeline cached for asset a1")

    monkeypatch.setattr(story_router, "render_round", boom)
    r = client.post("/api/v1/story/r1/render", json={"asset_id": "a1"})
    job = _wait(client, r.json()["job_id"])
    assert job["status"] == "failed"
    assert "timeline" in job["error"]


def test_render_unknown_round_404(client, rnd):
    assert client.post("/api/v1/story/nope/render", json={"asset_id": "a1"}).status_code == 404
