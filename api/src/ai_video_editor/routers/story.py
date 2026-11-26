"""Story requests and story re-render, drivable from chat.

    GET    /story/{round}/requests          list the owner's requests (0-based index)
    POST   /story/{round}/requests          append one request (validated)
    DELETE /story/{round}/requests/{index}  remove one request
    POST   /story/{round}/render            background job: same path as `story-render`

Requests live in `WORKSPACE/story/<round>/requests.json` (story/requests.py).
The render job emits `job.done` through `finish_job` like every job.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import settings
from ..database import get_db
from ..jobs import finish_job
from ..models import RankResponse
from ..story.cli import render_round
from ..story.requests import OwnerRequest, StoryRequests, load_requests, save_requests

router = APIRouter(tags=["story"], prefix="/story")

_ROUND_RE = re.compile(r"^[0-9A-Za-z_\-]+$")
_background_tasks: set[asyncio.Task] = set()


def _track(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _story_dir(round: str) -> Path:
    if not _ROUND_RE.match(round):
        raise HTTPException(422, "round may only contain letters, digits, '_' and '-'")
    ws = Path(settings.workspace_dir)
    story = ws / "story" / round
    if not (story.is_dir() or (ws / "panel" / round).is_dir()):
        raise HTTPException(404, f"Unknown round: {round}")
    return story


def _load(story: Path) -> StoryRequests:
    try:
        return load_requests(story)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


def _listing(round: str, reqs: StoryRequests) -> dict:
    return {
        "round": round,
        "requests": [
            {"index": i, **r.model_dump(mode="json", exclude_none=True)}
            for i, r in enumerate(reqs.requests)
        ],
    }


@router.get("/{round}/requests")
async def list_requests(round: str) -> dict:
    return _listing(round, _load(_story_dir(round)))


@router.post("/{round}/requests")
async def add_request(round: str, body: OwnerRequest) -> dict:
    story = _story_dir(round)
    reqs = _load(story)
    reqs.requests.append(body)
    save_requests(story, reqs)
    return _listing(round, reqs)


@router.delete("/{round}/requests/{index}")
async def remove_request(round: str, index: int) -> dict:
    story = _story_dir(round)
    reqs = _load(story)
    if not 0 <= index < len(reqs.requests):
        raise HTTPException(404, f"No request at index {index} ({len(reqs.requests)} stored)")
    del reqs.requests[index]
    save_requests(story, reqs)
    return _listing(round, reqs)


class RenderBody(BaseModel):
    asset_id: str
    plays: bool = True


async def _run_render_job(job_id: str, asset_id: str, round: str, plays: bool) -> None:
    db = await get_db()
    try:
        await db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        await db.commit()
        out = await asyncio.to_thread(render_round, asset_id, round, plays=plays)
        await finish_job(db, job_id, output_path=str(out), job_type="story_render")
    except Exception as exc:
        with contextlib.suppress(Exception):
            await finish_job(db, job_id, error=str(exc)[:1500], job_type="story_render")
    finally:
        await db.close()


@router.post("/{round}/render", response_model=RankResponse)
async def render(round: str, body: RenderBody) -> RankResponse:
    """Re-render the round's story with its current requests. Job-based."""
    _story_dir(round)
    job_id = str(uuid.uuid4())
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, 'story_render', 'pending', ?)",
            (job_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()
    _track(_run_render_job(job_id, body.asset_id, round, body.plays))
    return RankResponse(job_id=job_id)
