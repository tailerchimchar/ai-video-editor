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
    _sink(handler).send(event)
    assert len(seen) == 1
    assert str(seen[0].url) == "https://bot.example/hook"
    assert seen[0].headers["Authorization"] == "Bearer k"
    assert json.loads(seen[0].content) == event.model_dump()


def test_webhook_sink_failure_never_raises():
    def handler(request):
        raise httpx.ConnectError("boom")

    assert _sink(handler).send(Event(event="x")) is None


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
    assert len(calls) == 2
    assert sink.dropped == 1
    now[0] += 3600
    sink.send(Event(event="x"))
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
