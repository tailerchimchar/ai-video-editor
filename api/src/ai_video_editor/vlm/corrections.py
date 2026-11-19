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

Cache invalidation: `effective_prompt_version` hashes `RUBRIC_VERSION`,
the game hint file and ONLY the top-K corrections actually rendered into
the prompt. A correction older than the window changes no prompt text, so
it must not re-bill the analyzed library; one entering the window does.
Bump `RUBRIC_VERSION` when the critic rubric text changes.

Pure — no LLM calls, no ffmpeg. Callable from tests, HTTP handlers,
MCP tools alike.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from ..config import settings
from . import hint_files

# Bump when the rubric/critic wording changes outside the hint files.
RUBRIC_VERSION = "1"

Verdict = Literal["payoff", "false_positive", "style"]


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
    # Typed fields: a gaming-expert dictionary term, the call made on the
    # clip, and a concrete example. Optional so legacy rows still load.
    concept: str | None = None
    verdict: Verdict | None = None
    example: str | None = None


def _examples_path(game: str) -> Path:
    """`game_hints/<game_lower>_examples.jsonl`. Never raises — the file
    doesn't have to exist. Empty / missing → no examples injected."""
    return hint_files.HINTS_DIR / f"{game.lower().strip()}_examples.jsonl"


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
    concept: str | None = None,
    verdict: Verdict | None = None,
    example: str | None = None,
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
        concept=concept.strip() if concept else None,
        verdict=verdict,
        example=example.strip() if example else None,
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
        if c.concept:
            lines.append(f"   - Concept: {c.concept}")
        if c.verdict:
            lines.append(f"   - Verdict: {c.verdict}")
        if c.example:
            lines.append(f"   - Example: {c.example}")
        if c.tags:
            lines.append(f"   - Tags: {', '.join(c.tags)}")
        lines.append(f"   - Added: {c.added_at[:10]}")
        lines.append("")
    return "\n".join(lines)


def effective_prompt_version(game: str | None) -> str:
    """Cache key for per-clip VLM sidecars: hash of `RUBRIC_VERSION`, the
    game hint file bytes and the top-K rendered correction rows only.
    Rows outside the window and null typed fields render nothing, so
    neither appends beyond the window nor the typed migration move it."""
    path, resolved = hint_files.resolve_hint_file(game)
    digest = hashlib.sha1(RUBRIC_VERSION.encode("utf-8"))
    digest.update(path.read_bytes() if path else b"")
    if game and resolved not in ("_missing", "_default"):
        top_k = load_recent_corrections(resolved, limit=settings.corrections_top_k)
        digest.update(render_corrections_section(top_k).encode("utf-8"))
    return digest.hexdigest()[:12]
