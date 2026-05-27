"""Feedback capture + summarisation.

In-memory aiosqlite db to test the schema + helpers without touching the
real workspace database. The helpers are intentionally best-effort
(non-raising on partial failure) so error paths are covered too.
"""

import json

import aiosqlite
import pytest
import pytest_asyncio

from ai_video_editor.feedback import log_event, summarise

# Minimal schema just for this test — full _SCHEMA pulls in sqlite-vec
# virtual tables that need the extension loaded. We only exercise
# feedback_events here.
_TEST_SCHEMA = """
CREATE TABLE feedback_events (
    id TEXT PRIMARY KEY,
    compilation_id TEXT NOT NULL,
    clip_id TEXT,
    action TEXT NOT NULL,
    event_type TEXT,
    delta_before REAL,
    delta_after REAL,
    payload TEXT,
    created_at TEXT NOT NULL
);
"""


@pytest_asyncio.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(_TEST_SCHEMA)
    await conn.commit()
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_log_event_persists_full_row(db):
    await log_event(
        db,
        compilation_id="comp-1",
        clip_id="clip-a",
        action="extend",
        event_type="funny_audio",
        delta_before=2.0,
        delta_after=3.5,
        payload={"clip_ref": "01"},
    )
    rows = await db.execute_fetchall("SELECT * FROM feedback_events")
    assert len(rows) == 1
    r = dict(rows[0])
    assert r["compilation_id"] == "comp-1"
    assert r["clip_id"] == "clip-a"
    assert r["action"] == "extend"
    assert r["event_type"] == "funny_audio"
    assert r["delta_before"] == 2.0
    assert r["delta_after"] == 3.5
    assert json.loads(r["payload"]) == {"clip_ref": "01"}


@pytest.mark.asyncio
async def test_log_event_swallows_db_errors(db):
    """A failing log MUST NOT raise — the edit already succeeded by then."""
    await db.close()  # Force any insert to error out.
    # No exception should escape.
    await log_event(db, compilation_id="comp-1", action="extend")


@pytest.mark.asyncio
async def test_summary_aggregates_actions_and_event_types(db):
    await log_event(db, compilation_id="c1", action="extend", event_type="funny_audio",
                    delta_before=2.0, delta_after=3.0)
    await log_event(db, compilation_id="c1", action="extend", event_type="funny_audio",
                    delta_before=4.0, delta_after=2.0)
    await log_event(db, compilation_id="c1", action="remove_clip", event_type="kill")
    await log_event(db, compilation_id="c2", action="extend", event_type="kill",
                    delta_before=0.0, delta_after=5.0)
    out = await summarise(db)
    assert out["total_events"] == 4
    assert out["by_action"] == {"extend": 3, "remove_clip": 1}
    assert out["by_event_type"] == {"funny_audio": 2, "kill": 2}


@pytest.mark.asyncio
async def test_summary_computes_extend_medians_per_event_type(db):
    # 3 funny_audio extends → median should be the middle value
    for before, after in [(1.0, 5.0), (3.0, 7.0), (5.0, 9.0)]:
        await log_event(db, compilation_id="c1", action="extend",
                        event_type="funny_audio",
                        delta_before=before, delta_after=after)
    out = await summarise(db)
    medians = out["extend_medians_per_event_type"]
    assert medians["funny_audio"] == {"before": 3.0, "after": 7.0, "n_samples": 3}
    # 3+ samples → eligible for proposed override
    assert out["proposed_event_window_overrides"]["funny_audio"] == [3.0, 7.0]


@pytest.mark.asyncio
async def test_summary_skips_proposed_overrides_below_3_samples(db):
    """One-shot edits shouldn't reshape the global defaults — require ≥3."""
    await log_event(db, compilation_id="c1", action="extend", event_type="kill",
                    delta_before=99.0, delta_after=99.0)
    out = await summarise(db)
    # Still appears in medians for inspection...
    assert out["extend_medians_per_event_type"]["kill"]["n_samples"] == 1
    # ...but is NOT in the auto-apply proposal.
    assert "kill" not in out["proposed_event_window_overrides"]


@pytest.mark.asyncio
async def test_summary_scopes_to_compilation_when_passed(db):
    await log_event(db, compilation_id="c1", action="extend", event_type="kill",
                    delta_before=1.0, delta_after=2.0)
    await log_event(db, compilation_id="c2", action="extend", event_type="kill",
                    delta_before=10.0, delta_after=20.0)
    only_c1 = await summarise(db, compilation_id="c1")
    assert only_c1["total_events"] == 1
    assert only_c1["extend_medians_per_event_type"]["kill"]["before"] == 1.0
