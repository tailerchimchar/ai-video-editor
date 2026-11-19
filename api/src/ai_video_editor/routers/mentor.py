"""Mentor router — the memory flywheel.

`POST /mentor/record_correction` — user or Claude appends a structured
editorial correction. Persisted to `vlm/game_hints/<game>_examples.jsonl`;
the next compile picks it up automatically via `load_game_hints` and
its hash rolls into `game_hints_effective_version` so cached analyzer
sidecars invalidate cleanly.

`GET /mentor/recent_corrections` — inspect what's been recorded, so
the user can review + curate before it drives future compiles.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..vlm.corrections import (
    append_correction,
    load_recent_corrections,
)

router = APIRouter(tags=["mentor"], prefix="/mentor")


class RecordCorrectionBody(BaseModel):
    game: str = Field(
        ...,
        description=(
            "Game the correction applies to (e.g. 'valorant', 'league'). "
            "Case-insensitive. The correction is scoped to this game's "
            "rubric and won't leak into other games' compiles."
        ),
    )
    feedback: str = Field(
        ...,
        min_length=3,
        description="Verbatim what the user said (short).",
    )
    rule: str = Field(
        ...,
        min_length=3,
        description=(
            "The rule the LLM should follow next time. Actionable, "
            "specific. Bad: 'be better at pacing'. Good: 'sub-clip "
            "must include the killfeed pop within 1s of the frag'."
        ),
    )
    clip_ref: str | None = Field(
        default=None,
        description=(
            "Optional clip identifier (filename, session id, short-name) "
            "so future audits can trace which output triggered the rule."
        ),
    )
    source_context: str | None = Field(default=None)
    tags: list[str] | None = Field(default=None)


@router.post("/record_correction")
async def record_correction(body: RecordCorrectionBody) -> dict:
    """Append a structured correction to the game's examples file.
    Idempotent-ish — duplicate appends are allowed (recency wins in
    the loader's top-K cut) but the caller should dedupe when in doubt.
    """
    correction = append_correction(
        body.game,
        feedback=body.feedback,
        rule=body.rule,
        clip_ref=body.clip_ref,
        source_context=body.source_context,
        tags=body.tags,
    )
    return {
        "ok": True,
        "game": body.game.lower(),
        "correction": {
            "added_at": correction.added_at,
            "feedback": correction.feedback,
            "rule": correction.rule,
            "clip_ref": correction.clip_ref,
            "tags": correction.tags,
        },
    }


@router.get("/recent_corrections")
async def recent_corrections(game: str, limit: int = 10) -> dict:
    """List the most recent corrections for `game`, newest first.
    Empty list when the file is missing / no corrections yet."""
    if limit < 1 or limit > 100:
        raise HTTPException(400, "limit must be 1..100")
    corrections = load_recent_corrections(game, limit=limit)
    return {
        "ok": True,
        "game": game.lower(),
        "count": len(corrections),
        "corrections": [
            {
                "added_at": c.added_at,
                "feedback": c.feedback,
                "rule": c.rule,
                "clip_ref": c.clip_ref,
                "source_context": c.source_context,
                "tags": c.tags,
            }
            for c in corrections
        ],
    }
