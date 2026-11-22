import asyncio
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..anchoring import auto_windows_from_anchors, snap_window_to_anchor
from ..candidates.overwolf_events import kill_anchors_for_video
from ..candidates.probe import get_duration_seconds
from ..config import settings
from ..database import get_db
from ..editing import trim_clip
from ..edits import apply_caption
from ..models import ClipCreate, ClipOut

router = APIRouter(tags=["clips"])

_background_tasks: set[asyncio.Task] = set()  # prevent GC of background tasks


async def _run_clip_job(job_id: str, asset_path: str, output_path: str, start: float, end: float):
    db = await get_db()
    try:
        await db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        await db.commit()

        ok, error = await asyncio.to_thread(trim_clip, asset_path, output_path, start, end)
        now = datetime.now(timezone.utc).isoformat()
        if not ok:
            await db.execute(
                "UPDATE jobs SET status = 'failed', error = ?, completed_at = ? WHERE id = ?",
                (error, now, job_id),
            )
        else:
            await db.execute(
                "UPDATE jobs SET status = 'completed', output_path = ?,"
                " completed_at = ? WHERE id = ?",
                (output_path, now, job_id),
            )
        await db.commit()
    finally:
        await db.close()


@router.post("/clips", response_model=ClipOut)
async def create_clip(body: ClipCreate):
    if body.start_seconds >= body.end_seconds:
        raise HTTPException(status_code=400, detail="start_seconds must be less than end_seconds")
    if body.start_seconds < 0:
        raise HTTPException(status_code=400, detail="start_seconds must be non-negative")

    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM assets WHERE id = ?", (body.asset_id,))
        if not rows:
            raise HTTPException(status_code=404, detail="Asset not found")
        asset = dict(rows[0])

        clip_id = str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        clips_dir = settings.workspace_dir / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        output_path = (clips_dir / f"{clip_id}.mp4").as_posix()

        await db.execute(
            "INSERT INTO clips (id, asset_id, start_seconds, end_seconds, output_path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (clip_id, body.asset_id, body.start_seconds, body.end_seconds, output_path, now),
        )
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, 'clip', 'pending', ?)",
            (job_id, now),
        )
        await db.commit()
    finally:
        await db.close()

    task = asyncio.create_task(
        _run_clip_job(job_id, asset["path"], output_path, body.start_seconds, body.end_seconds)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return ClipOut(
        id=clip_id,
        asset_id=body.asset_id,
        start_seconds=body.start_seconds,
        end_seconds=body.end_seconds,
        output_path=output_path,
        created_at=now,
        job_id=job_id,
    )


# ---------------------------------------------------------------------
# Trim by source path (no asset row required) -- editing primitive for
# jump-cut edits and story shorts.
# ---------------------------------------------------------------------


class TrimSourceBody(BaseModel):
    """Trim any .mp4 by absolute path into a stable file under
    `WORKSPACE_DIR/trims/`. Runs SYNCHRONOUSLY (stream-copy trim finishes
    in <1s for anything under 5 min). No job row -- callers get the path
    back immediately and can feed it straight into
    `compile_custom_montage(clip_paths=[...])` for jump-cut edits.
    """

    source_path: str = Field(
        ...,
        description=(
            "Absolute path to the source .mp4. Any file on disk -- "
            "Outplayed folder, D:/ archive, temp dir, wherever."
        ),
    )
    start_seconds: float = Field(..., ge=0.0)
    end_seconds: float = Field(..., gt=0.0)
    name: str | None = Field(
        default=None,
        description=(
            "Output filename stem (no extension). Omit to derive from "
            "source + range (e.g. `sourcename_10-25s`)."
        ),
    )
    anchor_to_kill: bool = Field(
        default=False,
        description=(
            "When true and the source has Overwolf ground-truth kill "
            "anchors, snap the window to the nearest kill (per-type "
            "pre/post buffers). Falls through unchanged when no anchor "
            "is within 5s of the requested midpoint."
        ),
    )


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_\-]+")


def _sanitize_name(name: str) -> str:
    """Strip anything that would be dodgy in a filename."""
    return _SAFE_NAME_RE.sub("_", name).strip("_") or "clip"


