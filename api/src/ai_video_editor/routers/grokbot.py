"""Grokbot API endpoints for box render jobs.

These endpoints bridge the MCP tools to the queue repo for render jobs
that run on a headless Linux box.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import settings
from ..grokbot.queue import _HOSTILE

router = APIRouter(prefix="/grokbot", tags=["grokbot"])


class RenderClipInput(BaseModel):
    clip_ref: str
    drive_id: str
    start_s: float = 0.0
    end_s: float = 0.0


class SubmitRenderRequest(BaseModel):
    asset_id: str
    edit_plan_id: str
    edit_plan_version: str
    game: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    clips: list[RenderClipInput]
    concat_order: list[str]
    music_song_id: str | None = None


@router.post("/render")
async def submit_render(req: SubmitRenderRequest) -> dict:
    """Submit a render job to the queue for the box.

    Writes a render_request item to the queue repo. The box picks it up,
    renders both 9:16 and 16:9 outputs, uploads to Drive, and emits
    video.rendered.
    """
    if not settings.grokbot_queue_repo_dir:
        raise HTTPException(503, "queue repo not configured")

    queue_dir = Path(settings.grokbot_queue_repo_dir) / "queue"
    if not queue_dir.is_dir():
        raise HTTPException(503, "queue directory not found")

    for field_name, value in [
        ("asset_id", req.asset_id),
        ("edit_plan_id", req.edit_plan_id),
        ("edit_plan_version", req.edit_plan_version),
        ("game", req.game),
    ]:
        if _HOSTILE.search(value):
            raise HTTPException(400, f"hostile string in {field_name}")

    for clip in req.clips:
        if _HOSTILE.search(clip.drive_id) or _HOSTILE.search(clip.clip_ref):
            raise HTTPException(400, "hostile string in clip")

    for ref in req.concat_order:
        if _HOSTILE.search(ref):
            raise HTTPException(400, "hostile string in concat_order")

    item_id = str(uuid.uuid4())
    item = {
        "id": item_id,
        "kind": "render_request",
        "author_bot": "editor",
        "created": datetime.now(timezone.utc).isoformat(),
        "schema_version": 1,
        "asset_id": req.asset_id,
        "edit_plan_id": req.edit_plan_id,
        "edit_plan_version": req.edit_plan_version,
        "game": req.game,
        "clips": [
            {
                "clip_ref": c.clip_ref,
                "drive_id": c.drive_id,
                "start_s": c.start_s,
                "end_s": c.end_s,
            }
            for c in req.clips
        ],
        "concat_order": req.concat_order,
        "music_song_id": req.music_song_id,
    }

    item_path = queue_dir / f"{item_id}.json"
    try:
        item_path.write_text(json.dumps(item, indent=2), encoding="utf-8")
    except OSError as e:
        raise HTTPException(500, f"failed to write queue item: {e}") from e

    return {"id": item_id, "status": "queued"}
