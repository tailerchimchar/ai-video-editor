import asyncio
import contextlib
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..candidates.probe import get_duration_seconds
from ..config import settings
from ..database import get_db
from ..models import AssetOut, RankResponse, ScanResponse
from ..splitter import (
    child_filename,
    detect_game_boundaries,
    intervals_to_segments,
    split_segment,
)
from ..thumbnail import safe_extract_asset_thumbnail

router = APIRouter(tags=["assets"])

_background_tasks: set[asyncio.Task] = set()


def _track(coro) -> None:
    """Spawn a background task we won't await — same pattern as edits router."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@router.post("/assets/scan", response_model=ScanResponse)
async def scan_assets():
    media_dir = settings.outplayed_media_dir
    if not media_dir.exists():
        raise HTTPException(status_code=400, detail=f"Media directory not found: {media_dir}")

    db = await get_db()
    try:
        new_count = 0
        for root, _dirs, files in os.walk(media_dir):
            for filename in files:
                if not filename.lower().endswith(".mp4"):
                    continue
                filepath = os.path.join(root, filename)
                # Check if already indexed
                row = await db.execute_fetchall("SELECT id FROM assets WHERE path = ?", (filepath,))
                if row:
                    continue

                # Infer game from parent folder name
                parent = os.path.basename(os.path.dirname(filepath))
                game = parent if parent != os.path.basename(str(media_dir)) else None

                # Get file creation time
                stat = os.stat(filepath)
                created_at = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).isoformat()

                asset_id = str(uuid.uuid4())
                indexed_at = datetime.now(timezone.utc).isoformat()

                await db.execute(
                    "INSERT INTO assets (id, filename, path, game, created_at, indexed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (asset_id, filename, filepath, game, created_at, indexed_at),
                )
                # Auto-extract a poster frame so the gallery has something
                # to show on first paint. Best-effort — never fails the scan.
                # ~100ms per asset; on libraries of 1000+ recordings this
                # adds wall time but only on FIRST scan (idempotent after).
                safe_extract_asset_thumbnail(asset_id, filepath)
                new_count += 1

        await db.commit()
        total = await db.execute_fetchall("SELECT COUNT(*) as cnt FROM assets")
        return ScanResponse(new_assets=new_count, total_assets=total[0][0])
    finally:
        await db.close()


@router.get("/assets", response_model=list[AssetOut])
async def list_assets():
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM assets ORDER BY created_at DESC")
        return [AssetOut(**dict(row)) for row in rows]
    finally:
        await db.close()


@router.post("/assets/{asset_id}/thumbnail")
async def regenerate_asset_thumbnail(asset_id: str):
    """Extract (or re-extract) the poster frame for one asset.

    Useful when:
    - Backfilling thumbnails for assets indexed before auto-extract shipped.
    - Recovering after a deletion of the thumbnail file.
    - Forcing a refresh when the user wants a different frame.

    Returns the safe-extract structured result. 404 if asset unknown.
    """
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT path FROM assets WHERE id = ?", (asset_id,))
        if not rows:
            raise HTTPException(404, "Asset not found")
        return safe_extract_asset_thumbnail(asset_id, dict(rows[0])["path"], force=True)
    finally:
        await db.close()


@router.get("/assets/{asset_id}", response_model=AssetOut)
async def get_asset(asset_id: str):
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM assets WHERE id = ?", (asset_id,))
        if not rows:
            raise HTTPException(404, "Asset not found")
        return AssetOut(**dict(rows[0]))
    finally:
        await db.close()


# ---------------------------------------------------------------------
# Delete the source FILE (keeps the asset row + any compilations).
# ---------------------------------------------------------------------
#
# Use case: a 4GB Twitch VOD was downloaded via /assets/ingest_url and
# you've already compiled the highlights you want from it. The .mp4 is
# now dead weight on disk — delete it. The compilation reels stay
# intact (their rendered .mp4s are in compilations/, not next to the
# source). You just can't RE-CUT from the source after deletion.
#
# Guardrails (all enforced — refuses to delete otherwise):
# 1. `source_origin == 'downloaded'` — never auto-delete a manually
#    placed file (those are sacred).
# 2. `source_deleted_at IS NULL` — idempotent: already-deleted is a
#    no-op success.
# 3. Path must resolve INSIDE OUTPLAYED_MEDIA_DIR — defense against a
#    malformed DB row pointing at /etc/shadow or similar.
# 4. File must currently exist (just check) — if gone already, treat
#    same as already-deleted.


class DeleteSourceResponse(BaseModel):
    asset_id: str
    freed_bytes: int
    already_deleted: bool = False


@router.post("/assets/{asset_id}/delete_source", response_model=DeleteSourceResponse)
async def delete_source(asset_id: str) -> DeleteSourceResponse:
    """Delete the source .mp4 on disk. Asset row stays. Compilations
    made from this source remain intact and playable."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM assets WHERE id = ?", (asset_id,))
        if not rows:
            raise HTTPException(404, "Asset not found")
        asset = dict(rows[0])

        if asset.get("source_origin") != "downloaded":
            raise HTTPException(
                400,
                "Refusing to delete: source_origin is not 'downloaded'. "
                "Only files ingested via /assets/ingest_url can be auto-deleted "
                "— manually placed files are sacred.",
            )
        if asset.get("source_deleted_at"):
            return DeleteSourceResponse(asset_id=asset_id, freed_bytes=0, already_deleted=True)

        # Containment safety: the path MUST be inside OUTPLAYED_MEDIA_DIR.
        src_root = settings.outplayed_media_dir.resolve()
        try:
            file_path = (settings.outplayed_media_dir.parent / asset["path"]).resolve() \
                if not os.path.isabs(asset["path"]) else os.path.realpath(asset["path"])
            file_path = os.fspath(file_path)
        except Exception as exc:
            raise HTTPException(400, f"Could not resolve asset path: {exc}") from None

        if not file_path.startswith(str(src_root) + os.sep) and file_path != str(src_root):
            raise HTTPException(
                400,
                f"Refusing to delete: asset path {file_path!r} resolves outside "
                f"OUTPLAYED_MEDIA_DIR {str(src_root)!r}. Possible corrupted DB row.",
            )

        # Capture size before deletion so the caller can show "freed X GB".
        freed = 0
        if os.path.exists(file_path):
            try:
                freed = os.path.getsize(file_path)
            except OSError:
                freed = 0
            try:
                os.remove(file_path)
            except OSError as exc:
                raise HTTPException(500, f"os.remove failed: {exc}") from None

        await db.execute(
            "UPDATE assets SET source_deleted_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), asset_id),
        )
        await db.commit()
        return DeleteSourceResponse(asset_id=asset_id, freed_bytes=freed)
    finally:
        await db.close()


