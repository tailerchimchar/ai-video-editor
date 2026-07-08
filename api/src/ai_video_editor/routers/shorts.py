"""Shorts pipeline HTTP endpoints.

Three routes:

- `POST /assets/{asset_id}/shorts` — trigger a compile_shorts job. Runs
  the render pipeline in a background task tracked via the jobs table
  (same pattern as split/ingest_url). Returns a `job_id`.
- `GET /assets/{asset_id}/shorts` — list rendered shorts on disk for
  an asset in either mode. Reads `index.json`/`index.md`; never
  triggers a render.
- `GET /assets/{asset_id}/shorts/topics` — preview the bucket
  distribution (topic buckets + clip counts) without rendering
  anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..database import get_db
from ..models import RankResponse
from ..shorts import (
    _shorts_folder,
    build_cross_asset_montage,
    build_custom_montage,
    build_outplayed_montage,
    build_shorts,
    list_cross_asset_shorts,
    list_custom_shorts,
    list_highlights_clips,
    list_highlights_sessions,
    list_outplayed_clips,
    list_outplayed_shorts,
    preview_buckets,
    preview_cross_asset_montage,
    preview_outplayed_montage,
)

router = APIRouter(tags=["shorts"])

_background_tasks: set[asyncio.Task] = set()


def _track(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _finish_job(
    db, job_id: str, *, output_path: str | None = None, error: str | None = None
) -> None:
    status = "failed" if error else "completed"
    with contextlib.suppress(Exception):
        await db.execute(
            "UPDATE jobs SET status = ?, output_path = ?, error = ?, completed_at = ? "
            "WHERE id = ?",
            (status, output_path, error, datetime.now(timezone.utc).isoformat(), job_id),
        )
        await db.commit()


class CompileShortsBody(BaseModel):
    """Payload for `POST /assets/{id}/shorts`."""

    mode: str = Field(..., description="voiceover | montage")
    topic: str | None = Field(
        default=None,
        description=(
            "Optional bucket-substring filter, case-insensitive. "
            "e.g. 'laning', 'teamfight', 'objective'. Omit for all buckets."
        ),
    )
    music_path: str | None = Field(
        default=None,
        description=(
            "Override the config default music path for montage mode. "
            "Ignored in voiceover mode."
        ),
    )
    layout: str | None = Field(
        default=None,
        description=(
            "Visual layout: `cropped_hud` (default) or `blur_fill`. "
            "cropped_hud shrinks the play area less and re-composites "
            "the killfeed/minimap into the vertical frame. blur_fill "
            "preserves the full 16:9 in a thin center strip."
        ),
    )
    beat_flash: bool | None = Field(
        default=None,
        description=(
            "Overlay a subtle white pulse. Requires montage mode. "
            "Defaults to config `SHORTS_BEAT_FLASH_ENABLED`."
        ),
    )
    beat_flash_source: str | None = Field(
        default=None,
        description=(
            "What to flash on: `kills` (default, on each clip's payoff "
            "moment — tastefully rare, no music needed), `downbeats` "
            "(every 4th music beat, needs music), or `beats` (every "
            "music beat — hyperactive)."
        ),
    )


async def _load_candidates(db, asset_id: str) -> list[dict]:
    """Best-effort candidate load; the highlights folder resolver needs
    the candidate rows to derive the folder path (Riot correlation,
    champion, etc.)."""
    rows = await db.execute_fetchall(
        "SELECT * FROM highlight_candidates WHERE video_id = ?", (asset_id,)
    )
    return [dict(r) for r in rows]


async def _run_shorts_job(
    job_id: str,
    asset: dict,
    body: CompileShortsBody,
) -> None:
    """Background worker — the actual render happens here."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,)
        )
        await db.commit()

        candidates = await _load_candidates(db, asset["id"])
        result = await asyncio.to_thread(
            build_shorts,
            asset,
            candidates,
            mode=body.mode,
            topic=body.topic,
            music_path=body.music_path,
            layout=body.layout,
            beat_flash=body.beat_flash,
            beat_flash_source=body.beat_flash_source,
        )
        if not result.get("ok"):
            await _finish_job(
                db, job_id, error=str(result.get("error") or "shorts render failed")
            )
            return
        await _finish_job(
            db,
            job_id,
            output_path=(
                f"{result.get('shorts_written', 0)}/"
                f"{result.get('shorts_total', 0)} -> {result.get('folder', '?')}"
            ),
        )
    except Exception as exc:
        with contextlib.suppress(Exception):
            await _finish_job(db, job_id, error=str(exc)[:1500])
    finally:
        await db.close()


