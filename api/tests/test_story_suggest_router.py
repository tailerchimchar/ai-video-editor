"""Editor-suggestion endpoints under /api/v1/story/{round}/suggestions."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ai_video_editor.config import settings
from ai_video_editor.routers import story as story_router
from ai_video_editor.story.suggest.models import SuggestionItem, SuggestionSet
from ai_video_editor.story.suggest.store import FileSuggestionStore


@pytest.fixture
def rnd(sandbox):
    (Path(settings.workspace_dir) / "panel" / "r1").mkdir(parents=True)
    return "r1"


def _story_dir() -> Path:
    return Path(settings.workspace_dir) / "story" / "r1"


def _write_suggestions(status="ready", accepted=None):
    item = SuggestionItem(
        id="s-c1-slowmo",
        kind="slowmo",
        chapter_index=0,
        start_s=1000.0,
        end_s=1002.5,
        params={"factor": 0.5},
        reason="4v5 won",
        backend="rules",
        accepted=accepted,
    )
    FileSuggestionStore(_story_dir()).save(
        SuggestionSet(round="r1", created_at="t", status=status, items=[item])
    )


def _write_story_json(asset_id="a1"):
    d = _story_dir()
    d.mkdir(parents=True, exist_ok=True)
    story = {
        "asset_id": asset_id,
        "chapters": [],
        "total_s": 0,
        "mic_stream": None,
        "audio": "mixed",
    }
    (d / "story.json").write_text(json.dumps(story), encoding="utf-8")


def _wait(client, job_id):
    for _ in range(100):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in ("completed", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_get_404_then_the_set(client, rnd):
    assert client.get("/api/v1/story/r1/suggestions").status_code == 404
    _write_suggestions()
    r = client.get("/api/v1/story/r1/suggestions")
    assert r.status_code == 200
    assert r.json()["status"] == "ready" and r.json()["items"][0]["id"] == "s-c1-slowmo"


def test_get_unknown_round_404(client, rnd):
    assert client.get("/api/v1/story/nope/suggestions").status_code == 404


def test_post_runs_an_editor_suggest_job(client, rnd, monkeypatch):
    _write_story_json()
    seen = {}

    def fake(round_name, story_dir, **kw):
        seen.update(round=round_name, dir=story_dir)
        pending = json.loads((story_dir / "suggestions.json").read_text("utf-8"))
        seen["status_during_run"] = pending["status"]
        out = SuggestionSet(round=round_name, created_at="t", status="ready", items=[])
        FileSuggestionStore(story_dir).save(out)
        return out

    monkeypatch.setattr(story_router, "run_suggestions", fake)
    r = client.post("/api/v1/story/r1/suggestions")
    assert r.status_code == 200
    job = _wait(client, r.json()["job_id"])
    assert job["status"] == "completed"
    assert job["type"] == "editor_suggest"
    assert seen == {"round": "r1", "dir": _story_dir(), "status_during_run": "pending"}
    assert client.get("/api/v1/story/r1/suggestions").json()["status"] == "ready"


def test_post_failure_marks_the_set_failed(client, rnd, monkeypatch):
    _write_story_json()

    def boom(*a, **k):
        raise RuntimeError("no usable Riot timeline")

    monkeypatch.setattr(story_router, "run_suggestions", boom)
    job = _wait(client, client.post("/api/v1/story/r1/suggestions").json()["job_id"])
    assert job["status"] == "failed" and "timeline" in job["error"]
    body = client.get("/api/v1/story/r1/suggestions").json()
    assert body["status"] == "failed" and "timeline" in body["error"]


def test_post_needs_a_rendered_story(client, rnd):
    assert client.post("/api/v1/story/r1/suggestions").status_code == 409


def test_put_accepts_and_404s(client, rnd):
    _write_suggestions()
    url = "/api/v1/story/r1/suggestions/s-c1-slowmo"
    r = client.put(url, json={"accepted": True})
    assert r.status_code == 200 and r.json()["items"][0]["accepted"] is True
    assert client.put(url, json={"accepted": None}).json()["items"][0]["accepted"] is None
    bad = client.put("/api/v1/story/r1/suggestions/nope", json={"accepted": True})
    assert bad.status_code == 404


def test_apply_writes_requests_and_starts_a_render(client, rnd, monkeypatch):
    _write_story_json("asset-9")
    _write_suggestions(accepted=True)
    seen = {}

    def fake(asset_id, round, **kw):
        seen.update(asset_id=asset_id, round=round, **kw)
        return Path("story.mp4")

    monkeypatch.setattr(story_router, "render_round", fake)
    r = client.post("/api/v1/story/r1/suggestions/apply")
    assert r.status_code == 200
    job = _wait(client, r.json()["job_id"])
    assert job["status"] == "completed" and job["type"] == "story_render"
    assert seen == {"asset_id": "asset-9", "round": "r1", "plays": True}
    saved = json.loads((_story_dir() / "requests.json").read_text(encoding="utf-8"))
    assert [q["effect"] for q in saved["requests"]] == ["slowmo"]
    assert client.get("/api/v1/story/r1/suggestions").json()["status"] == "applied"


def test_apply_without_suggestions_404(client, rnd):
    _write_story_json()
    assert client.post("/api/v1/story/r1/suggestions/apply").status_code == 404


def test_apply_without_a_story_asset_409(client, rnd):
    _write_suggestions(accepted=True)
    assert client.post("/api/v1/story/r1/suggestions/apply").status_code == 409