# ---------------------------------------------------------------------
# Ingest from URL (Twitch / YouTube / any yt-dlp-supported source)
# ---------------------------------------------------------------------
#
# Why: replaces the "open browser → grab URL → run yt-dlp → wait → drop
# file → /assets/scan" manual flow with a single API call. The downloaded
# file lands in OUTPLAYED_MEDIA_DIR (under a <game>/ subfolder so the
# existing per-game scan logic finds it on the next scan).
#
# Privacy: the entire pipeline stays local. yt-dlp pulls from the source
# to YOUR machine; nothing is uploaded anywhere.
#
# Optional dependency: yt-dlp must be importable as a Python module
# (`python -m yt_dlp`) by SOME python on PATH. Not a hard requirement of
# this package so the rest of the system stays light. We probe for it
# at request time and return a clear error if missing.


class IngestUrlBody(BaseModel):
    url: str = Field(..., description="HTTPS URL to a Twitch / YouTube / etc VOD")
    game: str = Field(..., description="Game subfolder under OUTPLAYED_MEDIA_DIR (e.g. 'league')")


_URL_RE = re.compile(r"^https?://[^\s/$.?#].\S*$")
# Sanitise the game name to a single safe path segment — prevents path
# traversal via the API. Lowercased, only [a-z0-9_-].
_GAME_RE = re.compile(r"^[a-z0-9_-]+$")


def _resolve_yt_dlp_invocation() -> list[str] | None:
    """Return the argv prefix for invoking yt-dlp, or None if missing.

    Tries the standalone CLI first (faster startup), falls back to
    `python -m yt_dlp` against the system Python. None = "not installed,
    tell the user to `pip install yt-dlp`."
    """
    cli = shutil.which("yt-dlp")
    if cli:
        return [cli]
    # Try common Python interpreters for the module fallback.
    for py in ("python", "py", "python3"):
        py_path = shutil.which(py)
        if py_path:
            try:
                subprocess.run(
                    [py_path, "-c", "import yt_dlp"],
                    check=True,
                    capture_output=True,
                    timeout=5,
                )
                return [py_path, "-m", "yt_dlp"]
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                continue
    return None


