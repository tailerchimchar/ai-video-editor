"""Outbound events: webhook sink, fan-out and registry. No network."""

from __future__ import annotations

import json

import httpx

from ai_video_editor import notify
from ai_video_editor.config import settings
from ai_video_editor.notify import sinks
from ai_video_editor.notify.models import Event
from ai_video_editor.notify.sinks.webhook import WebhookSink


def _sink(handler, **kw):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return WebhookSink(
        "https://bot.example/hook",
        "k",
        max_per_hour=kw.pop("max_per_hour", 10),
        client=client,
        **kw,
    )


def test_webhook_sink_posts_bearer_json():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200)

    event = Event(event="job.done", payload={"id": 1})
    sink = _sink(handler)
    sink.send(event)
    sink.flush()
    assert len(seen) == 1
    assert str(seen[0].url) == "https://bot.example/hook"
    assert seen[0].headers["Authorization"] == "Bearer k"
    assert json.loads(seen[0].content) == event.model_dump(exclude={"priority"})


def test_webhook_sink_failure_never_raises():
    def handler(request):
        raise httpx.ConnectError("boom")

    sink = _sink(handler)
    assert sink.send(Event(event="x")) is None
    sink.flush()
    assert sink.failed == 1


def test_webhook_sink_rate_cap_folds_extra_events():
    calls = []
    now = [0.0]

    def handler(request):
        calls.append(request)
        return httpx.Response(200)

    sink = _sink(handler, max_per_hour=2, clock=lambda: now[0])
    for _ in range(3):
        now[0] += 10
        sink.send(Event(event="x"))
        sink.flush()
    assert len(calls) == 2
    assert sink.dropped == 1
    now[0] += 3600
    sink.send(Event(event="x"))
    sink.flush()
    assert len(calls) == 3


def test_emit_fans_out_to_every_configured_sink(monkeypatch):
    got = []

    class Fake:
        def __init__(self, tag):
            self.tag = tag

        def send(self, event):
            got.append((self.tag, event.event))

    class Bad:
        def send(self, event):
            raise RuntimeError("nope")

    monkeypatch.setattr(
        sinks,
        "SINKS",
        {
            "a": lambda: Fake("a"),
            "bad": lambda: Bad(),
            "b": lambda: Fake("b"),
            "off": lambda: None,
        },
    )
    assert notify.emit(Event(event="job.done")) is None
    assert got == [("a", "job.done"), ("b", "job.done")]


def test_emit_with_no_sinks_is_a_noop(monkeypatch):
    monkeypatch.setattr(settings, "grokbot_webhook_url", "")
    monkeypatch.setattr(settings, "grokbot_webhook_key", "")
    assert notify.emit(Event(event="heartbeat")) is None


def test_sink_registry_adds_webhook_only_when_url_and_key_set(monkeypatch):
    monkeypatch.setattr(settings, "grokbot_webhook_url", "")
    monkeypatch.setattr(settings, "grokbot_webhook_key", "k")
    assert sinks.SINKS["webhook"]() is None
    monkeypatch.setattr(settings, "grokbot_webhook_url", "https://x")
    monkeypatch.setattr(settings, "grokbot_webhook_key", "")
    assert sinks.SINKS["webhook"]() is None
    monkeypatch.setattr(settings, "grokbot_webhook_key", "k")
    assert isinstance(sinks.SINKS["webhook"](), WebhookSink)
    assert sinks.SINKS["log"]() is not None


def test_webhook_non_2xx_is_logged_and_counted(caplog):
    sink = _sink(lambda request: httpx.Response(401, text="bad key " + "x" * 300))
    with caplog.at_level("WARNING"):
        assert sink.send(Event(event="x")) is None
        sink.flush()
    assert sink.failed == 1
    assert "401" in caplog.text
    assert "bad key" in caplog.text
    assert "Bearer" not in caplog.text
    assert "x" * 250 not in caplog.text


def test_webhook_client_has_overall_timeout(monkeypatch):
    monkeypatch.setattr(settings, "grokbot_timeout_seconds", 7.0)
    t = WebhookSink("https://x", "k", max_per_hour=1).client.timeout
    assert (t.connect, t.read, t.write, t.pool) == (7.0, 7.0, 7.0, 7.0)


def test_webhook_concurrent_sends_respect_cap():
    import threading

    sink = _sink(lambda request: httpx.Response(200), max_per_hour=5, clock=lambda: 1.0)
    threads = [threading.Thread(target=sink.send, args=(Event(event="x"),)) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(sink._sent) == 5
    assert sink.dropped == 35


def test_webhook_factory_returns_single_instance_under_threads(monkeypatch):
    import threading

    monkeypatch.setattr(sinks, "_webhook", None)
    monkeypatch.setattr(settings, "grokbot_webhook_url", "https://x")
    monkeypatch.setattr(settings, "grokbot_webhook_key", "k")
    got = []
    threads = [
        threading.Thread(target=lambda: got.append(sinks.SINKS["webhook"]())) for _ in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(s) for s in got}) == 1


def test_send_returns_before_the_post_completes():
    import threading
    import time

    release = threading.Event()
    calls = []

    def handler(request):
        release.wait(5)
        calls.append(request)
        return httpx.Response(200)

    sink = _sink(handler)
    start = time.monotonic()
    sink.send(Event(event="x"))
    assert time.monotonic() - start < 0.5
    assert calls == []
    release.set()
    sink.flush()
    assert len(calls) == 1


def test_default_test_settings_build_no_webhook_sink():
    assert settings.grokbot_webhook_url == ""
    assert sinks.SINKS["webhook"]() is None


def test_live_env_webhook_is_blanked_by_test_isolation(tmp_path, monkeypatch):
    from conftest import isolate_grokbot

    from ai_video_editor.config import Settings

    fake_env = tmp_path / ".env"
    fake_env.write_text(
        "GROKBOT_WEBHOOK_URL=https://lead.example/hook\n"
        "GROKBOT_WEBHOOK_KEY=live\n"
        f"OUTPLAYED_MEDIA_DIR={tmp_path}\n"
        f"WORKSPACE_DIR={tmp_path}\n",
        encoding="utf-8",
    )
    loaded = Settings(_env_file=fake_env)  # type: ignore[call-arg]
    assert loaded.grokbot_webhook_url == "https://lead.example/hook"
    # Simulate a session whose api/.env held the live webhook.
    monkeypatch.setattr(settings, "grokbot_webhook_url", loaded.grokbot_webhook_url)
    monkeypatch.setattr(settings, "grokbot_webhook_key", loaded.grokbot_webhook_key)
    built = []
    monkeypatch.setattr(sinks, "WebhookSink", lambda *a, **k: built.append(a) or object())
    isolate_grokbot(monkeypatch)
    assert sinks.SINKS["webhook"]() is None
    notify.emit(Event(event="job.done"))
    assert built == []


def test_reset_sinks_drops_cached_webhook(monkeypatch):
    monkeypatch.setattr(settings, "grokbot_webhook_url", "https://x")
    monkeypatch.setattr(settings, "grokbot_webhook_key", "k")
    first = sinks.SINKS["webhook"]()
    sinks.reset_sinks()
    assert sinks._webhook is None
    assert sinks.SINKS["webhook"]() is not first
