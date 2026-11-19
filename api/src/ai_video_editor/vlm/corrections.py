"""Corrections memory — the DSPy-style few-shot layer of the anchor+
critic pipeline.

Two questions this module answers:

1. When the user says "this specific thing is wrong" (in chat, via the
   `mentor_record_correction` tool), where does that guidance land so
   the NEXT compile knows about it? Answer: `game_hints/<game>_examples.jsonl`
   — one JSON per line, structured, appendable, git-diffable.

2. How does the VLM see it? Answer: `load_game_hints` picks up the
   examples file automatically, formats the top-K recent entries as a
   `## Recent corrections you've made` section, and appends it to the
   rubric markdown before the prompt is built. No caller changes.

Cache invalidation is content-hash-based: the corrections file's hash
folds into `game_hints_prompt_version` so any new correction bumps the
effective version and invalidates per-clip analyzer sidecars. No manual
version bumps required.

Pure — no LLM calls, no ffmpeg. Callable from tests, HTTP handlers,
MCP tools alike.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_HINTS_DIR = Path(__file__).parent / "game_hints"


@dataclass
class Correction:
    """One structured user correction. Stored as one JSON line."""

    added_at: str  # ISO-8601 UTC
    feedback: str  # what the user said
    rule: str  # the rule the LLM should follow next time
    clip_ref: str | None = None  # e.g. filename or short-name (may30_captioned_v3)
    # optional extra grounding (rendered video path, session id, ...)
    source_context: str | None = None
    tags: list[str] = field(default_factory=list)  # freeform for future filtering


def _examples_path(game: str) -> Path:
    """`game_hints/<game_lower>_examples.jsonl`. Never raises — the file
    doesn't have to exist. Empty / missing → no examples injected."""
    return _HINTS_DIR / f"{game.lower().strip()}_examples.jsonl"


def load_recent_corrections(game: str, limit: int = 10) -> list[Correction]:
    """Read the most recent `limit` corrections for `game`. Newest first
    by FILE ORDER — last-appended line wins, regardless of the `added_at`
    timestamp. Robust to same-microsecond appends and clock skew.

    Silently drops malformed lines so one bad append doesn't kneecap
    the whole prompt.
    """
    path = _examples_path(game)
    if not path.is_file():
        return []
    out: list[Correction] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            out.append(Correction(**data))
        except (json.JSONDecodeError, TypeError):
            continue
    # Reverse so newest (last appended) is first, then cap.
    return list(reversed(out))[:limit]


def append_correction(
    game: str,
    *,
    feedback: str,
    rule: str,
    clip_ref: str | None = None,
    source_context: str | None = None,
    tags: list[str] | None = None,
) -> Correction:
    """Append one correction to the game's examples file. Creates the
    file if missing. Returns the stored `Correction` so the caller can
    echo it back to the user."""
    correction = Correction(
        added_at=datetime.now(timezone.utc).isoformat(),
        feedback=feedback.strip(),
        rule=rule.strip(),
        clip_ref=clip_ref.strip() if clip_ref else None,
        source_context=source_context.strip() if source_context else None,
        tags=list(tags or []),
    )
    path = _examples_path(game)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(correction), ensure_ascii=False) + "\n")
    return correction


def render_corrections_section(corrections: list[Correction]) -> str:
    """Format top-K corrections as markdown for injection into the
    game-hints prompt. Empty string when there are no corrections (so
    callers can unconditionally concatenate)."""
    if not corrections:
        return ""
    lines = [
        "",
        "## Recent corrections you've received (most recent first)",
        "",
        (
            "These are specific edits the user has flagged from prior "
            "compiles. Apply them BEFORE anything else in this rubric — "
            "they override defaults when they conflict."
        ),
        "",
    ]
    for i, c in enumerate(corrections, 1):
        lines.append(f"{i}. **Rule:** {c.rule}")
        lines.append(f"   - Feedback: {c.feedback}")
        if c.clip_ref:
            lines.append(f"   - Clip: `{c.clip_ref}`")
        if c.tags:
            lines.append(f"   - Tags: {', '.join(c.tags)}")
        lines.append(f"   - Added: {c.added_at[:10]}")
        lines.append("")
    return "\n".join(lines)


def examples_hash(game: str) -> str:
    """SHA1 hex of the examples file bytes. Empty string when file is
    missing. Used to invalidate per-clip analyzer caches: any append
    changes the hash → bumped effective `PROMPT_VERSION`."""
    path = _examples_path(game)
    if not path.is_file():
        return ""
    return hashlib.sha1(path.read_bytes()).hexdigest()[:12]