async def _run_ingest_url_job(job_id: str, url: str, game: str) -> None:
    """Download VOD via yt-dlp, register the asset, mark the job done."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,)
        )
        await db.commit()

        argv = _resolve_yt_dlp_invocation()
        if argv is None:
            await _finish_job(
                db,
                job_id,
                error=(
                    "yt-dlp not found on PATH. Install with `pip install yt-dlp` "
                    "in any Python on PATH, or download the standalone binary."
                ),
            )
            return

        dest_dir = settings.outplayed_media_dir / game
        dest_dir.mkdir(parents=True, exist_ok=True)

        # yt-dlp output template: <title-truncated>_<vod-id>.<ext>.
        # The vod-id keeps re-downloads idempotent (yt-dlp skips existing).
        out_template = str(dest_dir / "%(title).80B_%(id)s.%(ext)s")

        # --print after_move:filepath prints the final filepath AFTER any
        # post-processing moves — that's what we want to register in the DB.
        cmd = [
            *argv,
            "-f", "best",
            "-o", out_template,
            "--print", "after_move:filepath",
            "--no-progress",
            url,
        ]

        # Subprocess in a worker thread so we don't block the event loop.
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            timeout=3600,  # 1 hour cap — long enough for most VODs
        )

        if result.returncode != 0:
            err = (result.stderr or result.stdout or "yt-dlp failed").strip()[:1500]
            await _finish_job(db, job_id, error=err)
            return

        # Parse the printed filepath. yt-dlp emits one path per video; for
        # playlists we'd get multiple. We only handle the first/only path.
        filepath = (result.stdout or "").strip().splitlines()
        if not filepath:
            await _finish_job(db, job_id, error="yt-dlp returned no output path")
            return
        path = filepath[0].strip()
        if not os.path.exists(path):
            await _finish_job(db, job_id, error=f"yt-dlp claimed it wrote {path} but no such file")
            return

        # Register the asset with source_origin='downloaded' so the
        # cleanup tool (#2) can distinguish from manually-placed files.
        filename = os.path.basename(path)
        # If somehow scan already grabbed it, just update; otherwise insert.
        existing = await db.execute_fetchall(
            "SELECT id FROM assets WHERE path = ?", (path,)
        )
        if existing:
            asset_id = dict(existing[0])["id"]
            await db.execute(
                "UPDATE assets SET source_origin = 'downloaded' WHERE id = ?",
                (asset_id,),
            )
        else:
            asset_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            await db.execute(
                "INSERT INTO assets "
                "(id, filename, path, game, created_at, indexed_at, source_origin) "
                "VALUES (?, ?, ?, ?, ?, ?, 'downloaded')",
                (asset_id, filename, path, game, now, now),
            )
        await db.commit()
        await _finish_job(db, job_id, output_path=asset_id)
    except Exception as exc:  # never let the worker crash silently
        with contextlib.suppress(Exception):
            await _finish_job(db, job_id, error=str(exc)[:1500])
    finally:
        await db.close()


async def _finish_job(db, job_id: str, *, output_path: str | None = None,
                      error: str | None = None) -> None:
    status = "failed" if error else "completed"
    with contextlib.suppress(Exception):
        await db.execute(
            "UPDATE jobs SET status = ?, output_path = ?, error = ?, completed_at = ? "
            "WHERE id = ?",
            (status, output_path, error, datetime.now(timezone.utc).isoformat(), job_id),
        )
        await db.commit()


# ---------------------------------------------------------------------
# Multi-game VOD split — detect game boundaries via ffmpeg blackdetect,
# slice the parent into per-game child assets.
# ---------------------------------------------------------------------


async def _run_split_job(job_id: str, asset: dict) -> None:
    """Detect game boundaries + write per-game child files + register
    each child as a new asset linked to the parent."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,)
        )
        await db.commit()

        video_path = asset["path"]
        if not os.path.exists(video_path):
            await _finish_job(db, job_id, error=f"source file missing: {video_path}")
            return

        # Probe duration first — needed to bound the last segment.
        # ~50ms via ffprobe, much cheaper than the blackdetect scan.
        duration = await asyncio.to_thread(get_duration_seconds, video_path)
        if duration <= 0:
            await _finish_job(db, job_id, error="couldn't probe duration")
            return

        # Detect black intervals via ffmpeg blackdetect filter.
        # Run in a thread because it's a synchronous subprocess call
        # that scans the whole file (~30-60s for a 1.5hr VOD).
        intervals = await asyncio.to_thread(detect_game_boundaries, video_path)
        segments = intervals_to_segments(intervals, duration=duration)

        if len(segments) <= 1:
            # Either no black periods or only one valid segment after
            # filtering — nothing to split. Report as completed (not
            # an error) with a clear note so the user knows.
            await _finish_job(
                db,
                job_id,
                output_path=(
                    f"no split needed: detected {len(intervals)} black intervals, "
                    f"{len(segments)} game segment(s) after filtering"
                ),
            )
            return

        # Slice each segment into a child file beside the parent.
        # `-c copy` so it's fast (no re-encode) and quality-preserving.
        parent_dir = Path(video_path).parent
        new_ids: list[str] = []
        now = datetime.now(timezone.utc).isoformat()
        for seg in segments:
            child_name = child_filename(asset["filename"], seg)
            child_path = parent_dir / child_name
            ok, _err = await asyncio.to_thread(
                split_segment, video_path, str(child_path), seg.start, seg.end
            )
            if not ok:
                # One failed split shouldn't kill the whole job — report
                # what worked. The user can re-trigger or fix the bad
                # boundary manually.
                continue

            asset_id = str(uuid.uuid4())
            await db.execute(
                "INSERT INTO assets "
                "(id, filename, path, game, created_at, indexed_at, "
                " source_origin, parent_asset_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 'imported', ?)",
                (
                    asset_id,
                    child_name,
                    str(child_path),
                    asset.get("game"),
                    now,
                    now,
                    asset["id"],
                ),
            )
            new_ids.append(asset_id)
            # Best-effort thumbnail for the child too — the gallery will
            # show "no thumbnail yet" otherwise.
            safe_extract_asset_thumbnail(asset_id, str(child_path))

        await db.commit()
        await _finish_job(
            db,
            job_id,
            output_path=f"split into {len(new_ids)} games: {','.join(new_ids)}",
        )
    except Exception as exc:
        with contextlib.suppress(Exception):
            await _finish_job(db, job_id, error=str(exc)[:1500])
    finally:
        await db.close()


