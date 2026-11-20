"""Log sink: every event at INFO."""

from __future__ import annotations

import logging

from ..models import Event

logger = logging.getLogger(__name__)


class LogSink:
    def send(self, event: Event) -> None:
        logger.info("event %s %s", event.event, event.payload)
