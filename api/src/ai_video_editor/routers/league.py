"""LoL-specific HTTP endpoints.

Lives in the shared `routers/` layer (consumer of `league/`, like any
other router) but the prefix keeps every LoL-flavored URL under
`/api/v1/league/...` for discoverability.

Today: `POST /league/detect_champion`. Future LoL endpoints land here
(killfeed analysis if we ever revisit, chat-region OCR, etc.).
"""

import asyncio
import contextlib
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import settings
from ..database import get_db
from ..league.candidates.champion import (
    DEFAULT_MIN_CONFIDENCE,
    detect_champion,
)
from ..models import RankResponse

router = APIRouter(tags=["league"], prefix="/league")

_background_tasks: set[asyncio.Task] = set()


def _track(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _finish(db, job_id: str, *, output_path: str | None, error: str | None) -> None:
    with contextlib.suppress(Exception):
        status = "failed" if error else "completed"
        await db.execute(
            "UPDATE jobs SET status = ?, output_path = ?, error = ?, completed_at = ? WHERE id = ?",
            (status, output_path, error, datetime.now(timezone.utc).isoformat(), job_id),
        )
        await db.commit()


async def _load_asset(db, asset_id: str) -> dict:
    rows = await db.execute_fetchall("SELECT * FROM assets WHERE id = ?", (asset_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Asset not found")
    return dict(rows[0])


def _result_path(asset_id: str) -> str:
    folder = settings.workspace_dir / "champion_detections"
    folder.mkdir(parents=True, exist_ok=True)
    return (folder / f"{asset_id}.json").as_posix()


class DetectChampionRequest(BaseModel):
    asset_id: str
    # Override the auto mid-game sample point. Useful when the HUD is
    # covered by something at mid-game (death cam, champ select replay).
    at_seconds: float | None = Field(None, ge=0.0)
    min_confidence: float = Field(DEFAULT_MIN_CONFIDENCE, ge=0.0, le=1.0)


def _probe_duration(path: str) -> float:
    from ..candidates.probe import get_duration_seconds

    return get_duration_seconds(path)


async def _run_detect_job(job_id: str, asset: dict, req: DetectChampionRequest) -> None:
    db = await get_db()
    try:
        await db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        await db.commit()
        duration = await asyncio.to_thread(_probe_duration, asset["path"])
        if duration <= 0:
            await _finish(db, job_id, output_path=None, error="could not probe duration")
            return

        result = await asyncio.to_thread(
            detect_champion,
            asset,
            duration,
            at_seconds=req.at_seconds,
            min_confidence=req.min_confidence,
        )
        if result is None:
            # Not an error — CV simply didn't lock onto anything above
            # threshold. Record completion with a structured null result
            # so callers can distinguish "ran but no match" from "crashed".
            result = {"name": None, "confidence": None, "source": "cv", "reason": "no_match"}

        result["asset_id"] = asset["id"]
        result["sampled_at_seconds"] = result.get("sample_seconds")
        out_path = _result_path(asset["id"])
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        await _finish(db, job_id, output_path=out_path, error=None)
    except Exception as e:
        await _finish(db, job_id, output_path=None, error=str(e)[:2000])
    finally:
        await db.close()


@router.post("/detect_champion", response_model=RankResponse)
async def detect_champion_endpoint(req: DetectChampionRequest):
    db = await get_db()
    try:
        asset = await _load_asset(db, req.asset_id)
        job_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, 'detect_champion', 'pending', ?)",
            (job_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()

    _track(_run_detect_job(job_id, asset, req))
    return RankResponse(job_id=job_id)


@router.get("/champion/{asset_id}")
async def get_champion(asset_id: str):
    """Read the most recent champion-detection result for an asset.

    Returns 404 when no detection has been run. Result schema matches
    what `_run_detect_job` writes: `{name, confidence, source,
    datadragon_version, sample_seconds, asset_id, ...}`.
    """
    path = settings.workspace_dir / "champion_detections" / f"{asset_id}.json"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="No champion detection yet — POST /league/detect_champion first",
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f)
