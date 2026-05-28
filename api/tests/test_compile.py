"""Phase 3 Milestone B — compile pipeline.

Unit-test the pure planner (`plan_clips`) and validate the endpoint
wiring with ffmpeg mocked. Real ffmpeg never runs in tests.
"""

import json
import uuid
from datetime import datetime, timezone

import pytest

from ai_video_editor.compile import (
    _segments_in_window,
    cluster_ranked_candidates,
    plan_clips,
    reorder_clips_explicit,
)

# ----- pure unit tests on the planner -----


def _r(start: float, hype: float, keep: bool = True) -> dict:
    return {
        "candidate_id": str(uuid.uuid4()),
        "keep": keep,
        "suggested_start_seconds": start,
        "suggested_end_seconds": start + 10.0,
        "hype_score": hype,
        "funny_score": 0.2,
        "story_score": 0.3,
        "reason": "test",
    }


def test_plan_clips_filters_unkept():
    rankings = [_r(10, 0.5, keep=True), _r(20, 0.9, keep=False), _r(30, 0.6, keep=True)]
    assert len(plan_clips(rankings)) == 2


def test_plan_clips_chronological_explicit():
    rankings = [_r(100, 0.5), _r(50, 0.9), _r(75, 0.6)]
    out = plan_clips(rankings, order="chronological")
    assert [r["suggested_start_seconds"] for r in out] == [50, 75, 100]


def test_plan_clips_hype_order_sorts_descending():
    rankings = [_r(100, 0.5), _r(50, 0.9), _r(75, 0.6)]
    out = plan_clips(rankings, order="hype")
    assert [r["hype_score"] for r in out] == [0.9, 0.6, 0.5]


def test_plan_clips_hook_is_default():
    """The default order is `hook` — algorithm-driven retention favors
    starting with the highest-hype clip. If this changes, the change
    is intentional and this test should be updated."""
    rankings = [_r(10, 0.3), _r(50, 0.9), _r(100, 0.5)]
    out = plan_clips(rankings)
    # Hook mode: hottest first
    assert out[0]["hype_score"] == 0.9


def test_plan_clips_hook_puts_hottest_first_then_chronological():
    """Hook mode: highest-hype clip leads, rest in source-time order.
    The non-hottest clips keep their natural sequence so the rest of
    the reel reads as a story, not a hype-descending mash."""
    rankings = [_r(10, 0.3), _r(50, 0.9), _r(100, 0.5), _r(200, 0.7)]
    out = plan_clips(rankings, order="hook")
    # Hottest (0.9 @ t=50) first
    assert out[0]["suggested_start_seconds"] == 50
    # Remaining clips in chronological order (10, 100, 200)
    assert [r["suggested_start_seconds"] for r in out[1:]] == [10, 100, 200]


def test_plan_clips_hook_handles_single_clip():
    """One-clip case: just that clip, no ordering anomalies."""
    rankings = [_r(50, 0.9)]
    out = plan_clips(rankings, order="hook")
    assert len(out) == 1
    assert out[0]["suggested_start_seconds"] == 50


def test_plan_clips_hook_with_empty_rankings():
    assert plan_clips([], order="hook") == []


def test_plan_clips_limit_truncates_after_order():
    rankings = [_r(t, 0.5) for t in (10, 20, 30, 40, 50)]
    assert len(plan_clips(rankings, limit=2)) == 2


# ----- narrative mode: intro / main / outro sections in time order -----


def test_plan_clips_narrative_three_sections():
    """Narrative mode: intro (first 600s) → main → outro (last 600s).
    Within each section, top-by-hype, then sorted by time. Concatenated
    in time order."""
    # Simulate a 6500-second recording (similar to a real scrim VOD).
    rankings = [
        _r(100, 0.5),    # intro candidate
        _r(300, 0.9),    # intro candidate (higher hype → kept)
        _r(550, 0.7),    # intro candidate
        _r(2000, 0.6),   # main
        _r(3000, 0.4),   # main
        _r(4000, 0.8),   # main
        _r(6000, 0.3),   # outro candidate (rec_end = 6010)
        _r(5950, 0.9),   # outro candidate (higher hype → kept)
    ]
    out = plan_clips(rankings, order="narrative")
    # intro_max=2 → top 2 by hype from intro = (0.9 @ 300) + (0.7 @ 550),
    # sorted by time → [300, 550]
    # main = [2000, 3000, 4000] chronological
    # outro_max=1 → top 1 by hype from outro = (0.9 @ 5950)
    # full order: [300, 550, 2000, 3000, 4000, 5950]
    starts = [r["suggested_start_seconds"] for r in out]
    assert starts == [300, 550, 2000, 3000, 4000, 5950]


