"""Sink registry. A new sink is one new file here plus one entry.

A sink is `send(event) -> None`. Each factory returns None when the sink
is unconfigured, so an unconfigured sink is simply skipped.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ...config import settings
from ..models import Event
from .log import LogSink
from .webhook import WebhookSink


class Sink(Protocol):
    def send(self, event: Event) -> None: ...


_webhook: WebhookSink | None = None


def _log() -> Sink | None:
    return LogSink()


def _webhook_factory() -> Sink | None:
    """Cached per (url, key) so the rate-cap window persists across emits."""
    global _webhook
    url, key = settings.grokbot_webhook_url, settings.grokbot_webhook_key
    if not url or not key:
        return None
    if _webhook is None or (_webhook.url, _webhook.key) != (url, key):
        _webhook = WebhookSink(url, key, max_per_hour=settings.grokbot_max_events_per_hour)
    return _webhook


SINKS: dict[str, Callable[[], Sink | None]] = {
    "log": _log,
    "webhook": _webhook_factory,
}
