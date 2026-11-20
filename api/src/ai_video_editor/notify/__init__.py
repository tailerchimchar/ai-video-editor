"""Outbound events. `emit(Event(...))` fans out to every configured sink."""

from __future__ import annotations

import logging

from . import sinks
from .models import Event

logger = logging.getLogger(__name__)

__all__ = ["Event", "emit"]


def emit(event: Event) -> None:
    """Send to every configured sink. Never raises."""
    try:
        factories = list(sinks.SINKS.items())
    except Exception as exc:
        logger.warning("notify registry failed: %s", exc)
        return
    for name, factory in factories:
        try:
            sink = factory()
            if sink is not None:
                sink.send(event)
        except Exception as exc:
            logger.warning("notify sink %s failed: %s", name, exc)
