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