@router.post("/assets/{asset_id}/shorts", response_model=RankResponse)
async def compile_shorts(asset_id: str, body: CompileShortsBody):
    """Trigger a shorts render job for the given asset.

    Requires the asset to have been analyzed with `cut=True` first (so
    the highlights folder exists). Returns a `job_id`; poll
    `/api/v1/jobs/{id}` for status.
    """
    if body.mode not in ("voiceover", "montage"):
        raise HTTPException(400, "mode must be 'voiceover' or 'montage'")

    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM assets WHERE id = ?", (asset_id,)
        )
        if not rows:
            raise HTTPException(404, "Asset not found")
        asset = dict(rows[0])
        job_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, 'shorts', 'pending', ?)",
            (job_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()

    _track(_run_shorts_job(job_id, asset, body))
    return RankResponse(job_id=job_id)


@router.get("/assets/{asset_id}/shorts")
async def list_shorts(asset_id: str, mode: str | None = None):
    """List rendered shorts on disk for this asset.

    `mode` filters to voiceover / montage subfolder; omit for both.
    Read-only — never triggers a render.
    """
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM assets WHERE id = ?", (asset_id,)
        )
        if not rows:
            raise HTTPException(404, "Asset not found")
        asset = dict(rows[0])
    finally:
        await db.close()

    modes = [mode] if mode in ("voiceover", "montage") else ["voiceover", "montage"]
    out: dict[str, dict] = {}
    for m in modes:
        folder = _shorts_folder(asset, m)
        if not folder.is_dir():
            out[m] = {"folder": None, "shorts": []}
            continue
        shorts = sorted(p.name for p in folder.glob("*.mp4"))
        index_md = folder / "index.md"
        out[m] = {
            "folder": folder.as_posix(),
            "shorts": shorts,
            "index_md": index_md.read_text(encoding="utf-8") if index_md.is_file() else None,
        }
    return out


class CrossAssetMontageBody(BaseModel):
    """Payload for `POST /shorts/cross_asset_montage`."""

    game: str = Field(..., description="Game name (e.g. 'League of Legends')")
    window: str = Field(
        default="week",
        description=(
            "Time window over which to sweep session folders. "
            "`day` = today + yesterday, `week` = last 7 days, "
            "`all` = no filter. Defaults to `week`."
        ),
    )
    music_path: str | None = Field(default=None, description="Optional music bed.")
    layout: str | None = Field(
        default=None,
        description="`cropped_hud` (default) or `blur_fill`.",
    )
    beat_flash: bool | None = Field(
        default=None,
        description="Overlay a subtle white pulse. Defaults to config.",
    )
    beat_flash_source: str | None = Field(
        default=None,
        description=(
            "`kills` (default; on each clip's payoff), `downbeats` "
            "(every 4th music beat), or `beats` (every music beat)."
        ),
    )


