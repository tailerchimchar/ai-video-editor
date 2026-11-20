import httpx
import pytest

from ai_video_editor.grokbot.runner import ApiError, HttpApiClient


def make(handler):
    return HttpApiClient("http://api", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_exact_paths():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path))
        if req.url.path.endswith("/plan/version"):
            return httpx.Response(200, json={"version": "4"})
        if req.url.path.startswith("/api/v1/edit/compile/"):
            return httpx.Response(200, json={"output_path": "/x/c.mp4"})
        return httpx.Response(200, json={"corrections": [{"source_context": "s"}]})

    api = make(handler)
    assert api.plan_version("c1") == "4"
    api.apply_plan("c1", {})
    assert api.compilation_output("c1") == "/x/c.mp4"
    api.record_correction({})
    assert api.recent_corrections("league") == [{"source_context": "s"}]
    assert seen == [
        ("GET", "/api/v1/compilations/c1/plan/version"),
        ("POST", "/api/v1/compilations/c1/plan"),
        ("GET", "/api/v1/edit/compile/c1"),
        ("POST", "/api/v1/mentor/record_correction"),
        ("GET", "/api/v1/mentor/recent_corrections"),
    ]


def test_4xx_is_api_error_and_5xx_is_transient():
    api = make(lambda r: httpx.Response(422, json={"detail": {"problems": ["p"]}}))
    with pytest.raises(ApiError) as e:
        api.apply_plan("c", {})
    assert e.value.status == 422 and e.value.detail == {"problems": ["p"]}
    api = make(lambda r: httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        api.plan_version("c")
