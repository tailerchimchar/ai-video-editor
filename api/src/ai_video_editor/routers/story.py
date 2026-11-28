"""Story requests and story re-render, drivable from chat.

    GET    /story/{round}/requests          list the owner's requests (0-based index)
    POST   /story/{round}/requests          append one request (validated)
    DELETE /story/{round}/requests/{index}  remove one request
    POST   /story/{round}/render            background job: same path as `story-render`
    GET    /story/{round}/suggestions       the editor suggestions (404 before the first run)
    POST   /story/{round}/suggestions       background job `editor_suggest` (Jev panel)
    PUT    /story/{round}/suggestions/{id}  {accepted: true|false|null}
    POST   /story/{round}/suggestions/apply accepted -> effect requests, then a story_render job
    POST   /story/join                      background job: same path as `story-join`

Requests live in `WORKSPACE/story/<round>/requests.json` (story/requests.py).
The render job emits `job.done` through `finish_job` like every job.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import settings
from ..database import get_db
from ..jobs import finish_job
from ..models import RankResponse
from ..story.apply_edits import apply_edits
from ..story.bookends import parse_pick
from ..story.cli import render_round
from ..story.join import default_out, join_stories
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


class JoinBody(BaseModel):
    rounds: list[str] = []
    title: str | None = None
    subscribe_after: int | None = 1
    hook: list[str] = []  # "GAME:CHAPTER[:TEXT]", 1-based
    hook_mode: Literal["tease", "play"] = "tease"
    hook_title: str | None = None
    outro: bool = False
    music_start_s: float = 0.0
    apply_edits: str | None = None  # joined stem: re-join from its hook edits
    force_edits: bool = False


async def _run_join_job(job_id: str, body: JoinBody) -> None:
    db = await get_db()
    try:
        await db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        await db.commit()
        if body.apply_edits:
            out = await asyncio.to_thread(apply_edits, body.apply_edits, force=body.force_edits)
            await finish_job(db, job_id, output_path=str(out), job_type="story_join")
            return
        out = await asyncio.to_thread(
            join_stories,
            body.rounds,
            default_out(body.rounds),
            subscribe_after=body.subscribe_after or None,
            title=body.title,
            hook=[replace(parse_pick(h), mode=body.hook_mode) for h in body.hook] or None,
            hook_title=body.hook_title,
            outro=body.outro,
            music_start_s=body.music_start_s,
        )
        await finish_job(db, job_id, output_path=str(out), job_type="story_join")
    except Exception as exc:
        with contextlib.suppress(Exception):
            await finish_job(db, job_id, error=str(exc)[:1500], job_type="story_join")
    finally:
        await db.close()


@router.post("/join", response_model=RankResponse)
async def join(body: JoinBody) -> RankResponse:
    """Join rendered stories into one video (swipes, optional hook / outro). Job-based."""
    if body.apply_edits:
        if body.rounds:
            raise HTTPException(422, "apply_edits takes no rounds")
        defaults = JoinBody()
        given = [
            f
            for f in JoinBody.model_fields
            if f not in ("apply_edits", "force_edits", "rounds")
            and getattr(body, f) != getattr(defaults, f)
        ]
        if given:
            raise HTTPException(
                422, f"apply_edits takes no {', '.join(given)} (the edits file and map decide)"
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]+", body.apply_edits):
            raise HTTPException(422, "bad apply_edits stem")
        job_id = await _insert_job("story_join")
        _track(_run_join_job(job_id, body))
        return RankResponse(job_id=job_id)
    if len(body.rounds) < 2:
        raise HTTPException(422, "need at least 2 rounds")
    for r in body.rounds:
        if not (_story_dir(r) / "story.mp4").is_file():
            raise HTTPException(409, f"Round {r} has no rendered story yet")
    try:
        for h in body.hook:
            parse_pick(h)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    job_id = await _insert_job("story_join")
    _track(_run_join_job(job_id, body))
    return RankResponse(job_id=job_id)


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