async def _run_cross_asset_job(
    job_id: str,
    body: CrossAssetMontageBody,
) -> None:
    """Background worker for cross-asset montage compiles."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,)
        )
        await db.commit()

        result = await asyncio.to_thread(
            build_cross_asset_montage,
            game=body.game,
            window=body.window,
            music_path=body.music_path,
            layout=body.layout,
            beat_flash=body.beat_flash,
            beat_flash_source=body.beat_flash_source,
        )
        if not result.get("ok"):
            await _finish_job(
                db,
                job_id,
                error=str(result.get("error") or "cross-asset render failed"),
            )
            return
        await _finish_job(
            db,
            job_id,
            output_path=(
                f"{result.get('shorts_written', 0)}/"
                f"{result.get('shorts_total', 0)} -> {result.get('folder', '?')}"
            ),
        )
    except Exception as exc:
        with contextlib.suppress(Exception):
            await _finish_job(db, job_id, error=str(exc)[:1500])
    finally:
        await db.close()


@router.post("/shorts/cross_asset_montage", response_model=RankResponse)
async def compile_cross_asset_montage(body: CrossAssetMontageBody):
    """Trigger a cross-asset "best of" montage render job.

    Sweeps every `WORKSPACE/highlights/<game>/<session>/` folder
    inside `window`, ranks multikill clusters (3+ kills = HIGH
    priority), and renders ONE 9:16 montage short. Returns a
    `job_id`; poll `/api/v1/jobs/{id}` for status.
    """
    if body.window not in ("day", "week", "all"):
        raise HTTPException(400, "window must be 'day', 'week', or 'all'")

    db = await get_db()
    try:
        job_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, 'cross_asset_shorts', 'pending', ?)",
            (job_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()

    _track(_run_cross_asset_job(job_id, body))
    return RankResponse(job_id=job_id)


@router.get("/shorts/cross_asset_montage")
async def list_cross_asset(game: str | None = None):
    """List cross-asset "best of" shorts rendered on disk.

    Optionally filter by game (substring on the folder prefix).
    Read-only — never triggers a render.
    """
    return list_cross_asset_shorts(game=game)


class DownloadMusicBody(BaseModel):
    """Payload for `POST /shorts/music`."""

    url: str = Field(
        ...,
        description="YouTube URL or local file path. Cached by URL hash.",
    )
    name: str | None = Field(
        default=None,
        description=(
            "Optional filename stem (e.g. 'beat_drop' -> 'beat_drop.mp3'). "
            "Omit for hash-based auto name."
        ),
    )


@router.post("/shorts/music")
async def download_music_endpoint(body: DownloadMusicBody):
    """Download a music track to `WORKSPACE/music/` and return its
    local path. Idempotent — repeat calls return the cached file.
    Use the returned path as `music_path` in a subsequent compile call.
    """
    from ..music import download_music

    try:
        path = await asyncio.to_thread(
            download_music, body.url, name=body.name
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"music download failed: {exc}") from exc
    return {
        "ok": True,
        "path": path.as_posix(),
        "name": path.name,
        "size_bytes": path.stat().st_size if path.is_file() else 0,
    }


@router.get("/shorts/cross_asset_montage/preview")
async def preview_cross_asset(game: str, window: str = "week"):
    """Dry-run: scan + rank the cross-asset candidates without rendering.

    Cheap; useful to see WHAT would be compiled before spending
    ~5 minutes on ffmpeg.
    """
    if window not in ("day", "week", "all"):
        raise HTTPException(400, "window must be 'day', 'week', or 'all'")
    return preview_cross_asset_montage(game=game, window=window)


class OutplayedMontageBody(BaseModel):
    """Payload for `POST /shorts/outplayed_montage`."""

    game: str = Field(..., description="Game name — e.g. 'Valorant'.")
    limit: int | None = Field(
        default=None, description="Max clips in the montage. Defaults to 5."
    )
    since_days: int | None = Field(
        default=None,
        description=(
            "Only include clips modified within the last N days. "
            "Omit for all-time. e.g. `1` = today's session only."
        ),
    )
    music_path: str | None = Field(default=None)
    layout: str | None = Field(default=None)
    beat_flash: bool | None = Field(default=None)
    beat_flash_source: str | None = Field(default=None)
    use_clip_analyzer: bool | None = Field(
        default=None,
        description=(
            "Toggle the per-clip VLM analyzer that decides the trim "
            "window before render. Defaults to config "
            "`SHORTS_USE_CLIP_ANALYZER` (currently on)."
        ),
    )
    analyze_pool_size: int | None = Field(
        default=None,
        description=(
            "How many candidate clips to analyze before ranking by "
            "payoff type + confidence. Larger pool = better selection "
            "at the cost of more VLM calls (cached after first pass). "
            "Defaults to 3x `limit`."
        ),
    )


async def _run_outplayed_job(job_id: str, body: OutplayedMontageBody) -> None:
    """Background worker for outplayed direct compile."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,)
        )
        await db.commit()

        result = await asyncio.to_thread(
            build_outplayed_montage,
            game=body.game,
            limit=body.limit,
            since_days=body.since_days,
            music_path=body.music_path,
            layout=body.layout,
            beat_flash=body.beat_flash,
            beat_flash_source=body.beat_flash_source,
            use_clip_analyzer=body.use_clip_analyzer,
            analyze_pool_size=body.analyze_pool_size,
        )
        if not result.get("ok"):
            await _finish_job(
                db, job_id, error=str(result.get("error") or "outplayed render failed")
            )
            return
        await _finish_job(
            db,
            job_id,
            output_path=(
                f"{result.get('shorts_written', 0)}/"
                f"{result.get('shorts_total', 0)} -> {result.get('folder', '?')}"
            ),
        )
    except Exception as exc:
        with contextlib.suppress(Exception):
            await _finish_job(db, job_id, error=str(exc)[:1500])
    finally:
        await db.close()