def test_plan_clips_narrative_limit_preserves_intro_and_outro():
    """When limit is set, intro/outro slots are reserved and limit only
    truncates the main section. Structural sections always survive."""
    rankings = [
        _r(100, 0.9),    # intro
        _r(550, 0.5),    # intro (drops if cap=1; cap=2 keeps both)
        _r(2000, 0.6),   # main
        _r(3000, 0.5),   # main
        _r(4000, 0.4),   # main
        _r(5000, 0.3),   # main
        _r(6450, 0.8),   # outro (rec_end = 6460)
    ]
    out = plan_clips(rankings, order="narrative", limit=5)
    starts = [r["suggested_start_seconds"] for r in out]
    # intro (2) + outro (1) = 3 reserved; main_budget = 5 - 3 = 2
    # → 2 intro + 2 main + 1 outro = 5 clips
    assert len(out) == 5
    assert starts[0:2] == [100, 550]  # intro time-sorted
    assert starts[-1] == 6450  # outro at end
    # Main = first 2 by time in the middle bucket
    assert starts[2:4] == [2000, 3000]


def test_plan_clips_narrative_empty_intro_is_fine():
    """No intro candidates (all clips past first 600s): narrative still
    works — intro section empty, main chronological, outro picks top by
    hype from the end window."""
    # max_end = 10010 → outro_threshold = 9410 → outro contains the 10000 clip.
    # Other clips (2000, 2500, 3000) all in main (>600, <9410).
    rankings = [_r(2000, 0.5), _r(2500, 0.6), _r(3000, 0.5), _r(10000, 0.1)]
    out = plan_clips(rankings, order="narrative")
    starts = [r["suggested_start_seconds"] for r in out]
    assert starts == [2000, 2500, 3000, 10000]


# ----- pure unit tests on clustering -----


def _ranked(cid: str, start: float, end: float, hype: float = 0.5, keep: bool = True) -> dict:
    return {
        "candidate_id": cid,
        "keep": keep,
        "suggested_start_seconds": start,
        "suggested_end_seconds": end,
        "funny_score": 0.1,
        "hype_score": hype,
        "story_score": 0.2,
        "reason": f"r-{cid}",
    }


def _cand(cid: str, source: str = "audio_peak", confidence: float = 0.5) -> dict:
    return {"id": cid, "source": source, "confidence": confidence}


def test_cluster_empty_returns_empty():
    assert cluster_ranked_candidates([], []) == []


def test_cluster_drops_unkept():
    rs = [_ranked("a", 10, 15, keep=False), _ranked("b", 30, 35, keep=False)]
    assert cluster_ranked_candidates(rs, []) == []


def test_cluster_singletons_passed_through():
    rs = [_ranked("a", 10, 15), _ranked("b", 200, 210)]  # far apart, gap >> 30s
    out = cluster_ranked_candidates(rs, [])
    assert [r["candidate_id"] for r in out] == ["a", "b"]
    assert "[merged" not in out[0]["reason"]


def test_cluster_overlapping_windows_merge():
    rs = [_ranked("a", 10, 25), _ranked("b", 20, 35)]
    out = cluster_ranked_candidates(rs, [])
    assert len(out) == 1
    assert out[0]["suggested_start_seconds"] == 10
    assert out[0]["suggested_end_seconds"] == 35


def test_cluster_within_gap_merges():
    # 5s gap between a's end (25) and b's start (30) — under 30s threshold.
    rs = [_ranked("a", 10, 25), _ranked("b", 30, 40)]
    out = cluster_ranked_candidates(rs, [], gap_seconds=30.0)
    assert len(out) == 1
    assert (out[0]["suggested_start_seconds"], out[0]["suggested_end_seconds"]) == (10, 40)


def test_cluster_beyond_gap_does_not_merge():
    rs = [_ranked("a", 10, 25), _ranked("b", 80, 90)]  # 55s gap > 30
    out = cluster_ranked_candidates(rs, [], gap_seconds=30.0)
    assert len(out) == 2


def test_cluster_transitive_chain_merges_all():
    # A→B and B→C each within 30s; A→C alone wouldn't be (gap = 50s).
    rs = [_ranked("a", 10, 20), _ranked("b", 40, 50), _ranked("c", 65, 75)]
    out = cluster_ranked_candidates(rs, [], gap_seconds=30.0)
    assert len(out) == 1
    assert (out[0]["suggested_start_seconds"], out[0]["suggested_end_seconds"]) == (10, 75)