@router.post("/assets/{asset_id}/split", response_model=RankResponse)
async def split_vod(asset_id: str):
    """Detect game boundaries in a long VOD (typically a Twitch scrim
    that contains 2-4 games) and slice it into per-game child files.

    Uses ffmpeg `blackdetect` to find dark transitions between games
    (loading screens, return-to-lobby fades), filters to "real" game-
    length segments (>60s), and writes each segment to a child .mp4
    beside the parent. Each child is registered as its own asset with
    `parent_asset_id` linking back.

    Stream-copies (no re-encode) so it's fast + quality-preserving.
    Boundaries snap to nearest keyframe — 0-2s drift acceptable for
    game-boundary granularity.

    Returns a job_id — poll /api/v1/jobs/{id} for status. The job's
    `output_path` field carries a comma-separated list of new asset
    ids on success, or "no split needed: ..." when the VOD didn't
    look multi-game.
    """
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM assets WHERE id = ?", (asset_id,))
        if not rows:
            raise HTTPException(404, "Asset not found")
        asset = dict(rows[0])

        if asset.get("source_deleted_at"):
            raise HTTPException(
                400,
                "Refusing to split: the source file was deleted via "
                "/assets/{id}/delete_source — re-download or re-import first.",
            )

        job_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, 'split', 'pending', ?)",
            (job_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()

    _track(_run_split_job(job_id, asset))
    return RankResponse(job_id=job_id)


@router.post("/assets/ingest_url", response_model=RankResponse)
async def ingest_url(body: IngestUrlBody):
    """Download a VOD from a URL (Twitch / YouTube / etc) via yt-dlp.

    The downloaded file lands in OUTPLAYED_MEDIA_DIR/<game>/ and an
    asset row is registered with source_origin='downloaded'.

    Returns a job_id — poll /api/v1/jobs/{id} for status. The job's
    `output_path` field carries the new asset id on success.
    """
    if not _URL_RE.match(body.url):
        raise HTTPException(400, "url must start with http:// or https://")
    if not _GAME_RE.match(body.game.lower()):
        raise HTTPException(400, "game must be lowercase alphanumeric (+ dash/underscore)")
    if not settings.outplayed_media_dir.exists():
        raise HTTPException(
            500,
            f"OUTPLAYED_MEDIA_DIR does not exist: {settings.outplayed_media_dir}",
        )

    db = await get_db()
    try:
        job_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, 'ingest_url', 'pending', ?)",
            (job_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()

    _track(_run_ingest_url_job(job_id, body.url, body.game.lower()))
    return RankResponse(job_id=job_id)