@router.post("/shorts/outplayed_montage", response_model=RankResponse)
async def compile_outplayed_montage_endpoint(body: OutplayedMontageBody):
    """Compile a montage DIRECTLY from Outplayed's auto-cut clips for
    `game` — no DB scan or LLM ranking. Best for games where
    Outplayed's event detection already picks clean clips (Valorant
    especially).
    """
    db = await get_db()
    try:
        job_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, 'outplayed_shorts', 'pending', ?)",
            (job_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()

    _track(_run_outplayed_job(job_id, body))
    return RankResponse(job_id=job_id)


@router.get("/shorts/outplayed_montage")
async def list_outplayed(game: str | None = None):
    """List Outplayed best-of shorts on disk. Read-only."""
    return list_outplayed_shorts(game=game)


@router.get("/shorts/outplayed_montage/preview")
async def preview_outplayed(
    game: str, limit: int | None = None, since_days: int | None = None
):
    """Dry-run: scan Outplayed clips + show what would be picked."""
    return preview_outplayed_montage(
        game=game, limit=limit, since_days=since_days
    )


# ---------------------------------------------------------------------
# Browse — list what's available in each source location
# ---------------------------------------------------------------------


@router.get("/shorts/browse/outplayed")
async def browse_outplayed(
    game: str,
    since_days: int | None = None,
    limit: int | None = None,
):
    """List Outplayed auto-cut clips for `game`. Read-only."""
    return list_outplayed_clips(
        game, since_days=since_days, limit=limit
    )


@router.get("/shorts/browse/highlights")
async def browse_highlights(game: str | None = None):
    """List analyzed session folders under WORKSPACE/highlights/."""
    return list_highlights_sessions(game)


@router.get("/shorts/browse/highlights/{session_folder}")
async def browse_highlights_session(
    session_folder: str, game: str | None = None
):
    """List clips inside one analyzed highlights session folder."""
    return list_highlights_clips(session_folder, game=game)


# ---------------------------------------------------------------------
# Custom montage — stitch an explicit list of paths from anywhere
# ---------------------------------------------------------------------


class CustomMontageBody(BaseModel):
    """Payload for `POST /shorts/custom_montage`."""

    clip_paths: list[str] = Field(
        ...,
        description=(
            "Ordered list of clip file paths. Any existing `.mp4` "
            "works — Outplayed folder, highlights folder, wherever."
        ),
    )
    name: str | None = Field(
        default=None,
        description="Output folder + filename stem. Omit for timestamp.",
    )
    game: str | None = Field(
        default=None,
        description=(
            "Selects the editing rubric for the per-clip analyzer "
            "(vlm/game_hints/<game>.md). Omit for the generic rubric."
        ),
    )
    music_path: str | None = Field(default=None)
    layout: str | None = Field(default=None)
    beat_flash: bool | None = Field(default=None)
    beat_flash_source: str | None = Field(default=None)
    use_clip_analyzer: bool | None = Field(
        default=None,
        description="Per-clip VLM analyzer. Defaults to config.",
    )


async def _run_custom_job(job_id: str, body: CustomMontageBody) -> None:
    """Background worker for custom montage compile."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,)
        )
        await db.commit()

        result = await asyncio.to_thread(
            build_custom_montage,
            clip_paths=body.clip_paths,
            name=body.name,
            game=body.game,
            music_path=body.music_path,
            layout=body.layout,
            beat_flash=body.beat_flash,
            beat_flash_source=body.beat_flash_source,
            use_clip_analyzer=body.use_clip_analyzer,
        )
        if not result.get("ok"):
            await _finish_job(
                db,
                job_id,
                error=str(result.get("error") or "custom render failed"),
            )
            return
        await _finish_job(
            db,
            job_id,
            output_path=(
                f"{result.get('shorts_written', 0)}/"
                f"{result.get('shorts_total', 0)} -> {result.get('folder', '?')}"
            ),
        )
    except Exception as exc:
        with contextlib.suppress(Exception):
            await _finish_job(db, job_id, error=str(exc)[:1500])
    finally:
        await db.close()


@router.post("/shorts/custom_montage", response_model=RankResponse)
async def compile_custom_montage_endpoint(body: CustomMontageBody):
    """Compile a montage from an EXPLICIT list of clip paths. Any
    existing `.mp4` works — mix Outplayed clips, highlights clips,
    anything on disk."""
    if not body.clip_paths:
        raise HTTPException(400, "clip_paths cannot be empty")
    db = await get_db()
    try:
        job_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, 'custom_shorts', 'pending', ?)",
            (job_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()

    _track(_run_custom_job(job_id, body))
    return RankResponse(job_id=job_id)


@router.get("/shorts/custom_montage")
async def list_custom(game: str | None = None):
    """List custom-compile outputs. `game` param ignored (kept for
    symmetry with other list endpoints)."""
    _ = game
    return list_custom_shorts()


@router.get("/assets/{asset_id}/shorts/topics")
async def preview_shorts_topics(asset_id: str):
    """Preview the bucket distribution for this asset without rendering.

    Reads the highlights folder + `index.json`, runs `categorize_clips`,
    returns per-bucket clip counts + a small clip preview. Useful before
    committing to a compile.
    """
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM assets WHERE id = ?", (asset_id,)
        )
        if not rows:
            raise HTTPException(404, "Asset not found")
        asset = dict(rows[0])
        candidates = await _load_candidates(db, asset_id)
    finally:
        await db.close()
    # `preview_buckets` is a pure read — no ffmpeg, no I/O beyond
    # `index.json`. Safe to run inline.
    _ = Path  # silence unused-import warnings under some ruff configs
    return preview_buckets(asset, candidates)
