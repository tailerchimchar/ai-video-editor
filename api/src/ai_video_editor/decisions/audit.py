"""Local audit log of every typed-decision answer.

Jev answers keep/format questions nobody can audit after the fact; this
appends one JSON line per answer to `<workspace>/decisions/jev-<date>.jsonl`
so a scorecard can later compare them with the owner's corrections.
Logging must never change what `ask_many` returns, so it never raises.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import settings

_log = logging.getLogger(__name__)

EXCERPT_CHARS = 600
_DROPPED_KEYS = frozenset({"text", "transcript", "words", "voice"})


@dataclass
class AnswerRecord:
    ts: str
    backend: str
    question: str
    state_hash: str
    state_excerpt: dict
    value: Any
    probability: float | None
    clip_ref: str | None
    # Round / asset id when the state carries one, so the scorecard can join
    # on (round, ref) instead of the bare NmNNs ref that repeats across games.
    round: str | None = None


def _strip(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k not in _DROPPED_KEYS}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def state_hash(state: dict) -> str:
    raw = json.dumps(state, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def state_excerpt(state: dict) -> dict:
    """State minus transcript-like keys, serialised and cut to 600 chars.
    A cut excerpt is no longer valid JSON, so it is kept as `{"excerpt": str}`."""
    text = json.dumps(_strip(state), default=str, ensure_ascii=False)
    if len(text) <= EXCERPT_CHARS:
        return json.loads(text)
    return {"excerpt": text[:EXCERPT_CHARS]}


def clip_ref(state: dict) -> str | None:
    clip = state.get("clip")
    candidate = state.get("candidate")
    ref = clip.get("ref") if isinstance(clip, dict) else None
    if ref is None and isinstance(candidate, dict):
        ref = candidate.get("id")
    return None if ref is None else str(ref)


def round_of(state: dict) -> str | None:
    clip = state.get("clip")
    value = clip.get("round") if isinstance(clip, dict) else None
    if value is None:
        value = state.get("round")
    return None if value is None else str(value)


def make_record(backend: str, question: str, state: dict, answer: Any) -> AnswerRecord:
    value = getattr(answer.value, "value", answer.value)  # enum member -> its value
    return AnswerRecord(
        ts=datetime.now(timezone.utc).isoformat(),
        backend=backend,
        question=question,
        state_hash=state_hash(state),
        state_excerpt=state_excerpt(state),
        value=value,
        probability=answer.probability,
        clip_ref=clip_ref(state),
        round=round_of(state),
    )


def log_answers(records: list[AnswerRecord], *, log_dir: Path | None = None) -> None:
    """Append one JSON line per record. Never raises."""
    if not records:
        return
    try:
        directory = log_dir or settings.decisions_log_dir or settings.workspace_dir / "decisions"
        directory.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lines = "".join(
            json.dumps(asdict(r), default=str, ensure_ascii=False) + "\n" for r in records
        )
        with (directory / f"jev-{day}.jsonl").open("a", encoding="utf-8") as f:
            f.write(lines)
    except Exception as exc:  # the audit must never break a decision
        _log.warning("decisions: could not write audit log (%s)", exc)
