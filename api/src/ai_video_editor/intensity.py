"""Clip intensity: a 0-1 score from signals the pipeline already has.

Pure and deterministic (no LLM), so the arc planner and panel brief can
rank clips cheaply. Only `score_session` touches disk (one index.json plus
optional per-clip sidecars), and it never raises on missing or bad data.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from .config import settings
from .taxonomy import kills_for_event

_DEATH_EVENT = "death"


class IntensityWeights(BaseModel):
    """How much each signal contributes. Defaults come from `INTENSITY_*`
    settings, read when the object is built so overrides take effect."""

    kills: float = Field(default_factory=lambda: settings.intensity_kill_weight)
    multi_kill_bonus: dict[int, float] = Field(
        default_factory=lambda: dict(settings.intensity_multi_kill_bonus)
    )
    vlm_payoff: float = Field(default_factory=lambda: settings.intensity_vlm_weight)
    voice: float = Field(default_factory=lambda: settings.intensity_voice_weight)
    death_penalty: float = Field(default_factory=lambda: settings.intensity_death_penalty)


class ClipWithIntensity(BaseModel):
    clip_ref: str
    start: float
    end: float
    intensity: float
    topic: str | None = None
    event_type: str | None = None
    """The index entry's event (`kill`, `multi_kill_3`, `pentakill`, ...)."""


def _event_type(event: dict) -> str:
    return str(event.get("event_type") or event.get("event") or "")


def score_clip(
    events: list[dict],
    transcript_arousal: float | None,
    vlm_payoff: float | None,
    weights: IntensityWeights | None = None,
) -> float:
    """Intensity in [0, 1]. Absent optional signals contribute nothing."""
    w = weights or IntensityWeights()
    types = [_event_type(e) for e in events]
    per_event = [kills_for_event(t) for t in types]
    kills = sum(per_event)
    tier = max((k for k in per_event if k > 1), default=0)
    deaths = types.count(_DEATH_EVENT)

    raw = (
        kills * w.kills
        + w.multi_kill_bonus.get(tier, 0.0)
        + (vlm_payoff or 0.0) * w.vlm_payoff
        + (transcript_arousal or 0.0) * w.voice
        - deaths * w.death_penalty
    )
    return min(1.0, max(0.0, raw))


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _vlm_payoff(clip_path: Path) -> float | None:
    """Confidence of the clip analyzer's primary payoff, read from its
    `<clip>.analysis.json` sidecar. None when absent or not a real payoff."""
    sidecar = _read_json(clip_path.with_suffix(clip_path.suffix + ".analysis.json"))
    analysis = sidecar.get("analysis")
    if not isinstance(analysis, dict) or analysis.get("payoff_type") == "other":
        return None
    return _optional_float(analysis.get("confidence"))


def score_session(index_json_path: Path) -> list[ClipWithIntensity]:
    """Score every rendered clip in a highlights folder's index.json.

    Optional per-clip signals: `sentiment_score` (transcript VADER arousal)
    on the index entry, and the clip analyzer's `<clip>.analysis.json`
    sidecar (see `vlm/clip_analyzer.py`).
    """
    folder = index_json_path.parent
    result: list[ClipWithIntensity] = []
    for entry in _read_json(index_json_path).get("clips") or []:
        file = entry.get("file")
        if not entry.get("ok") or not file:
            continue
        event_type = entry.get("event") or entry.get("event_type")
        intensity = score_clip(
            [{"event_type": event_type}],
            _optional_float(entry.get("sentiment_score")),
            _vlm_payoff(folder / file),
        )
        result.append(
            ClipWithIntensity(
                clip_ref=file,
                start=float(entry.get("start_seconds") or 0),
                end=float(entry.get("end_seconds") or 0),
                intensity=intensity,
                topic=entry.get("topic"),
                event_type=event_type,
            )
        )
    return result
