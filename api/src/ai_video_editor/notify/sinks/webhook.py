"""Webhook sink: POST the event as JSON with a Bearer key.

Never raises. A sliding one-hour window caps sends; events over the cap
are dropped and counted in `dropped` (surfaced by the heartbeat).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable

import httpx

from ...config import settings
from ..models import Event

logger = logging.getLogger(__name__)

_WINDOW_SECONDS = 3600.0


class WebhookSink:
    def __init__(
        self,
        url: str,
        key: str,
        *,
        max_per_hour: int,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = url
        self.key = key
        self.max_per_hour = max_per_hour
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(settings.grokbot_timeout_seconds)
        )
        self.clock = clock
        self.dropped = 0
        self.failed = 0
        self._lock = threading.Lock()
        self._sent: deque[float] = deque()

    def send(self, event: Event) -> None:
        with self._lock:
            now = self.clock()
            while self._sent and now - self._sent[0] >= _WINDOW_SECONDS:
                self._sent.popleft()
            if len(self._sent) >= self.max_per_hour:
                self.dropped += 1
                return
            self._sent.append(now)
        try:
            response = self.client.post(
                self.url,
                json=event.model_dump(),
                headers={"Authorization": f"Bearer {self.key}"},
            )
        except Exception as exc:
            with self._lock:
                self.failed += 1
            logger.warning("webhook send failed: %s", exc)
            return
        if response.status_code >= 300:
            with self._lock:
                self.failed += 1
            logger.warning("webhook returned %s: %s", response.status_code, response.text[:200])
