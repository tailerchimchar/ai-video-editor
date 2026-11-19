"""Content-type router -- given a session's ranked clips, route each
to one of the four mentor buckets: `hype`, `story`, `voice_over`,
`funny`.

Pure and deterministic (no LLM). The `event_type` alone is enough
signal for 3 of 4 buckets; `story` is left as a manual-pick bucket
until we build round-boundary detection.

Routing rules (see `~/.claude/skills/mentor/SKILL.md` for authority):

- **hype**: aces, 4Ks, 3Ks -- the "showreel" material. Anything below
  a triple kill is disqualified from hype per mentor rule 1.
- **funny**: laughter cues and funny_callout from the transcript.
- **voice_over**: single kills (1-2K plays), hype_callouts, high-arousal
  speech without a callout. The "solo queue moments" pool.
- **story**: reserved for user picks. A single round with narrative arc
  is not detectable from the current candidate metadata alone -- needs
  round-boundary detection to auto-route here.

The output is a manifest per bucket the user (or the mentor skill) can
consume to drive per-bucket compile calls.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import settings
from .decisions import Answer, BoolQuestion, ChoiceQuestion, ask_many
from .taxonomy import Format, PayoffType, kills_for_event

_log = logging.getLogger(__name__)


# Kill events route by kill count (`taxonomy.kills_for_event`, which folds
# League / Valorant names): a 3K and up -- ace, quadra, penta, the
# overwolf_events multi-kill clusters -- is hype; a 1K/2K is voice-over
# material per mentor rule 1, never hype.
_HYPE_MIN_KILLS = 3

# Non-kill events that AUTOMATICALLY route to hype regardless of
# hype_score (mentor rule: 1v3+ clutches qualify for hype).
_HYPE_EVENT_TYPES = frozenset({PayoffType.clutch})

# Event types for the funny bucket.
_FUNNY_EVENT_TYPES = frozenset(
    {
        "funny_callout",
        "funny_audio",
    }
)

# Non-kill event types for the voice-over bucket. Hype callouts +
# assists end up here alongside 1K/2Ks -- material Noodlz might talk over.
_VOICE_OVER_EVENT_TYPES = frozenset(
    {
        "assist",
        "hype_callout",
        "audio_peak",  # loud moments without a visible payoff
        "objective",  # spike plants without a frag
        "death",  # own death, could be a "how I threw" storytelling
        "victory",  # round-win / match-win screens -- reel closer material
        "unknown",
        "clip",  # legacy generic label when event_type was dropped
    }
)

# Bucket names -- align with the mentor skill's taxonomy.
BUCKETS = (Format.hype, Format.story, Format.voice_over, Format.funny)
SKIPPED = "skipped"


@dataclass(frozen=True)
class BucketPick:
    """One clip's placement in a bucket."""

    file: str
    """Clip filename inside the session folder."""

    event_type: str
    hype_score: float
    funny_score: float
    story_score: float
    reason: str
    """The rule that put this clip here (for debugging)."""


@dataclass
class BucketManifest:
    """Routing result for one session."""

    session: str
    hype: list[BucketPick] = field(default_factory=list)
    story: list[BucketPick] = field(default_factory=list)
    voice_over: list[BucketPick] = field(default_factory=list)
    funny: list[BucketPick] = field(default_factory=list)
    skipped: list[BucketPick] = field(default_factory=list)
    """Clips whose event_type didn't match any bucket (rare, but not
    silent -- surfaced so the caller notices new event types)."""

    def total_routed(self) -> int:
        return len(self.hype) + len(self.story) + len(self.voice_over) + len(self.funny)


