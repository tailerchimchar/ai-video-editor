"""Per-video cost logging.

Records each model call (model name, input/output tokens or frame count,
and dollars when the client reports them). Unknown costs are marked as
null — never estimated silently.

Targets for reference (not enforced here):
  - ~$0.05 per short
  - ~$0.20 per 10-minute video

Usage:
    cost_log = CostLog(video_id="abc123")
    cost_log.record_call(
        model="claude-haiku-4-5",
        input_tokens=1500,
        output_tokens=200,
        cost_dollars=0.002,
    )
    cost_log.record_call(
        model="jev-latest",
        input_tokens=800,
        cost_dollars=None,  # Jev doesn't report cost
    )
    cost_log.emit()  # structured log line
    result = cost_log.to_dict()  # attach to run result
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger(__name__)


@dataclass
class ModelCall:
    """One model call record."""

    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    frame_count: int | None = None
    cost_dollars: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"model": self.model}
        if self.input_tokens is not None:
            d["input_tokens"] = self.input_tokens
        if self.output_tokens is not None:
            d["output_tokens"] = self.output_tokens
        if self.frame_count is not None:
            d["frame_count"] = self.frame_count
        d["cost_dollars"] = self.cost_dollars
        return d


@dataclass
class CostLog:
    """Per-video/run cost record. Accumulates model calls and emits a
    structured log line when done."""

    video_id: str | None = None
    run_id: str | None = None
    calls: list[ModelCall] = field(default_factory=list)

    def record_call(
        self,
        model: str,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        frame_count: int | None = None,
        cost_dollars: float | None = None,
    ) -> None:
        """Record one model call. cost_dollars should be None if unknown
        — never estimate silently."""
        self.calls.append(
            ModelCall(
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                frame_count=frame_count,
                cost_dollars=cost_dollars,
            )
        )

    @property
    def total_cost_dollars(self) -> float | None:
        """Sum of all known costs. Returns None if any call has unknown cost."""
        if not self.calls:
            return 0.0
        costs = [c.cost_dollars for c in self.calls]
        if any(c is None for c in costs):
            return None
        return sum(c for c in costs if c is not None)

    @property
    def total_input_tokens(self) -> int:
        return sum(c.input_tokens or 0 for c in self.calls)

    @property
    def total_output_tokens(self) -> int:
        return sum(c.output_tokens or 0 for c in self.calls)

    @property
    def total_frames(self) -> int:
        return sum(c.frame_count or 0 for c in self.calls)

    def to_dict(self) -> dict[str, Any]:
        """Return a dict suitable for attaching to run results."""
        return {
            "video_id": self.video_id,
            "run_id": self.run_id,
            "calls": [c.to_dict() for c in self.calls],
            "summary": {
                "total_calls": len(self.calls),
                "total_input_tokens": self.total_input_tokens,
                "total_output_tokens": self.total_output_tokens,
                "total_frames": self.total_frames,
                "total_cost_dollars": self.total_cost_dollars,
            },
        }

    def emit(self) -> None:
        """Emit a structured log line with the cost record."""
        record = self.to_dict()
        _log.info(
            "cost_record: video_id=%s run_id=%s calls=%d total_cost=%s | %s",
            self.video_id,
            self.run_id,
            len(self.calls),
            self.total_cost_dollars,
            json.dumps(record, default=str),
        )

    def merge(self, other: CostLog) -> None:
        """Merge another CostLog's calls into this one."""
        self.calls.extend(other.calls)
