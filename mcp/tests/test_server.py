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
                {
                    "id": "1",
                    "filename": "lol.mp4",
                    "game": "League of Legends",
                    "created_at": "2026-05-21T01:00:00Z",
                },
                {
                    "id": "2",
                    "filename": "val.mp4",
                    "game": "Valorant",
                    "created_at": "2026-05-20T01:00:00Z",
                },
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
                "hype": [
                    {
                        "file": "01_ace_5m.mp4",
                        "event_type": "ace",
                        "hype_score": 0.95,
                        "funny_score": 0.2,
                        "story_score": 0.4,
                        "reason": "event_type=ace",
                    }
                ],
                "story": [],
                "voice_over": [],
                "funny": [],
                "skipped": [],
            },
        )

    with _patch_client(handler):
        out = server.list_session_buckets(
            game="Valorant",
            session="07-07-2026_23h44",
        )
    assert seen["method"] == "GET"
    assert seen["url"].endswith("/api/v1/highlights/Valorant/07-07-2026_23h44/buckets")
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


def test_story_list_and_remove_request():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"round": "r1", "requests": []})

    with _patch_client(handler):
        server.story_list_requests("r1")
        server.story_remove_request("r1", 2)

    assert seen == [
        ("GET", "/api/v1/story/r1/requests"),
        ("DELETE", "/api/v1/story/r1/requests/2"),
    ]


def test_story_add_request_sends_only_set_fields():
    import json as _json

    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/story/r1/requests"
        bodies.append(_json.loads(request.content))
        return httpx.Response(200, json={"round": "r1", "requests": [{"index": 0}]})

    with _patch_client(handler):
        server.story_add_request("r1", "add_chapter", "20:05", title="Walk out", after_s=15)
        server.story_add_request(
            "r1",
            "effect",
            "31:10",
            effect="slowmo",
            start="31:08",
            end="31:14",
            params={"speed": 0.5},
        )

    assert bodies[0] == {"kind": "add_chapter", "at": "20:05", "title": "Walk out", "after_s": 15}
    assert bodies[1] == {
        "kind": "effect",
        "at": "31:10",
        "effect": "slowmo",
        "start": "31:08",
        "end": "31:14",
        "params": {"speed": 0.5},
    }


def test_story_add_request_forwards_trim_and_mute():
    import json as _json

    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(_json.loads(request.content))
        return httpx.Response(200, json={"round": "r1", "requests": []})

    with _patch_client(handler):
        server.story_add_request("r1", "trim", "31:10", start="31:20")
        server.story_add_request("r1", "mute", "12:10", start="12:00", end="12:30")

    assert bodies == [
        {"kind": "trim", "at": "31:10", "start": "31:20"},
        {"kind": "mute", "at": "12:10", "start": "12:00", "end": "12:30"},
    ]


def test_story_render_polls_the_job():
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.path == "/api/v1/story/r1/render"
            assert _json.loads(request.content) == {"asset_id": "a1", "plays": True}
            return httpx.Response(200, json={"job_id": "j1"})
        assert request.url.path == "/api/v1/jobs/j1"
        return httpx.Response(200, json={"status": "completed", "output_path": "story.mp4"})

    with _patch_client(handler):
        out = server.story_render("r1", "a1")
    assert out["output_path"] == "story.mp4"


def test_story_join_posts_hook_and_outro():
    import json as _json

    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.path == "/api/v1/story/join"
            bodies.append(_json.loads(request.content))
            return httpx.Response(200, json={"job_id": "j1"})
        return httpx.Response(200, json={"status": "completed", "output_path": "j.mp4"})

    with _patch_client(handler):
        out = server.story_join(["a", "b"], hook=["2:2:2v3?"], hook_title="Hi", outro=True)
    assert out["output_path"] == "j.mp4"
    assert bodies[0]["hook"] == ["2:2:2v3?"] and bodies[0]["outro"] is True
    assert bodies[0]["rounds"] == ["a", "b"] and bodies[0]["subscribe_after"] == 1