def test_cluster_anchor_prefers_riot_api():
    rs = [
        _ranked("a", 10, 20, hype=0.9),  # audio_peak with high hype
        _ranked("b", 22, 30, hype=0.4),  # riot_api ground-truth kill
    ]
    cs = [_cand("a", source="audio_peak"), _cand("b", source="riot_api", confidence=0.95)]
    out = cluster_ranked_candidates(rs, cs, gap_seconds=30.0)
    assert len(out) == 1
    # Anchor's id is b (riot), reason flows from b, scores still aggregated.
    assert out[0]["candidate_id"] == "b"
    assert "r-b" in out[0]["reason"]
    assert out[0]["hype_score"] == 0.9


def test_cluster_anchor_falls_back_to_highest_hype_without_riot():
    rs = [_ranked("a", 10, 20, hype=0.3), _ranked("b", 22, 30, hype=0.8)]
    cs = [_cand("a", source="audio_peak"), _cand("b", source="audio_peak")]
    out = cluster_ranked_candidates(rs, cs, gap_seconds=30.0)
    assert out[0]["candidate_id"] == "b"


def test_cluster_anchor_prefers_highest_confidence_riot():
    rs = [_ranked("a", 10, 20), _ranked("b", 22, 30), _ranked("c", 33, 40)]
    cs = [
        _cand("a", source="riot_api", confidence=0.7),
        _cand("b", source="audio_peak"),
        _cand("c", source="riot_api", confidence=0.95),
    ]
    out = cluster_ranked_candidates(rs, cs, gap_seconds=30.0)
    assert out[0]["candidate_id"] == "c"


def test_cluster_merged_reason_tags_count():
    rs = [_ranked("a", 10, 20), _ranked("b", 25, 35), _ranked("c", 40, 50)]
    out = cluster_ranked_candidates(rs, [], gap_seconds=30.0)
    assert len(out) == 1
    assert out[0]["reason"].startswith("[merged 3x]")


def test_cluster_gap_zero_disables_merging():
    rs = [_ranked("a", 10, 25), _ranked("b", 20, 35)]  # overlap
    out = cluster_ranked_candidates(rs, [], gap_seconds=0)
    assert len(out) == 2  # passthrough


def test_cluster_scores_are_aggregated_max():
    rs = [
        _ranked("a", 10, 20, hype=0.3),
        _ranked("b", 22, 30, hype=0.9),
    ]
    rs[0]["funny_score"] = 0.7
    rs[1]["funny_score"] = 0.2
    rs[0]["story_score"] = 0.1
    rs[1]["story_score"] = 0.6
    out = cluster_ranked_candidates(rs, [], gap_seconds=30.0)
    assert out[0]["hype_score"] == 0.9
    assert out[0]["funny_score"] == 0.7
    assert out[0]["story_score"] == 0.6


def test_segments_in_window_inclusive_on_boundaries():
    segs = [
        {"start_seconds": 0, "end_seconds": 5, "text": "before"},
        {"start_seconds": 7, "end_seconds": 12, "text": "overlap-front"},
        {"start_seconds": 13, "end_seconds": 18, "text": "inside"},
        {"start_seconds": 19, "end_seconds": 25, "text": "overlap-back"},
        {"start_seconds": 30, "end_seconds": 33, "text": "after"},
    ]
    out = _segments_in_window(segs, start=10, end=20)
    assert [s["text"] for s in out] == ["overlap-front", "inside", "overlap-back"]


# ----- integration: compile endpoint, ffmpeg mocked -----


@pytest.fixture
def seed_rankings(sandbox, asset):
    """Drop a minimal rankings JSON next to the asset workspace."""
    from ai_video_editor.config import settings

    folder = settings.workspace_dir / "rankings"
    folder.mkdir(parents=True, exist_ok=True)
    data = [
        {
            "candidate_id": str(uuid.uuid4()),
            "keep": True,
            "suggested_start_seconds": 10.0,
            "suggested_end_seconds": 20.0,
            "hype_score": 0.8,
            "funny_score": 0.1,
            "story_score": 0.4,
            "reason": "first",
        },
        {
            "candidate_id": str(uuid.uuid4()),
            "keep": True,
            "suggested_start_seconds": 40.0,
            "suggested_end_seconds": 50.0,
            "hype_score": 0.6,
            "funny_score": 0.2,
            "story_score": 0.3,
            "reason": "second",
        },
    ]
    (folder / f"{asset['id']}.json").write_text(json.dumps(data), encoding="utf-8")
    return data