@router.post("/clips/trim_source")
async def trim_source_to_clip(body: TrimSourceBody):
    """Cut a window out of any source .mp4 to a stable file. Returns
    `{ok, output_path, snapped, anchor_seconds}`. `snapped=True` when
    `anchor_to_kill` moved the window to a ground-truth kill anchor.

    Fails 400 on empty/reversed range or missing source. Fails 500
    with the ffmpeg stderr tail on encode failures.
    """
    if body.end_seconds <= body.start_seconds:
        raise HTTPException(400, "end_seconds must be greater than start_seconds")
    src = Path(body.source_path)
    if not src.is_file():
        raise HTTPException(404, f"source not found: {src}")

    start = body.start_seconds
    end = body.end_seconds
    snapped = False
    anchor_seconds: float | None = None
    event_type: str | None = None
    if body.anchor_to_kill:
        duration = get_duration_seconds(str(src))
        if duration > 0:
            anchors = await asyncio.to_thread(kill_anchors_for_video, str(src), duration)
            snap = snap_window_to_anchor(start, end, anchors, duration_seconds=duration)
            start, end = snap.start_seconds, snap.end_seconds
            snapped = snap.snapped
            anchor_seconds = snap.anchor_seconds
            event_type = snap.event_type

    if body.name:
        stem = _sanitize_name(body.name)
    else:
        stem = f"{_sanitize_name(src.stem)}_{int(start)}-{int(end)}s"
    out_dir = settings.workspace_dir / "trims"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{stem}.mp4"

    ok, error = await asyncio.to_thread(
        trim_clip,
        str(src),
        str(out_path),
        start,
        end,
    )
    if not ok:
        raise HTTPException(500, f"trim failed: {(error or '')[:500]}")
    return {
        "ok": True,
        "output_path": str(out_path),
        "snapped": snapped,
        "anchor_seconds": anchor_seconds,
        "event_type": event_type,
        "start_seconds": start,
        "end_seconds": end,
    }


# ---------------------------------------------------------------------
# Ground-truth kill anchor preview + auto-cut. The mechanical layer of
# the anchor+critic pipeline -- deterministic, zero LLM, only fires
# when Outplayed dispatched the source (fresh files, log not rotated).
# ---------------------------------------------------------------------


class ListKillAnchorsBody(BaseModel):
    source_path: str = Field(...)
    include_clusters: bool = Field(
        default=True,
        description=(
            "Include multi-kill cluster anchors (double/triple/quad/ace) "
            "alongside individual kills. Set False for voice-over pacing "
            "where each frag lands separately."
        ),
    )


@router.post("/clips/list_kill_anchors")
async def list_kill_anchors(body: ListKillAnchorsBody):
    """Preview ground-truth kill anchors from Outplayed's log. Empty
    list when the source isn't dispatched (log rotated / user-clipped
    / non-Val game). Use this BEFORE `auto_cut_kills` to see what the
    log says before spending encode time.
    """
    src = Path(body.source_path)
    if not src.is_file():
        raise HTTPException(404, f"source not found: {src}")
    duration = get_duration_seconds(str(src))
    if duration <= 0:
        raise HTTPException(400, f"could not probe duration: {src}")

    anchors = await asyncio.to_thread(
        kill_anchors_for_video,
        str(src),
        duration,
        include_clusters=body.include_clusters,
    )
    return {
        "ok": True,
        "source_path": str(src),
        "duration_seconds": duration,
        "count": len(anchors),
        "anchors": [
            {
                "anchor_seconds": a.anchor_seconds,
                "event_type": a.event_type,
                "cluster_size": a.metadata.get("cluster_size"),
                "headshot": a.metadata.get("headshot", False),
            }
            for a in anchors
        ],
    }


class AutoCutKillsBody(BaseModel):
    source_path: str = Field(...)
    include_clusters: bool = Field(default=True)
    name_prefix: str | None = Field(
        default=None,
        description=(
            "Filename stem prefix. Each kill gets `<prefix>_<idx>_"
            "<event_type>.mp4`. Omit to derive from source name."
        ),
    )


@router.post("/clips/auto_cut_kills")
async def auto_cut_kills(body: AutoCutKillsBody):
    """Auto-cut one sub-clip per Overwolf kill anchor with per-type
    buffers, merging overlapping windows. Returns a list of output
    paths ready to feed into `compile_custom_montage`.

    Empty `clips` when the source isn't in Outplayed's dispatch log
    (with `reason` set) -- the caller falls back to hand-picked
    windows or waits for the killfeed-OCR retroactive path (queued).
    """
    src = Path(body.source_path)
    if not src.is_file():
        raise HTTPException(404, f"source not found: {src}")
    duration = get_duration_seconds(str(src))
    if duration <= 0:
        raise HTTPException(400, f"could not probe duration: {src}")

    anchors = await asyncio.to_thread(
        kill_anchors_for_video,
        str(src),
        duration,
        include_clusters=body.include_clusters,
    )
    if not anchors:
        return {
            "ok": True,
            "source_path": str(src),
            "clips": [],
            "reason": (
                "no anchors -- source not in Outplayed dispatch log "
                "(rotated / user-clipped / non-Val)"
            ),
        }

    windows = auto_windows_from_anchors(anchors, duration_seconds=duration)
    prefix = _sanitize_name(body.name_prefix) if body.name_prefix else _sanitize_name(src.stem)
    out_dir = settings.workspace_dir / "trims"
    out_dir.mkdir(parents=True, exist_ok=True)

    clips = []
    for idx, w in enumerate(windows):
        stem = f"{prefix}_{idx:02d}_{w.event_type or 'kill'}"
        out_path = out_dir / f"{stem}.mp4"
        ok, error = await asyncio.to_thread(
            trim_clip, str(src), str(out_path), w.start_seconds, w.end_seconds
        )
        if not ok:
            raise HTTPException(500, f"trim failed at idx {idx}: {(error or '')[:300]}")
        clips.append(
            {
                "output_path": str(out_path),
                "start_seconds": w.start_seconds,
                "end_seconds": w.end_seconds,
                "anchor_seconds": w.anchor_seconds,
                "event_type": w.event_type,
            }
        )
    return {
        "ok": True,
        "source_path": str(src),
        "count": len(clips),
        "clips": clips,
    }


