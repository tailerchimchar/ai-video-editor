"""Smoke tests for the MCP server.

The server is intentionally a thin HTTP adapter, so the tests just
verify that calling a tool produces the expected backend HTTP call —
no real backend needed. httpx's MockTransport is enough.
"""

from unittest.mock import patch

import httpx

from ai_video_editor_mcp import server


def _patch_client(handler) -> object:
    """Patch server._client() to return an httpx.Client backed by `handler`."""
    transport = httpx.MockTransport(handler)
    return patch.object(server, "_client", lambda: httpx.Client(transport=transport))


def test_scan_assets_posts_to_backend():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"new": 3, "total": 12})

    with _patch_client(handler):
        out = server.scan_assets()

    assert seen["method"] == "POST"
    assert seen["url"].endswith("/api/v1/assets/scan")
    assert out == {"new": 3, "total": 12}


def test_list_assets_filters_by_game_substring():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"id": "1", "filename": "lol.mp4", "game": "League of Legends",
                 "created_at": "2026-05-21T01:00:00Z"},
                {"id": "2", "filename": "val.mp4", "game": "Valorant",
                 "created_at": "2026-05-20T01:00:00Z"},
            ],
        )

    with _patch_client(handler):
        out = server.list_assets(game="League")

    assert len(out) == 1
    assert out[0]["filename"] == "lol.mp4"


def test_get_job_returns_backend_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/api/v1/jobs/abc-123")
        return httpx.Response(200, json={"status": "completed", "output_path": "/x.mp4"})

    with _patch_client(handler):
        out = server.get_job("abc-123")

    assert out["status"] == "completed"
    assert out["output_path"] == "/x.mp4"


def test_insert_compilation_clip_posts_correct_body():
    """Spot-check the newest tool — body includes optional fields only when set."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"compiled": True})

    with _patch_client(handler):
        server.insert_compilation_clip(
            compilation_id="comp-1",
            asset_id="asset-1",
            start_seconds=10.0,
            end_seconds=20.0,
            text="CLUTCH",
        )

    import json as _json
    assert seen["url"].endswith("/api/v1/edit/compile/comp-1/insert")
    body = _json.loads(seen["body"])
    assert body["text"] == "CLUTCH"
    assert body["event_type"] == "manual"
    # position omitted in call -> not in body
    assert "position" not in body


def test_watch_montage_requires_name_or_path():
    """Neither custom_name nor montage_path -> error, no HTTP call."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not have called backend")

    with _patch_client(handler):
        out = server.watch_montage()
    assert out["status"] == "failed"
    assert "custom_name" in out["error"] or "montage_path" in out["error"]


def test_list_session_buckets_hits_bucketing_endpoint():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "session": "07-07-2026_23h44",
                "hype": [{"file": "01_ace_5m.mp4", "event_type": "ace",
                          "hype_score": 0.95, "funny_score": 0.2,
                          "story_score": 0.4, "reason": "event_type=ace"}],
                "story": [],
                "voice_over": [],
                "funny": [],
                "skipped": [],
            },
        )

    with _patch_client(handler):
        out = server.list_session_buckets(
            game="Valorant", session="07-07-2026_23h44",
        )
    assert seen["method"] == "GET"
    assert seen["url"].endswith(
        "/api/v1/highlights/Valorant/07-07-2026_23h44/buckets"
    )
    assert out["hype"][0]["file"] == "01_ace_5m.mp4"


def test_watch_montage_posts_custom_name_body():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        return httpx.Response(
            200,
            json={
                "panel_count": 15,
                "total_seconds": 60.0,
                "error": None,
                "critique": {
                    "overall_verdict": "needs_edits",
                    "opener_strength": 4,
                    "climax_strength": 5,
                    "pacing": "just_right",
                    "top_recommendation": "cut the scoreboard at t=4-8s",
                    "flags": [],
                },
            },
        )

    with _patch_client(handler):
        out = server.watch_montage(custom_name="Valorant_best_of_v1")

    import json as _json
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/api/v1/shorts/watch")
    body = _json.loads(seen["body"])
    assert body["custom_name"] == "Valorant_best_of_v1"
    assert body["save_markdown"] is True
    assert out["critique"]["overall_verdict"] == "needs_edits"


def test_submit_box_render_posts_to_backend():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"id": "item-1", "status": "queued"})

    with _patch_client(handler):
        out = server.submit_box_render(
            asset_id="asset-1",
            edit_plan_id="plan-1",
            edit_plan_version="1",
            game="league",
            clips=[{"clip_ref": "1", "drive_id": "d1", "start_s": 0.0, "end_s": 10.0}],
            concat_order=["1"],
        )

    import json as _json
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/api/v1/grokbot/render")
    body = _json.loads(seen["body"])
    assert body["asset_id"] == "asset-1"
    assert body["game"] == "league"
    assert out == {"id": "item-1", "status": "queued"}


def test_submit_box_render_surfaces_disabled_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "box render disabled"})

    with _patch_client(handler):
        out = server.submit_box_render(
            asset_id="asset-1",
            edit_plan_id="plan-1",
            edit_plan_version="1",
            game="league",
            clips=[{"clip_ref": "1", "drive_id": "d1", "start_s": 0.0, "end_s": 10.0}],
            concat_order=["1"],
        )

    assert out == {"error": "box render disabled", "status_code": 503}
