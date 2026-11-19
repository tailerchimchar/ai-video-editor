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

# TODO(taxonomy): replace these raw event_type strings with the
# taxonomy.PayoffType / Topic enums once Task 1 is merged.
_KILLS_PER_EVENT: dict[str, int] = {
    "kill": 1,
    "multi_kill_2": 2,
    "multi_kill_3": 3,
    "multi_kill_4": 4,
    "ace": 5,
    # LoL event names (Riot candidates)
    "doublekill": 2,
    "triplekill": 3,
    "quadrakill": 4,
    "pentakill": 5,
}
_MULTI_KILL_TIER: dict[str, int] = {
    name: kills for name, kills in _KILLS_PER_EVENT.items() if kills > 1
}
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
    kills = sum(_KILLS_PER_EVENT.get(t, 0) for t in types)
    tier = max((_MULTI_KILL_TIER.get(t, 0) for t in types), default=0)
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
        intensity = score_clip(
            [{"event_type": entry.get("event") or entry.get("event_type")}],
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
            )
        )
    return result
