"""Story requests and story re-render, drivable from chat.

    GET    /story/{round}/requests          list the owner's requests (0-based index)
    POST   /story/{round}/requests          append one request (validated)
    DELETE /story/{round}/requests/{index}  remove one request
    POST   /story/{round}/render            background job: same path as `story-render`
    GET    /story/{round}/suggestions       the editor suggestions (404 before the first run)
    POST   /story/{round}/suggestions       background job `editor_suggest` (Jev panel)
    PUT    /story/{round}/suggestions/{id}  {accepted: true|false|null}
    POST   /story/{round}/suggestions/apply accepted -> effect requests, then a story_render job

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
from ..story.suggest.apply import apply_accepted
from ..story.suggest.models import SuggestionSet
from ..story.suggest.service import (
    STORY_FILE,
    SuggestInputError,
    now_iso,
    run_suggestions,
    story_asset_id,
)
from ..story.suggest.store import SUGGESTIONS_FILE, FileSuggestionStore, UnknownSuggestionError

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


async def _insert_job(job_type: str) -> str:
    job_id = str(uuid.uuid4())
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, ?, 'pending', ?)",
            (job_id, job_type, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()
    return job_id


async def _start_render(asset_id: str, round: str, plays: bool) -> RankResponse:
    job_id = await _insert_job("story_render")
    _track(_run_render_job(job_id, asset_id, round, plays))
    return RankResponse(job_id=job_id)


@router.post("/{round}/render", response_model=RankResponse)
async def render(round: str, body: RenderBody) -> RankResponse:
    """Re-render the round's story with its current requests. Job-based."""
    _story_dir(round)
    return await _start_render(body.asset_id, round, body.plays)


# ---- editor suggestions (story/suggest, docs/story-suggestions.md) -----------


def _suggestions(story: Path) -> SuggestionSet:
    try:
        found = FileSuggestionStore(story).load()
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if found is None:
        raise HTTPException(404, "No suggestions for this round yet: POST to start the job")
    return found


async def _run_suggest_job(job_id: str, round: str, story: Path) -> None:
    db = await get_db()
    try:
        await db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        await db.commit()
        out = await asyncio.to_thread(run_suggestions, round, story)
        await finish_job(
            db,
            job_id,
            output_path=str(story / SUGGESTIONS_FILE),
            job_type="editor_suggest",
            result={"suggestions": len(out.items)},
        )
    except Exception as exc:
        error = str(exc)[:1500]
        with contextlib.suppress(Exception):
            FileSuggestionStore(story).save(
                SuggestionSet(round=round, created_at=now_iso(), status="failed", error=error)
            )
        with contextlib.suppress(Exception):
            await finish_job(db, job_id, error=error, job_type="editor_suggest")
    finally:
        await db.close()


@router.get("/{round}/suggestions", response_model=SuggestionSet)
async def get_suggestions(round: str) -> SuggestionSet:
    return _suggestions(_story_dir(round))


@router.post("/{round}/suggestions", response_model=RankResponse)
async def start_suggestions(round: str) -> RankResponse:
    """Ask the panel (Jev, rules fallback) for 3-6 effect suggestions. Job-based."""
    story = _story_dir(round)
    if not (story / STORY_FILE).is_file():
        raise HTTPException(409, f"Round {round} has no rendered story yet: render it first")
    FileSuggestionStore(story).save(
        SuggestionSet(round=round, created_at=now_iso(), status="pending")
    )
    job_id = await _insert_job("editor_suggest")
    _track(_run_suggest_job(job_id, round, story))
    return RankResponse(job_id=job_id)


class AcceptBody(BaseModel):
    accepted: bool | None


@router.put("/{round}/suggestions/{item_id}", response_model=SuggestionSet)
async def accept_suggestion(round: str, item_id: str, body: AcceptBody) -> SuggestionSet:
    story = _story_dir(round)
    try:
        return FileSuggestionStore(story).set_accepted(item_id, body.accepted)
    except UnknownSuggestionError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@router.post("/{round}/suggestions/apply", response_model=RankResponse)
async def apply_suggestions(round: str) -> RankResponse:
    """Write the accepted suggestions as effect requests, then re-render."""
    story = _story_dir(round)
    try:
        asset_id = story_asset_id(story)
    except SuggestInputError as e:
        raise HTTPException(409, str(e)) from e
    try:
        apply_accepted(story, FileSuggestionStore(story))
    except UnknownSuggestionError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return await _start_render(asset_id, round, plays=True)
