"""OpenAI-compatible backend (Gemini / OpenRouter): transport only, no network."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ai_video_editor.config import settings
from ai_video_editor.league import understand
from ai_video_editor.vlm.backends.base import VLMUnavailableError
from ai_video_editor.vlm.backends.openai_compat_backend import OpenAICompatBackend
from ai_video_editor.vlm.client import select_backend


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


@pytest.fixture
def frame(tmp_path: Path) -> Path:
    p = tmp_path / "f.jpg"
    p.write_bytes(b"\xff\xd8jpeg")
    return p


@pytest.fixture
def gemini_key(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")


def test_call_posts_frames_as_data_urls_in_json_mode(gemini_key, frame):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return _ok('{"frames": []}')

    be = OpenAICompatBackend("gemini", client=_client(handler))
    out = be.call(frames=[frame], system_prompt="sys", user_prompt="usr", max_tokens=99)

    assert out == '{"frames": []}'
    assert seen["url"].endswith("/v1beta/openai/chat/completions")
    assert seen["auth"] == "Bearer test-key"
    body = seen["body"]
    assert body["model"] == settings.gemini_model
    assert body["max_tokens"] == 99
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    parts = body["messages"][1]["content"]
    assert parts[0] == {"type": "text", "text": "usr"}
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_missing_key_is_unavailable(monkeypatch, frame):
    monkeypatch.setattr(settings, "gemini_api_key", None)
    be = OpenAICompatBackend("gemini", client=_client(lambda r: _ok("{}")))
    with pytest.raises(VLMUnavailableError, match="GEMINI_API_KEY"):
        be.call(frames=[frame], system_prompt="s", user_prompt="u")
    assert be.health()["ok"] is False


def test_rate_limit_waits_then_succeeds(gemini_key, frame):
    replies = iter([httpx.Response(429, text="slow down"), _ok("{}")])
    waits: list[float] = []
    be = OpenAICompatBackend("gemini", client=_client(lambda r: next(replies)), sleep=waits.append)
    assert be.call(frames=[frame], system_prompt="s", user_prompt="u") == "{}"
    assert len(waits) == 1


def test_rate_limit_that_persists_is_unavailable(gemini_key, frame):
    be = OpenAICompatBackend(
        "gemini",
        client=_client(lambda r: httpx.Response(429, text="quota")),
        sleep=lambda s: None,
    )
    with pytest.raises(VLMUnavailableError, match="429"):
        be.call(frames=[frame], system_prompt="s", user_prompt="u")


def test_auth_error_is_unavailable_and_does_not_leak_key(gemini_key, frame):
    be = OpenAICompatBackend(
        "gemini", client=_client(lambda r: httpx.Response(401, text="bad key"))
    )
    with pytest.raises(VLMUnavailableError) as exc:
        be.call(frames=[frame], system_prompt="s", user_prompt="u")
    assert "test-key" not in str(exc.value)


def test_network_error_is_unavailable(gemini_key, frame):
    def handler(request):
        raise httpx.ConnectError("down")

    be = OpenAICompatBackend("gemini", client=_client(handler))
    with pytest.raises(VLMUnavailableError, match="unreachable"):
        be.call(frames=[frame], system_prompt="s", user_prompt="u")


def test_openrouter_uses_its_own_url_key_and_model(monkeypatch, frame):
    monkeypatch.setattr(settings, "openrouter_api_key", "or-key")
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["model"] = json.loads(request.content)["model"]
        return _ok("{}")

    OpenAICompatBackend("openrouter", client=_client(handler)).call(
        frames=[frame], system_prompt="s", user_prompt="u"
    )
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["model"] == settings.openrouter_model


def test_select_backend_knows_gemini_and_openrouter():
    assert select_backend("gemini").name == "gemini"
    assert select_backend("OpenRouter").name == "openrouter"


def test_play_confirm_backend_overrides_vlm_backend(monkeypatch):
    monkeypatch.setattr(settings, "vlm_backend", "anthropic")
    monkeypatch.setattr(settings, "play_confirm_backend", "gemini")
    assert understand.default_backend("a1", cap_usd=1.0).name == "gemini"


def test_play_confirm_backend_empty_keeps_sonnet(monkeypatch):
    monkeypatch.setattr(settings, "vlm_backend", "anthropic")
    monkeypatch.setattr(settings, "play_confirm_backend", "")
    be = understand.default_backend("a1", cap_usd=1.0)
    assert be.name == "anthropic"


def test_overloaded_503_waits_then_succeeds(gemini_key, frame):
    replies = iter([httpx.Response(503, text="overloaded"), _ok("{}")])
    waits: list[float] = []
    be = OpenAICompatBackend("gemini", client=_client(lambda r: next(replies)), sleep=waits.append)
    assert be.call(frames=[frame], system_prompt="s", user_prompt="u") == "{}"
    assert len(waits) == 1