def test_narration_status_and_remove_take():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"takes": []})

    with _patch_client(handler):
        server.narration_status("story", "r1")
        server.narration_remove_take("story", "r1", "abc123")

    assert seen == [
        ("GET", "/api/v1/narration/story/r1"),
        ("DELETE", "/api/v1/narration/story/r1/takes/abc123"),
    ]


def test_narration_set_camera_sends_only_set_fields():
    import json as _json

    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("PUT", "/api/v1/narration/story/r1/camera")
        bodies.append(_json.loads(request.content))
        return httpx.Response(200, json={"camera": {}})

    with _patch_client(handler):
        server.narration_set_camera("story", "r1", True)
        server.narration_set_camera("story", "r1", False, width_frac=0.2, x=0.7, y=0.6)

    assert bodies[0] == {"enabled": True}
    assert bodies[1] == {"enabled": False, "width_frac": 0.2, "x": 0.7, "y": 0.6}


def test_narration_export_polls_the_job():
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.path == "/api/v1/narration/story/r1/export"
            assert _json.loads(request.content) == {"force": True}
            return httpx.Response(200, json={"job_id": "j1"})
        assert request.url.path == "/api/v1/jobs/j1"
        return httpx.Response(200, json={"status": "completed", "output_path": "narrated.mp4"})

    with _patch_client(handler):
        out = server.narration_export("story", "r1", force=True)
    assert out["output_path"] == "narrated.mp4"


def test_narration_export_stale_returns_the_409_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "the video changed"})

    with _patch_client(handler):
        out = server.narration_export("story", "r1")
    assert out["status"] == "failed" and "video changed" in out["error"]


def test_story_suggestion_tools_are_registered():
    import asyncio

    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {
        "story_suggest_edits",
        "story_list_suggestions",
        "story_accept_suggestion",
        "story_apply_suggestions",
    } <= names


def test_story_suggest_edits_polls_the_job_then_lists():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(200, json={"job_id": "j1"})
        if request.url.path == "/api/v1/jobs/j1":
            return httpx.Response(200, json={"status": "completed"})
        return httpx.Response(200, json={"status": "ready", "items": [{"id": "s1"}]})

    with _patch_client(handler):
        out = server.story_suggest_edits("r1")
    assert seen == [
        ("POST", "/api/v1/story/r1/suggestions"),
        ("GET", "/api/v1/jobs/j1"),
        ("GET", "/api/v1/story/r1/suggestions"),
    ]
    assert out["items"] == [{"id": "s1"}]


def test_story_suggest_edits_returns_a_failed_job():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"job_id": "j1"})
        return httpx.Response(200, json={"status": "failed", "error": "no timeline"})

    with _patch_client(handler):
        out = server.story_suggest_edits("r1")
    assert out["status"] == "failed" and out["error"] == "no timeline"


def test_story_list_and_accept_suggestion():
    import json as _json

    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        return httpx.Response(200, json={"status": "ready", "items": []})

    with _patch_client(handler):
        server.story_list_suggestions("r1")
        server.story_accept_suggestion("r1", "s-c1-slowmo", True)
        server.story_accept_suggestion("r1", "s-c1-slowmo", None)

    assert seen == [
        ("GET", "/api/v1/story/r1/suggestions", None),
        ("PUT", "/api/v1/story/r1/suggestions/s-c1-slowmo", {"accepted": True}),
        ("PUT", "/api/v1/story/r1/suggestions/s-c1-slowmo", {"accepted": None}),
    ]


def test_story_apply_suggestions_polls_the_render_job():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.path == "/api/v1/story/r1/suggestions/apply"
            return httpx.Response(200, json={"job_id": "j2"})
        assert request.url.path == "/api/v1/jobs/j2"
        return httpx.Response(200, json={"status": "completed", "output_path": "story.mp4"})

    with _patch_client(handler):
        out = server.story_apply_suggestions("r1")
    assert out["output_path"] == "story.mp4"


def test_story_apply_suggestions_error_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "no suggestions"})

    with _patch_client(handler):
        out = server.story_apply_suggestions("r1")
    assert out["status"] == "failed" and "no suggestions" in out["error"]