def test_compile_endpoint_404s_without_rankings(client, asset, poll):
    r = client.post("/api/v1/edit/compile", json={"asset_id": asset["id"]}).json()
    job = poll(client, r["job_id"])
    assert job["status"] == "failed"
    assert "No rankings" in (job.get("error") or "")


def test_compile_endpoint_runs_with_seeded_rankings(
    client, asset, poll, monkeypatch, seed_rankings
):
    """End-to-end through the router, with build_compilation mocked to
    pretend ffmpeg succeeded — we want to verify wiring & DB persistence."""
    captured: dict = {}

    def fake_build(asset, rankings, segments, **kw):
        captured["kept"] = sum(1 for r in rankings if r.get("keep"))
        captured["kwargs"] = kw
        from ai_video_editor.config import settings

        folder = settings.workspace_dir / "compilations" / "fake"
        folder.mkdir(parents=True, exist_ok=True)
        out = (folder / "compilation.mp4").as_posix()
        return {
            "asset_id": asset.get("id"),
            "output": out,
            "compiled": True,
            "kept_total": captured["kept"],
            "parts_rendered": captured["kept"],
            "folder": folder.as_posix(),
            "parts": [],
        }

    monkeypatch.setattr("ai_video_editor.routers.edits.build_compilation", fake_build)

    payload = {
        "asset_id": asset["id"],
        "aspect": "9:16",
        "order": "hype",
        "fade_seconds": 0.5,
        "music_volume": 0.2,
    }
    r = client.post("/api/v1/edit/compile", json=payload).json()
    job = poll(client, r["job_id"])
    assert job["status"] == "completed"
    assert captured["kept"] == 2  # both rankings were keep=True
    assert captured["kwargs"]["aspect"] == "9:16"
    assert captured["kwargs"]["order"] == "hype"

    # Verify a compilations row landed in the DB.
    import asyncio

    from ai_video_editor.database import get_db

    async def _q():
        db = await get_db()
        try:
            rows = await db.execute_fetchall(
                "SELECT * FROM compilations WHERE asset_id = ?", (asset["id"],)
            )
            return [dict(x) for x in rows]
        finally:
            await db.close()

    saved = asyncio.run(_q())
    assert saved, "compilations row should be persisted"
    assert saved[0]["output_path"].endswith("compilation.mp4")


def test_compile_endpoint_failure_records_error(client, asset, poll, monkeypatch, seed_rankings):
    """If build_compilation reports compiled=False, the job is failed."""

    def fake_build(asset, rankings, segments, **kw):
        return {
            "asset_id": asset.get("id"),
            "output": None,
            "compiled": False,
            "error": "ffmpeg blew up",
            "kept_total": 2,
            "parts_rendered": 0,
            "folder": "/tmp/nope",
            "parts": [],
        }

    monkeypatch.setattr("ai_video_editor.routers.edits.build_compilation", fake_build)

    r = client.post("/api/v1/edit/compile", json={"asset_id": asset["id"]}).json()
    job = poll(client, r["job_id"])
    assert job["status"] == "failed"
    assert "ffmpeg blew up" in (job.get("error") or "")


def test_compile_endpoint_404s_unknown_asset(client):
    r = client.post(
        "/api/v1/edit/compile",
        json={"asset_id": str(uuid.uuid4())},
    )
    assert r.status_code == 404


# ----- iterative editing: pure helpers (resolver + mutators) -----


def _spec(*clips: tuple[str, float, float]) -> dict:
    return {
        "id": "spec-1",
        "aspect": "16:9",
        "fade_seconds": 0.3,
        "clips": [
            {
                "id": cid,
                "asset_id": "a1",
                "asset_path": "/fake.mp4",
                "start_seconds": s,
                "end_seconds": e,
                "event_type": "clip",
                "caption_segments": [],
                "effects": [],
            }
            for cid, s, e in clips
        ],
    }


def test_reel_positions_running_sum():
    from ai_video_editor.compile import reel_positions

    spec = _spec(("a", 100, 110), ("b", 200, 215), ("c", 500, 508))
    assert reel_positions(spec) == [(0.0, 10.0), (10.0, 25.0), (25.0, 33.0)]


def test_resolve_clip_ref_by_index():
    from ai_video_editor.compile import resolve_clip_ref

    spec = _spec(("a", 100, 110), ("b", 200, 215))
    assert resolve_clip_ref(spec, "1") == 0
    assert resolve_clip_ref(spec, 2) == 1


