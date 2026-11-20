"""finish_job: the single job-completion helper that also emits job.done."""
from __future__ import annotations

from pathlib import Path

import aiosqlite

from ai_video_editor import jobs


async def _db():
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT, output_path TEXT, "
        "error TEXT, completed_at TEXT)"
    )
    await db.execute("INSERT INTO jobs (id, status) VALUES ('j1', 'running')")
    await db.commit()
    return db


def _capture(monkeypatch):
    events = []
    monkeypatch.setattr(jobs, "emit", events.append)
    return events


async def test_finish_job_updates_row_and_emits(monkeypatch):
    events = _capture(monkeypatch)
    db = await _db()
    await jobs.finish_job(
        db, "j1", output_path="3/5 -> out", job_type="highlights",
        result={"clips_written": 3, "clips_total": 5, "folder": "out"},
    )
    row = (await db.execute_fetchall("SELECT status, output_path, completed_at FROM jobs"))[0]
    assert row[0] == "completed" and row[1] == "3/5 -> out" and row[2]
    assert len(events) == 1
    ev = events[0]
    assert ev.event == "job.done"
    assert ev.payload["job_id"] == "j1"
    assert ev.payload["job_type"] == "highlights"
    assert ev.payload["status"] == "completed"
    assert ev.payload["summary"]["clips_written"] == 3
    await db.close()


async def test_finish_job_error_path_emits_failed(monkeypatch):
    events = _capture(monkeypatch)
    db = await _db()
    await jobs.finish_job(db, "j1", error="boom")
    row = (await db.execute_fetchall("SELECT status, error FROM jobs"))[0]
    assert tuple(row) == ("failed", "boom")
    assert events[0].payload["status"] == "failed"
    assert events[0].payload["error"] == "boom"
    assert events[0].payload["summary"] == {}
    await db.close()


def test_summarize_strips_free_text():
    out = jobs.summarize({
        "folder": "f", "shorts_written": 1, "reason": "secret", "vo_prompt": "p",
        "transcript": ["x"],
        "shorts": [{"file": "a.mp4", "bucket": "b", "title": "t",
                    "vlm_verdict": "ok", "reason": "r"}],
        "clips": [{"file": "c.mp4", "event": "kill", "start_seconds": 1,
                   "end_seconds": 2, "vlm_verdict": "ok", "reason": "r"}],
    })
    assert "reason" not in out and "vo_prompt" not in out and "transcript" not in out
    assert out["shorts"] == [{"file": "a.mp4", "bucket": "b", "title": "t", "vlm_verdict": "ok"}]
    assert set(out["clips"][0]) == {
        "file", "event", "start_seconds", "end_seconds", "vlm_verdict"
    }
    assert jobs.summarize(None) == {}


async def test_finish_job_survives_db_failure(monkeypatch):
    events = _capture(monkeypatch)

    class BadDb:
        async def execute(self, *a, **k):
            raise RuntimeError("disk full")

        async def commit(self):
            raise RuntimeError("disk full")

    await jobs.finish_job(BadDb(), "j1", output_path="x")
    assert len(events) == 1 and events[0].payload["status"] == "completed"


def test_no_router_defines_a_private_finish_helper():
    routers = Path(jobs.__file__).parent / "routers"
    for p in routers.glob("*.py"):
        src = p.read_text(encoding="utf-8")
        assert "async def _finish" not in src, p.name