# ---------------------------------------------------------------------
# Caption any video file by path (raw / trimmed / compiled). Companion
# to `trim_source_to_clip` -- together they let jump-cut edits with
# overlays work on any file, not just indexed assets.
# ---------------------------------------------------------------------


class CaptionSegmentIn(BaseModel):
    """One drawtext overlay. Times are in the OUTPUT file's timeline
    (start_seconds=0 = first frame of the rendered clip)."""

    text: str
    start_seconds: float = Field(..., ge=0.0)
    end_seconds: float = Field(..., gt=0.0)
    style: dict | None = Field(
        default=None,
        description=(
            "Optional style preset override (fontsize, color, "
            "y_position, etc). See edits.resolve_caption_style."
        ),
    )
    font_path: str | None = Field(
        default=None,
        description=(
            "Per-segment font override. Pass "
            "`settings.caption_emoji_font_path` for emoji glyphs, or "
            "any other .ttf. Falls back to the caption default."
        ),
    )


class CaptionVideoBody(BaseModel):
    """Overlay captions onto any .mp4 by path. Sync -- returns the
    output path when the encode finishes."""

    source_path: str = Field(...)
    segments: list[CaptionSegmentIn] = Field(..., min_length=1)
    name: str | None = Field(default=None)
    aspect: str = Field(
        default="9:16",
        description=(
            "Output aspect: '9:16', '16:9', 'source' (no change), or "
            "'9:16_hud' (HUD-preserving vertical; requires HUD_ASPECT_ENABLED)."
        ),
    )
    game: str | None = Field(
        default=None,
        description=(
            "Game name for '9:16_hud' layout variant (Valorant vs LoL). Ignored for other aspects."
        ),
    )


@router.post("/clips/caption_video")
async def caption_video_by_path(body: CaptionVideoBody):
    """Add drawtext overlays to any .mp4 file, no asset row required.

    Each segment can specify its own `font_path` -- pass the config's
    `caption_emoji_font_path` for emoji glyphs (rendered as monochrome
    outlines through drawtext), or any other .ttf. Different segments
    at the same timestamp can use different fonts (emoji glyph on the
    left, text on the right).
    """
    from ..edits import validate_edit_aspect

    try:
        validate_edit_aspect(body.aspect, hud_enabled=settings.hud_aspect_enabled)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    src = Path(body.source_path)
    if not src.is_file():
        raise HTTPException(404, f"source not found: {src}")

    # Reuse the sanitizer from the trim endpoint.
    stem = _sanitize_name(body.name) if body.name else f"{_sanitize_name(src.stem)}_captioned"
    out_dir = settings.workspace_dir / "captions"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{stem}.mp4"

    # apply_caption expects times in source-VOD seconds and takes a
    # (start, end) window across the whole source. For raw-path use we
    # trim from 0 to the file's end -- the caller placed the overlay
    # times in output-timeline seconds already.
    from ..candidates.probe import get_duration_seconds

    duration = get_duration_seconds(str(src))
    if duration <= 0:
        raise HTTPException(400, f"could not probe duration: {src}")

    segments = [s.model_dump() for s in body.segments]
    cap_scale = settings.caption_fontsize_scale_enabled
    ok, err = await asyncio.to_thread(
        apply_caption,
        str(src),
        str(out_path),
        start=0.0,
        end=duration,
        segments=segments,
        aspect=body.aspect,
        game=body.game,
        scale_enabled=cap_scale,
    )
    if not ok:
        raise HTTPException(500, f"caption failed: {(err or '')[:500]}")
    return {"ok": True, "output_path": str(out_path)}