def test_resolve_clip_ref_by_uuid_prefix():
    from ai_video_editor.compile import resolve_clip_ref

    spec = _spec(("abcd1234-x", 0, 1), ("efgh-y", 5, 6))
    assert resolve_clip_ref(spec, "abcd") == 0
    assert resolve_clip_ref(spec, "efgh") == 1


def test_resolve_clip_ref_by_reel_time_then_source():
    from ai_video_editor.compile import resolve_clip_ref

    # reel widths: [0..10), [10..25)
    spec = _spec(("a", 100, 110), ("b", 200, 215))
    assert resolve_clip_ref(spec, "0:05") == 0  # reel time
    assert resolve_clip_ref(spec, "0:12") == 1  # reel time
    # source-time fallback (not in any reel range; matches source)
    assert resolve_clip_ref(spec, "3:25") == 1  # 205s, inside clip b's source range


def test_resolve_clip_ref_unknown_raises():
    from ai_video_editor.compile import resolve_clip_ref

    spec = _spec(("a", 100, 110))
    with pytest.raises(KeyError):
        resolve_clip_ref(spec, "nope")
    with pytest.raises(KeyError):
        resolve_clip_ref(spec, "99")


def test_add_effect_does_not_mutate_caller_spec():
    from ai_video_editor.compile import add_effect

    spec = _spec(("a", 0, 5), ("b", 10, 15))
    new, dirty = add_effect(spec, 1, {"kind": "zoom", "factor": 1.5})
    assert dirty == {"b"}
    assert new["clips"][1]["effects"] == [{"kind": "zoom", "factor": 1.5}]
    # original unchanged
    assert spec["clips"][1]["effects"] == []


def test_extend_clip_grows_window_and_clamps_to_zero():
    from ai_video_editor.compile import extend_clip

    spec = _spec(("a", 3.0, 10.0))
    new, dirty = extend_clip(spec, 0, before=5.0, after=2.0)
    # before=5 from start=3 -> clamped to 0; after extends end
    assert new["clips"][0]["start_seconds"] == 0.0
    assert new["clips"][0]["end_seconds"] == 12.0
    assert dirty == {"a"}


def test_insert_clip_appends_when_position_omitted_and_asset_unknown_to_reel():
    from ai_video_editor.compile import insert_clip

    spec = _spec(("a", 0, 5), ("b", 10, 15))
    # asset 'other' not currently in reel -> append at end
    new, dirty = insert_clip(
        spec,
        asset_id="other",
        asset_path="/other.mp4",
        asset_filename="other.mp4",
        start_seconds=100,
        end_seconds=110,
    )
    assert len(new["clips"]) == 3
    assert new["clips"][2]["asset_id"] == "other"
    assert len(dirty) == 1
    assert next(iter(dirty)) == new["clips"][2]["id"]
    # caller's spec untouched
    assert len(spec["clips"]) == 2


def test_insert_clip_chronological_by_source_within_same_asset():
    from ai_video_editor.compile import insert_clip

    # All clips share asset_id "a1"; new clip at source 150 should slot
    # between (100..110) and (200..215).
    spec = _spec(("c-a", 100, 110), ("c-b", 200, 215))
    new, _ = insert_clip(
        spec,
        asset_id="a1",
        asset_path="/fake.mp4",
        asset_filename="fake.mp4",
        start_seconds=150,
        end_seconds=160,
    )
    starts = [c["start_seconds"] for c in new["clips"]]
    assert starts == [100, 150, 200]


def test_insert_clip_explicit_position_overrides_chronology():
    from ai_video_editor.compile import insert_clip

    spec = _spec(("c-a", 100, 110), ("c-b", 200, 215))
    # Force the new clip to the front even though chronology would slot
    # it between the existing two.
    new, _ = insert_clip(
        spec,
        asset_id="a1",
        asset_path="/fake.mp4",
        asset_filename="fake.mp4",
        start_seconds=150,
        end_seconds=160,
        position=1,
    )
    assert [c["start_seconds"] for c in new["clips"]] == [150, 100, 200]


def test_insert_clip_clamps_out_of_range_position():
    from ai_video_editor.compile import insert_clip

    spec = _spec(("c-a", 0, 5))
    # position=99 beyond the reel -> append at len(clips)+1 = 2
    new, _ = insert_clip(
        spec,
        asset_id="a1",
        asset_path="/x.mp4",
        asset_filename="x.mp4",
        start_seconds=10,
        end_seconds=15,
        position=99,
    )
    assert [c["start_seconds"] for c in new["clips"]] == [0, 10]


