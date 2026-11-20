"""Outbound event model."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Event(BaseModel):
    event: str
    payload: dict = Field(default_factory=dict)
    ts: str = Field(default_factory=_now)
