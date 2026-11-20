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
class CascadeMetrics:
    """Cascade-specific metrics for the cost record."""

    total_candidates: int = 0
    jev_decided: int = 0
    jev_kept: int = 0
    jev_rejected: int = 0
    escalated_unsure: int = 0
    timing_refine: int = 0
    escalation_rate: float = 0.0
    haiku_candidates_saved: int = 0
    haiku_call_skipped: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_candidates": self.total_candidates,
            "jev_decided": self.jev_decided,
            "jev_kept": self.jev_kept,
            "jev_rejected": self.jev_rejected,
            "escalated_unsure": self.escalated_unsure,
            "timing_refine": self.timing_refine,
            "escalation_rate": round(self.escalation_rate, 4),
            "haiku_candidates_saved": self.haiku_candidates_saved,
            "haiku_call_skipped": self.haiku_call_skipped,
        }


@dataclass
class CostLog:
    """Per-video/run cost record. Accumulates model calls and emits a
    structured log line when done."""

    video_id: str | None = None
    run_id: str | None = None
    calls: list[ModelCall] = field(default_factory=list)
    cascade_metrics: CascadeMetrics | None = None

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

    def set_cascade_metrics(
        self,
        *,
        total_candidates: int,
        jev_decided: int,
        jev_kept: int,
        jev_rejected: int,
        escalated_unsure: int,
        timing_refine: int,
        escalation_rate: float,
        haiku_candidates_saved: int,
        haiku_call_skipped: int,
    ) -> None:
        """Set cascade-specific metrics for the cost record."""
        self.cascade_metrics = CascadeMetrics(
            total_candidates=total_candidates,
            jev_decided=jev_decided,
            jev_kept=jev_kept,
            jev_rejected=jev_rejected,
            escalated_unsure=escalated_unsure,
            timing_refine=timing_refine,
            escalation_rate=escalation_rate,
            haiku_candidates_saved=haiku_candidates_saved,
            haiku_call_skipped=haiku_call_skipped,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a dict suitable for attaching to run results."""
        result: dict[str, Any] = {
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
        if self.cascade_metrics is not None:
            result["cascade"] = self.cascade_metrics.to_dict()
        return result

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