def test_insert_clip_defaults_event_type_to_manual():
    from ai_video_editor.compile import insert_clip

    spec = _spec(("c-a", 0, 5))
    new, _ = insert_clip(
        spec,
        asset_id="a1",
        asset_path="/x.mp4",
        asset_filename="x.mp4",
        start_seconds=10,
        end_seconds=15,
    )
    assert new["clips"][-1]["event_type"] == "manual"


def test_remove_clip_shrinks_list_and_reports_no_dirty_renders():
    from ai_video_editor.compile import remove_clip

    spec = _spec(("a", 0, 5), ("b", 10, 15), ("c", 20, 25))
    new, dirty = remove_clip(spec, 1)
    assert [c["id"] for c in new["clips"]] == ["a", "c"]
    # nothing needs re-rendering — concat is the only step
    assert dirty == set()


# ----- iterative editing: integration tests against the endpoints -----


@pytest.fixture
def seeded_compilation(client, asset, monkeypatch):
    """Make a finished compilation visible to the iterative-edit endpoints
    (DB row + folder + spec.json + dummy compilation.mp4)."""
    from ai_video_editor.config import settings

    monkeypatch.setattr(
        "ai_video_editor.routers.edits.build_compilation",
        lambda *a, **kw: _fake_build_compilation(asset),
    )
    folder = settings.workspace_dir / "compilations" / "test-folder"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "_parts").mkdir(exist_ok=True)

    spec = _spec(("c-aaa", 100.0, 110.0), ("c-bbb", 200.0, 215.0))
    spec["asset_id"] = asset["id"]
    # Anchor asset_path to the asset's record so render_spec can find a path
    for c in spec["clips"]:
        c["asset_id"] = asset["id"]
        c["asset_path"] = asset["path"]
    (folder / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    (folder / "compilation.mp4").write_bytes(b"")

    import asyncio

    from ai_video_editor.database import get_db

    comp_id = str(uuid.uuid4())

    async def _seed():
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO compilations (id, asset_id, output_path, params, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    comp_id,
                    asset["id"],
                    (folder / "compilation.mp4").as_posix(),
                    "{}",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            await db.commit()
        finally:
            await db.close()

    asyncio.run(_seed())
    return comp_id, folder, spec


def _fake_build_compilation(asset):
    return {
        "output": None,
        "compiled": False,
        "kept_total": 0,
        "parts_rendered": 0,
        "folder": "/tmp/x",
        "parts": [],
    }


def test_list_compilation_clips_returns_reel_and_source(client, seeded_compilation):
    comp_id, _, _ = seeded_compilation
    r = client.get(f"/api/v1/edit/compile/{comp_id}/clips").json()
    assert r["compilation_id"] == comp_id
    assert len(r["clips"]) == 2
    assert r["clips"][0]["reel"] == "0:00-0:10"
    assert r["clips"][1]["reel"] == "0:10-0:25"


def test_add_effect_endpoint_mutates_spec_and_rerenders(client, seeded_compilation, monkeypatch):
    comp_id, folder, _ = seeded_compilation

    seen: dict = {}

    def fake_render(spec, fld, dirty_clip_ids=None):
        seen["dirty"] = set(dirty_clip_ids) if dirty_clip_ids is not None else None
        seen["spec_clips"] = [c["effects"] for c in spec["clips"]]
        return {"output": (folder / "compilation.mp4").as_posix(), "compiled": True}

    monkeypatch.setattr("ai_video_editor.routers.edits.render_spec", fake_render)

    r = client.post(
        f"/api/v1/edit/compile/{comp_id}/effect",
        json={"clip_ref": "2", "kind": "zoom", "factor": 1.6, "roi": "center"},
    )
    assert r.status_code == 200
    assert seen["spec_clips"][1] == [{"kind": "zoom", "factor": 1.6, "roi": "center"}]
    assert seen["dirty"] == {"c-bbb"}

    # Spec on disk was actually mutated
    spec_after = json.loads((folder / "spec.json").read_text(encoding="utf-8"))
    assert spec_after["clips"][1]["effects"][0]["kind"] == "zoom"


def test_extend_endpoint_grows_clip(client, seeded_compilation, monkeypatch):
    comp_id, folder, _ = seeded_compilation
    monkeypatch.setattr(
        "ai_video_editor.routers.edits.render_spec",
        lambda spec, fld, dirty_clip_ids=None: {
            "output": (folder / "compilation.mp4").as_posix(),
            "compiled": True,
        },
    )
    r = client.post(
        f"/api/v1/edit/compile/{comp_id}/extend",
        json={"clip_ref": "1", "before": 5.0, "after": 3.0},
    )
    assert r.status_code == 200
    after = json.loads((folder / "spec.json").read_text(encoding="utf-8"))
    assert after["clips"][0]["start_seconds"] == 95.0
    assert after["clips"][0]["end_seconds"] == 113.0


def test_remove_endpoint_drops_clip(client, seeded_compilation, monkeypatch):
    comp_id, folder, _ = seeded_compilation
    monkeypatch.setattr(
        "ai_video_editor.routers.edits.render_spec",
        lambda spec, fld, dirty_clip_ids=None: {
            "output": (folder / "compilation.mp4").as_posix(),
            "compiled": True,
        },
    )
    r = client.post(f"/api/v1/edit/compile/{comp_id}/remove", json={"clip_ref": "1"})
    assert r.status_code == 200
    after = json.loads((folder / "spec.json").read_text(encoding="utf-8"))
    assert len(after["clips"]) == 1
    assert after["clips"][0]["id"] == "c-bbb"


def test_insert_endpoint_appends_clip_and_pulls_caption_segments(
    client, seeded_compilation, asset, monkeypatch
):
    """Auto caption-pull path: drop transcript rows in the window and
    confirm the inserted clip carries them as caption_segments."""
    comp_id, folder, _ = seeded_compilation

    # Seed two transcript rows that overlap [50, 60] in the asset.
    import asyncio

    from ai_video_editor.database import get_db

    async def _seed_transcript():
        db = await get_db()
        try:
            now = datetime.now(timezone.utc).isoformat()
            for s, e, t in [(48.0, 52.0, "before"), (55.0, 58.0, "middle")]:
                await db.execute(
                    "INSERT INTO transcripts "
                    "(id, video_id, start_seconds, end_seconds, text, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), asset["id"], s, e, t, now),
                )
            await db.commit()
        finally:
            await db.close()

    asyncio.run(_seed_transcript())

    seen: dict = {}

    def fake_render(spec, fld, dirty_clip_ids=None):
        seen["clips"] = list(spec["clips"])
        seen["dirty"] = set(dirty_clip_ids) if dirty_clip_ids is not None else None
        return {"output": (folder / "compilation.mp4").as_posix(), "compiled": True}

    monkeypatch.setattr("ai_video_editor.routers.edits.render_spec", fake_render)

    r = client.post(
        f"/api/v1/edit/compile/{comp_id}/insert",
        json={
            "asset_id": asset["id"],
            "start_seconds": 50.0,
            "end_seconds": 60.0,
        },
    )
    assert r.status_code == 200
    # 2 originals + 1 inserted; chronological-within-asset, but seeded
    # spec's asset_id is the same as `asset['id']`, and start 50 lands
    # before c-aaa at 100 -> inserted at index 0.
    assert len(seen["clips"]) == 3
    inserted = seen["clips"][0]
    assert inserted["asset_id"] == asset["id"]
    assert inserted["start_seconds"] == 50.0
    assert inserted["end_seconds"] == 60.0
    assert inserted["event_type"] == "manual"
    # Caption auto-pull picked up both transcript rows.
    assert [s["text"] for s in inserted["caption_segments"]] == ["before", "middle"]
    # Only the new clip is in the dirty set.
    assert seen["dirty"] == {inserted["id"]}

    # Spec on disk reflects the mutation.
    spec_after = json.loads((folder / "spec.json").read_text(encoding="utf-8"))
    assert len(spec_after["clips"]) == 3
    assert spec_after["clips"][0]["start_seconds"] == 50.0


def test_insert_endpoint_text_override_skips_transcript_pull(
    client, seeded_compilation, asset, monkeypatch
):
    """text= path: one synthetic segment spanning the clip, no DB pull."""
    comp_id, folder, _ = seeded_compilation
    monkeypatch.setattr(
        "ai_video_editor.routers.edits.render_spec",
        lambda spec, fld, dirty_clip_ids=None: {
            "output": (folder / "compilation.mp4").as_posix(),
            "compiled": True,
        },
    )
    r = client.post(
        f"/api/v1/edit/compile/{comp_id}/insert",
        json={
            "asset_id": asset["id"],
            "start_seconds": 300.0,
            "end_seconds": 310.0,
            "text": "CLUTCH",
            "event_type": "callout",
        },
    )
    assert r.status_code == 200
    spec_after = json.loads((folder / "spec.json").read_text(encoding="utf-8"))
    # Inserted at the end (300 > 100, 200).
    inserted = spec_after["clips"][-1]
    assert inserted["event_type"] == "callout"
    assert inserted["caption_segments"] == [
        {"start_seconds": 300.0, "end_seconds": 310.0, "text": "CLUTCH"}
    ]


def test_insert_endpoint_404s_unknown_asset(client, seeded_compilation):
    comp_id, _, _ = seeded_compilation
    r = client.post(
        f"/api/v1/edit/compile/{comp_id}/insert",
        json={
            "asset_id": str(uuid.uuid4()),
            "start_seconds": 0.0,
            "end_seconds": 5.0,
        },
    )
    assert r.status_code == 404


def test_insert_endpoint_400s_when_end_le_start(client, seeded_compilation, asset):
    comp_id, _, _ = seeded_compilation
    r = client.post(
        f"/api/v1/edit/compile/{comp_id}/insert",
        json={
            "asset_id": asset["id"],
            "start_seconds": 50.0,
            "end_seconds": 50.0,
        },
    )
    assert r.status_code == 400


def test_edit_endpoints_404_unknown_compilation(client):
    r = client.get(f"/api/v1/edit/compile/{uuid.uuid4()}/clips")
    assert r.status_code == 404
    r2 = client.post(
        f"/api/v1/edit/compile/{uuid.uuid4()}/effect",
        json={"clip_ref": "1", "kind": "zoom"},
    )
    assert r2.status_code == 404


def test_add_effect_bad_clip_ref_400s(client, seeded_compilation, monkeypatch):
    comp_id, _, _ = seeded_compilation
    monkeypatch.setattr(
        "ai_video_editor.routers.edits.render_spec",
        lambda *a, **k: {"output": None, "compiled": True},
    )
    r = client.post(
        f"/api/v1/edit/compile/{comp_id}/effect",
        json={"clip_ref": "nonsense", "kind": "zoom"},
    )
    assert r.status_code == 400


# ----- reorder_clips_explicit (drag-and-drop user reordering) -----


def _spec_with_clips(ids: list[str]) -> dict:
    """Minimal spec with the given clip ids — just enough for the mutator."""
    return {"clips": [{"id": cid, "start_seconds": float(i)} for i, cid in enumerate(ids)]}


def test_reorder_explicit_swaps_order():
    spec = _spec_with_clips(["a", "b", "c"])
    new_spec, dirty = reorder_clips_explicit(spec, ["c", "a", "b"])
    assert [c["id"] for c in new_spec["clips"]] == ["c", "a", "b"]
    assert dirty == {"a", "b", "c"}  # all clip ids are reported dirty


def test_reorder_explicit_preserves_clip_data():
    """Reordering must not lose or mutate per-clip fields."""
    spec = {
        "clips": [
            {"id": "a", "start_seconds": 10.0, "event_type": "kill"},
            {"id": "b", "start_seconds": 20.0, "event_type": "death"},
        ]
    }
    new_spec, _ = reorder_clips_explicit(spec, ["b", "a"])
    assert new_spec["clips"][0]["start_seconds"] == 20.0
    assert new_spec["clips"][0]["event_type"] == "death"
    assert new_spec["clips"][1]["start_seconds"] == 10.0


def test_reorder_explicit_rejects_missing_id():
    spec = _spec_with_clips(["a", "b", "c"])
    with pytest.raises(ValueError, match="missing"):
        reorder_clips_explicit(spec, ["a", "b"])  # dropped "c"


def test_reorder_explicit_rejects_unknown_id():
    spec = _spec_with_clips(["a", "b"])
    with pytest.raises(ValueError, match="unknown"):
        reorder_clips_explicit(spec, ["a", "b", "zzz"])


def test_reorder_explicit_rejects_duplicates():
    spec = _spec_with_clips(["a", "b"])
    with pytest.raises(ValueError, match="duplicate"):
        reorder_clips_explicit(spec, ["a", "a"])


def test_reorder_explicit_handles_empty_spec():
    """No clips = no-op, no error."""
    new_spec, dirty = reorder_clips_explicit({"clips": []}, [])
    assert new_spec["clips"] == []
    assert dirty == set()


def test_reorder_explicit_moves_intros_freely():
    """Unlike mode-based reorder which fixes intros in place, an
    explicit user-driven reorder lets intros move with the rest."""
    spec = {
        "clips": [
            {"id": "intro1", "event_type": "intro"},
            {"id": "kill1", "event_type": "kill"},
            {"id": "kill2", "event_type": "kill"},
        ]
    }
    # User drags the intro to the END.
    new_spec, _ = reorder_clips_explicit(spec, ["kill1", "kill2", "intro1"])
    assert [c["id"] for c in new_spec["clips"]] == ["kill1", "kill2", "intro1"]


# Optional integration helper retained for future use — currently unused.
_ = datetime.now(timezone.utc)