def route_clip(clip: dict) -> tuple[Format | str, str]:
    """Decide the bucket for ONE clip. Pure.

    Returns `(bucket_name, reason)`. Bucket is one of `BUCKETS` or the
    `SKIPPED` when no rule matched. Bucket names match
    BucketManifest fields.
    """
    event = (clip.get("event") or clip.get("event_type") or "").lower()
    hype_score = float(clip.get("hype_score") or 0.0)
    funny_score = float(clip.get("funny_score") or 0.0)
    kills = kills_for_event(event)

    if kills >= _HYPE_MIN_KILLS or event in _HYPE_EVENT_TYPES:
        return Format.hype, f"event_type={event} -> hype (multi-kill / ace)"
    if event in _FUNNY_EVENT_TYPES:
        return Format.funny, f"event_type={event} -> funny"
    # High funny_score can push a `kill` into funny territory if the
    # ranker thought the moment was silly (e.g. an unusual weapon
    # or a whiff-then-hit sequence). Threshold tuned conservatively --
    # kills usually stay in voice_over unless funny is dominant.
    if funny_score >= 0.8 and hype_score < 0.7:
        return Format.funny, f"funny_score={funny_score:.2f} -> funny override"
    if kills or event in _VOICE_OVER_EVENT_TYPES:
        return Format.voice_over, f"event_type={event} -> voice_over pool"
    return SKIPPED, f"event_type={event!r} did not match any bucket"


def _pick_from_clip(clip: dict, reason: str) -> BucketPick:
    return BucketPick(
        file=clip.get("file") or "",
        event_type=(clip.get("event") or clip.get("event_type") or ""),
        hype_score=float(clip.get("hype_score") or 0.0),
        funny_score=float(clip.get("funny_score") or 0.0),
        story_score=float(clip.get("story_score") or 0.0),
        reason=reason,
    )


def route_session_index(index_json: dict) -> BucketManifest:
    """Route a session by its `index.json`. Pure -- no filesystem.

    `index_json` is a highlights folder's `index.json` shape (as
    written by `highlights.write_index_files`). Only successfully-cut
    clips (`ok: True`) are routed.
    """
    session = index_json.get("folder", "")
    if session:
        session = Path(session).name
    manifest = BucketManifest(session=session)
    for clip in index_json.get("clips", []) or []:
        if not clip.get("ok"):
            continue
        if not clip.get("file"):
            continue
        bucket, reason = route_clip(clip)
        pick = _pick_from_clip(clip, reason)
        getattr(manifest, bucket).append(pick)
    return manifest


def route_session_folder(session_folder: Path) -> BucketManifest | None:
    """Load `index.json` from `session_folder` and route. Returns None
    on missing / unreadable index."""
    idx_path = session_folder / "index.json"
    if not idx_path.is_file():
        _log.info("bucketing: no index.json in %s", session_folder)
        return None
    try:
        data = json.loads(idx_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("bucketing: bad index.json in %s: %s", session_folder, exc)
        return None
    return route_session_index(data)


_INCLUDE_QUESTION = BoolQuestion(
    prompt=(
        "Is this candidate moment worth keeping for a gameplay highlight "
        "reel (skill, hype, humor or a story beat), rather than noise?"
    )
)


def _include_probability(answer: Answer) -> float:
    """P(include). A BoolQuestion answer's probability is confidence in
    its own value, so a confident "no" is a low include-probability."""
    if answer.value is None:
        return 1.0
    return answer.probability if answer.value else 1.0 - answer.probability


def prefilter_candidates(
    candidates: list[dict], ask_many: Callable[..., list[Answer]] = ask_many
) -> tuple[list[dict], list[dict]]:
    """Split candidates into (kept, dropped) before any agent or VLM call.

    A candidate is dropped when its include-probability is below
    `PREFILTER_MIN_PROBABILITY`. Dropped copies carry
    `prefilter_probability` so the decision stays auditable. One batched
    ask for the whole list; with the rules backend everything is kept.
    """
    answers = ask_many([(_INCLUDE_QUESTION, {"candidate": c}) for c in candidates])
    kept: list[dict] = []
    dropped: list[dict] = []
    for candidate, answer in zip(candidates, answers, strict=True):
        probability = _include_probability(answer)
        if probability >= settings.prefilter_min_probability:
            kept.append(candidate)
        else:
            dropped.append({**candidate, "prefilter_probability": round(probability, 4)})
    return kept, dropped


_FORMAT_QUESTION = ChoiceQuestion(
    prompt="Which editorial format fits this clip: hype, story, voice_over, funny or arc_montage?",
    options=Format,
)


def classify_formats(
    clips: list[dict], ask_many: Callable[..., list[Answer]] = ask_many
) -> list[Format | str]:
    """Format for each clip via one batched ask. Falls back to `SKIPPED`
    when the backend has no opinion."""
    answers = ask_many([(_FORMAT_QUESTION, {"clip": clip}) for clip in clips])
    return [a.value if a.value is not None else SKIPPED for a in answers]
