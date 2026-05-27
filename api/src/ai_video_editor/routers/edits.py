"""Editing primitives (Phase 3, Milestone A).

POST /edit/zoom    — crop+scale on an ROI
POST /edit/caption — burn transcript text over the clip
POST /edit/focus   — spotlight: dim except for a soft circle

All three take an asset + sub-range and produce a new .mp4 in
WORKSPACE/edits/<asset-stem>/. Background jobs, same /jobs/{id} poll
pattern as the rest of the app. Source files are read-only.
"""

import asyncio
import contextlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..compile import (
    add_caption_segment as spec_add_caption_segment,
)
from ..compile import (
    add_effect as spec_add_effect,
)
from ..compile import (
    build_compilation,
    load_spec,
    reel_positions,
    render_spec,
    resolve_clip_ref,
    save_spec,
)
from ..compile import (
    clear_intro_clip as spec_clear_intro_clip,
)
from ..compile import (
    extend_clip as spec_extend_clip,
)
from ..compile import (
    insert_clip as spec_insert_clip,
)
from ..compile import (
    insert_intro_at_position as spec_insert_intro_at_position,
)
from ..compile import (
    remove_caption_segment as spec_remove_caption_segment,
)
from ..compile import (
    remove_clip as spec_remove_clip,
)
from ..compile import (
    reorder_clips as spec_reorder_clips,
)
from ..compile import (
    set_caption_mode as spec_set_caption_mode,
)
from ..compile import (
    set_clip_captions as spec_set_clip_captions,
)
from ..compile import (
    set_clip_numbers as spec_set_clip_numbers,
)
from ..compile import (
    set_intro_clip as spec_set_intro_clip,
)
from ..compile import (
    tiktokify_clip as spec_tiktokify_clip,
)
from ..compile_cleanup import cleanup_compilation as _cleanup_compilation
from ..compile_journal import (
    RevertError,
    append_journal,
    revert_steps,
    revert_to_version,
    summarise_journal,
)
from ..config import settings
from ..database import get_db
from ..edits import apply_caption, apply_focus, apply_zoom
from ..feedback import log_event as _log_feedback
from ..intros import (
    get_default_intro_name,
    intro_folder,
    intro_output_path,
    load_intro,
)
from ..models import RankResponse

router = APIRouter(tags=["edit"], prefix="/edit")

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


def _out_path(asset_filename: str, kind: str) -> str:
    stem = Path(asset_filename).stem
    folder = settings.workspace_dir / "edits" / stem
    folder.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return (folder / f"{kind}_{ts}.mp4").as_posix()


# --- shared request shape ---
class _BaseEditRequest(BaseModel):
    asset_id: str
    start_seconds: float = Field(0.0, ge=0.0)
    end_seconds: float = Field(..., gt=0.0)
    aspect: Literal["16:9", "9:16"] = "16:9"


class ZoomRequest(_BaseEditRequest):
    factor: float = Field(2.0, gt=1.0)
    roi: str | dict = "center"  # preset name OR {"x","y","w","h"} fractions


class CaptionRequest(_BaseEditRequest):
    text: str | None = None  # override; defaults to the transcript window


class FocusRequest(_BaseEditRequest):
    x: float = Field(0.5, ge=0.0, le=1.0)  # fractional center
    y: float = Field(0.5, ge=0.0, le=1.0)
    radius: float = Field(0.2, gt=0.0, le=1.0)  # x min(w, h)
    dim: float = Field(0.3, ge=0.0, le=1.0)  # outside-circle brightness


# --- core job wrapper ---
async def _run_edit_job(
    job_id: str, edit_id: str, asset: dict, kind: str, params: dict, output_path: str, work
) -> None:
    db = await get_db()
    try:
        await db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        await db.commit()
        ok, err = await asyncio.to_thread(work)
        if not ok:
            await _finish(db, job_id, output_path=None, error=err)
            return
        # Persist the edit row on success.
        await db.execute(
            "INSERT INTO edits (id, asset_id, kind, params, output_path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                edit_id,
                asset["id"],
                kind,
                json.dumps(params),
                output_path,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()
        await _finish(db, job_id, output_path=output_path, error=None)
    except Exception as e:
        await _finish(db, job_id, output_path=None, error=str(e)[:2000])
    finally:
        await db.close()


async def _enqueue_edit(asset_id: str, kind: str) -> tuple[dict, str, str, str]:
    """Common preamble: load asset, create job + edit ids, persist job row."""
    db = await get_db()
    try:
        asset = await _load_asset(db, asset_id)
        job_id = str(uuid.uuid4())
        edit_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, ?, 'pending', ?)",
            (job_id, f"edit_{kind}", datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()
    output_path = _out_path(asset["filename"], kind)
    return asset, job_id, edit_id, output_path


# --- endpoints ---
@router.post("/zoom", response_model=RankResponse)
async def zoom(req: ZoomRequest):
    if req.end_seconds <= req.start_seconds:
        raise HTTPException(400, "end_seconds must be greater than start_seconds")
    asset, job_id, edit_id, out_path = await _enqueue_edit(req.asset_id, "zoom")
    params = req.model_dump()

    def work():
        return apply_zoom(
            asset["path"],
            out_path,
            start=req.start_seconds,
            end=req.end_seconds,
            factor=req.factor,
            roi=req.roi,
            aspect=req.aspect,
        )

    _track(_run_edit_job(job_id, edit_id, asset, "zoom", params, out_path, work))
    return RankResponse(job_id=job_id)


@router.post("/caption", response_model=RankResponse)
async def caption(req: CaptionRequest):
    if req.end_seconds <= req.start_seconds:
        raise HTTPException(400, "end_seconds must be greater than start_seconds")
    asset, job_id, edit_id, out_path = await _enqueue_edit(req.asset_id, "caption")

    # Default: auto-pull transcript segments overlapping the clip window.
    if req.text is None:
        db = await get_db()
        try:
            rows = await db.execute_fetchall(
                "SELECT start_seconds, end_seconds, text FROM transcripts "
                "WHERE video_id = ? AND end_seconds >= ? AND start_seconds <= ? "
                "ORDER BY start_seconds",
                (req.asset_id, req.start_seconds, req.end_seconds),
            )
            segments = [dict(r) for r in rows]
        finally:
            await db.close()
    else:
        # Single synthetic segment spanning the whole clip
        segments = [
            {
                "start_seconds": req.start_seconds,
                "end_seconds": req.end_seconds,
                "text": req.text,
            }
        ]

    params = req.model_dump() | {"segment_count": len(segments)}

    def work():
        return apply_caption(
            asset["path"],
            out_path,
            start=req.start_seconds,
            end=req.end_seconds,
            segments=segments,
            aspect=req.aspect,
        )

    _track(_run_edit_job(job_id, edit_id, asset, "caption", params, out_path, work))
    return RankResponse(job_id=job_id)


@router.post("/focus", response_model=RankResponse)
async def focus(req: FocusRequest):
    if req.end_seconds <= req.start_seconds:
        raise HTTPException(400, "end_seconds must be greater than start_seconds")
    asset, job_id, edit_id, out_path = await _enqueue_edit(req.asset_id, "focus")
    params = req.model_dump()

    def work():
        return apply_focus(
            asset["path"],
            out_path,
            start=req.start_seconds,
            end=req.end_seconds,
            x_frac=req.x,
            y_frac=req.y,
            r_frac=req.radius,
            dim=req.dim,
            aspect=req.aspect,
        )

    _track(_run_edit_job(job_id, edit_id, asset, "focus", params, out_path, work))
    return RankResponse(job_id=job_id)


# --- /edit/compile (Milestone B) ---
class CompileRequest(BaseModel):
    asset_id: str
    aspect: Literal["16:9", "9:16"] = "16:9"
    # "hook" = highest-hype clip first, rest chronological. Default
    # because algorithm-driven retention favors the first 3 seconds
    # being the hottest moment.
    # "narrative" = three sections in recording order (intro/main/outro)
    # for long VODs that benefit from a story arc — see plan_clips.
    order: Literal["chronological", "hype", "hook", "narrative"] = "hook"
    limit: int | None = Field(None, gt=0)
    fade_seconds: float = Field(0.3, ge=0.0, le=2.0)
    music_path: str | None = None
    music_volume: float = Field(0.25, ge=0.0, le=1.0)
    # First render is labeled "#01", "#02"... so you can iterate by
    # position. Flip off via POST /edit/compile/{id}/labels {enabled:false}
    # when you're done.
    show_clip_numbers: bool = True


async def _run_compile_job(job_id: str, comp_id: str, asset: dict, req: CompileRequest) -> None:
    db = await get_db()
    try:
        await db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        await db.commit()

        rankings_path = settings.workspace_dir / "rankings" / f"{asset['id']}.json"
        if not rankings_path.exists():
            await _finish(
                db,
                job_id,
                output_path=None,
                error="No rankings — run POST /assets/{id}/rank first.",
            )
            return
        rankings = json.loads(rankings_path.read_text(encoding="utf-8"))

        seg_rows = await db.execute_fetchall(
            "SELECT start_seconds, end_seconds, text, words FROM transcripts "
            "WHERE video_id = ? ORDER BY start_seconds",
            (asset["id"],),
        )
        segments = []
        for r in seg_rows:
            d = dict(r)
            # `words` is JSON text in the DB; hydrate to a list so the
            # renderer's TikTok path can iterate without re-parsing.
            d["words"] = json.loads(d["words"]) if d.get("words") else []
            segments.append(d)

        cand_rows = await db.execute_fetchall(
            "SELECT * FROM highlight_candidates WHERE video_id = ? ORDER BY start_seconds",
            (asset["id"],),
        )
        candidates = []
        for row in cand_rows:
            d = dict(row)
            d["metadata"] = json.loads(d["metadata"]) if d.get("metadata") else None
            candidates.append(d)

        summary = await asyncio.to_thread(
            build_compilation,
            asset,
            rankings,
            segments,
            candidates=candidates,
            aspect=req.aspect,
            order=req.order,
            limit=req.limit,
            fade_seconds=req.fade_seconds,
            music_path=req.music_path,
            music_volume=req.music_volume,
            show_clip_numbers=req.show_clip_numbers,
        )

        # Journal the initial compile so revert has a v1 to walk back to.
        # Best-effort: a malformed `folder` or unreadable spec must not
        # fail the compile (the user's reel is already done).
        folder_path = Path(summary["folder"]) if summary.get("folder") else None
        if folder_path and (folder_path / "spec.json").is_file():
            with contextlib.suppress(Exception):
                initial_spec = load_spec(folder_path)
                append_journal(
                    folder_path,
                    initial_spec,
                    action="initial_compile",
                    details={"order": req.order, "limit": req.limit},
                )

        out_path = summary.get("output")
        await db.execute(
            "INSERT INTO compilations (id, asset_id, output_path, params, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                comp_id,
                asset["id"],
                out_path,
                json.dumps(req.model_dump()),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()

        if not summary.get("compiled"):
            err = summary.get("error") or "compile failed"
            await _finish(db, job_id, output_path=None, error=err)
            return
        msg = f"{summary['parts_rendered']}/{summary['kept_total']} -> {out_path}"
        await _finish(db, job_id, output_path=msg, error=None)
    except Exception as e:
        await _finish(db, job_id, output_path=None, error=str(e)[:2000])
    finally:
        await db.close()


class CompileResponse(BaseModel):
    """Initial-compile response — exposes the new compilation id so the
    caller can drive subsequent iterative edits without an extra lookup."""

    job_id: str
    compilation_id: str


@router.post("/compile", response_model=CompileResponse)
async def compile_highlights(req: CompileRequest):
    db = await get_db()
    try:
        asset = await _load_asset(db, req.asset_id)
        job_id = str(uuid.uuid4())
        comp_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, 'compile', 'pending', ?)",
            (job_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()
    _track(_run_compile_job(job_id, comp_id, asset, req))
    return CompileResponse(job_id=job_id, compilation_id=comp_id)


@router.get("/compile")
async def list_compilations(asset_id: str | None = None, limit: int = 20):
    """List recent compilations (optionally filtered by asset) so callers
    can find a `compilation_id` for iterative editing."""
    db = await get_db()
    try:
        if asset_id:
            rows = await db.execute_fetchall(
                "SELECT id, asset_id, output_path, created_at FROM compilations "
                "WHERE asset_id = ? ORDER BY created_at DESC LIMIT ?",
                (asset_id, limit),
            )
        else:
            rows = await db.execute_fetchall(
                "SELECT id, asset_id, output_path, created_at FROM compilations "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        return [dict(r) for r in rows]
    finally:
        await db.close()


@router.get("/compile/{compilation_id}")
async def get_compilation(compilation_id: str):
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM compilations WHERE id = ?", (compilation_id,)
        )
        if not rows:
            raise HTTPException(404, "Compilation not found")
        d = dict(rows[0])
        d["params"] = json.loads(d["params"])
        # Inline the index.json from the folder if available.
        if d.get("output_path"):
            idx = Path(d["output_path"]).parent / "index.json"
            if idx.exists():
                d["index"] = json.loads(idx.read_text(encoding="utf-8"))
        return d
    finally:
        await db.close()


# --- Iterative compile editing ------------------------------------------
async def _load_compilation_folder(compilation_id: str) -> Path:
    """Resolve a compilation id to its on-disk folder. 404s if unknown."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT output_path FROM compilations WHERE id = ?", (compilation_id,)
        )
    finally:
        await db.close()
    if not rows or not rows[0]["output_path"]:
        raise HTTPException(404, "Compilation not found or never rendered")
    return Path(rows[0]["output_path"]).parent


async def _update_compilation_output(compilation_id: str, output_path: str | None) -> None:
    db = await get_db()
    try:
        await db.execute(
            "UPDATE compilations SET output_path = ? WHERE id = ?",
            (output_path, compilation_id),
        )
        await db.commit()
    finally:
        await db.close()


def _mmss_from_seconds(s: float) -> str:
    s = int(s)
    return f"{s // 60}:{s % 60:02d}"


def _summarise_clips(spec: dict) -> list[dict]:
    """Human-readable per-clip listing including reel + source timestamps.

    Includes the full `caption_segments` so the caption editor in the
    webapp can render + edit them without a separate API roundtrip.
    Segments stay small (avg ~5 lines x 100 chars per clip); keeping
    them inline avoids N+1 fetches for the typical filmstrip render.
    """
    out: list[dict] = []
    for (rs, re_), c in zip(reel_positions(spec), spec.get("clips", []), strict=True):
        out.append(
            {
                "index": len(out) + 1,
                "id": c["id"],
                "reel": f"{_mmss_from_seconds(rs)}-{_mmss_from_seconds(re_)}",
                "source": (
                    f"{_mmss_from_seconds(c['start_seconds'])}"
                    f"-{_mmss_from_seconds(c['end_seconds'])}"
                ),
                "duration": round(re_ - rs, 2),
                "event": c.get("event_type"),
                "effects": list(c.get("effects") or []),
                "caption_segments": list(c.get("caption_segments") or []),
                "caption_mode": c.get("caption_mode") or "segment",
            }
        )
    return out


@router.get("/compile/{compilation_id}/clips")
async def list_compilation_clips(compilation_id: str):
    """Show the clips in a compilation with reel + source timestamps so
    the caller can pick one to edit by index, reel time, or source time."""
    folder = await _load_compilation_folder(compilation_id)
    spec = load_spec(folder)
    return {"compilation_id": compilation_id, "clips": _summarise_clips(spec)}


class _ClipRefBody(BaseModel):
    clip_ref: str  # accepts int-string, UUID prefix, "M:SS" (reel) or source time


class AddEffectBody(_ClipRefBody):
    kind: Literal["zoom", "focus", "caption"]
    # zoom
    factor: float | None = None
    roi: str | dict | None = None
    # focus
    x: float | None = None
    y: float | None = None
    radius: float | None = None
    dim: float | None = None
    # caption
    text: str | None = None


class ExtendBody(_ClipRefBody):
    before: float = Field(0.0, ge=0.0)
    after: float = Field(0.0, ge=0.0)


def _do_edit(
    folder: Path,
    mutator,
    compilation_id: str,
    *,
    action: str = "edit",
    details: dict | None = None,
) -> dict:
    """Load spec, apply mutator (returns new spec + dirty ids), save spec,
    re-render, journal the new state. Synchronous because per-clip
    re-encodes in seconds.

    `action` and `details` are recorded in the spec history journal so
    `list_compilation_history` can show *what* the user did, not just
    *that* they did something.
    """
    spec = load_spec(folder)
    new_spec, dirty = mutator(spec)
    save_spec(folder, new_spec)
    summary = render_spec(new_spec, folder, dirty_clip_ids=dirty)
    summary["clips"] = _summarise_clips(new_spec)
    # Journal the post-edit state. Append is best-effort — a journal
    # write failure doesn't break the edit (the spec is already saved
    # and the render is already done).
    append_journal(folder, new_spec, action=action, details=details)
    return summary


def _effect_from_body(body: AddEffectBody) -> dict:
    """Pick only the fields relevant to the requested effect kind."""
    if body.kind == "zoom":
        e: dict = {"kind": "zoom"}
        if body.factor is not None:
            e["factor"] = body.factor
        if body.roi is not None:
            e["roi"] = body.roi
        return e
    if body.kind == "focus":
        e = {"kind": "focus"}
        for k in ("x", "y", "radius", "dim"):
            v = getattr(body, k)
            if v is not None:
                e[k] = v
        return e
    # caption
    if not body.text:
        raise HTTPException(400, "caption effect requires `text`")
    return {"kind": "caption", "text": body.text}


@router.post("/compile/{compilation_id}/effect")
async def add_clip_effect(compilation_id: str, body: AddEffectBody):
    """Add a zoom/focus/caption effect to a clip; re-render and return
    the updated spec + output path."""
    folder = await _load_compilation_folder(compilation_id)
    spec = load_spec(folder)
    try:
        idx = resolve_clip_ref(spec, body.clip_ref)
    except KeyError as e:
        raise HTTPException(400, str(e)) from None
    effect = _effect_from_body(body)

    def mutator(s: dict) -> tuple[dict, set[str]]:
        return spec_add_effect(s, idx, effect)

    summary = await asyncio.to_thread(
        _do_edit,
        folder,
        mutator,
        compilation_id,
        action=f"add_effect:{body.kind}",
        details={"clip_ref": body.clip_ref, "effect": effect},
    )
    await _update_compilation_output(compilation_id, summary.get("output"))
    return summary


@router.post("/compile/{compilation_id}/extend")
async def extend_compilation_clip(compilation_id: str, body: ExtendBody):
    """Grow a clip's source window (and re-render it). before/after are
    seconds to add to the clip's start and end respectively."""
    folder = await _load_compilation_folder(compilation_id)
    spec = load_spec(folder)
    try:
        idx = resolve_clip_ref(spec, body.clip_ref)
    except KeyError as e:
        raise HTTPException(400, str(e)) from None

    # Snapshot the affected clip's metadata BEFORE the mutation so the
    # feedback row captures the event_type at decision time.
    target_clip = (spec.get("clips") or [])[idx] if 0 <= idx < len(spec.get("clips") or []) else {}
    target_clip_id = target_clip.get("id")
    target_event_type = target_clip.get("event_type")

    def mutator(s: dict) -> tuple[dict, set[str]]:
        return spec_extend_clip(s, idx, body.before, body.after)

    summary = await asyncio.to_thread(
        _do_edit,
        folder,
        mutator,
        compilation_id,
        action="extend_clip",
        details={"clip_ref": body.clip_ref, "before": body.before, "after": body.after},
    )
    await _update_compilation_output(compilation_id, summary.get("output"))

    # User-edit feedback signal: tunes future per-event windows.
    fb_db = await get_db()
    try:
        await _log_feedback(
            fb_db,
            compilation_id=compilation_id,
            clip_id=target_clip_id,
            event_type=target_event_type,
            action="extend",
            delta_before=body.before,
            delta_after=body.after,
            payload={"clip_ref": body.clip_ref},
        )
    finally:
        await fb_db.close()
    return summary


class CaptionModeBody(BaseModel):
    clip_ref: str
    mode: Literal["segment", "tiktok"]


@router.post("/compile/{compilation_id}/caption_mode")
async def set_compilation_caption_mode(compilation_id: str, body: CaptionModeBody):
    """Switch a single clip's caption style.

    `"tiktok"` now ROUTES to `tiktokify_clip` — explodes segments into
    word-segments with the tiktok preset. Same data model, no separate
    code path. `"segment"` clears the legacy `caption_mode` field and
    leaves segments at whatever shape they currently are (use
    `set_clip_captions` or `revert` to truly undo a tiktokify).

    Kept for backward compat — new callers should use
    `/clip_captions/tiktokify` directly for the explicit transformation.
    """
    folder = await _load_compilation_folder(compilation_id)
    spec_now = load_spec(folder)
    try:
        idx = resolve_clip_ref(spec_now, body.clip_ref)
    except KeyError as e:
        raise HTTPException(400, str(e)) from None

    if body.mode == "tiktok":
        # Route to the new transformation — same effect, new data model.
        def mutator(s: dict) -> tuple[dict, set[str]]:
            return spec_tiktokify_clip(s, idx)
    else:
        # "segment": clear the legacy caption_mode field; leave segments
        # alone (tiktokify is destructive, no inverse without journal revert).
        def mutator(s: dict) -> tuple[dict, set[str]]:
            return spec_set_caption_mode(s, idx, "segment")

    summary = await asyncio.to_thread(
        _do_edit,
        folder,
        mutator,
        compilation_id,
        action=f"caption_mode:{body.mode}",
        details={"clip_ref": body.clip_ref},
    )
    await _update_compilation_output(compilation_id, summary.get("output"))
    return summary


class LabelsBody(BaseModel):
    enabled: bool


@router.post("/compile/{compilation_id}/labels")
async def toggle_compilation_labels(compilation_id: str, body: LabelsBody):
    """Toggle the per-clip "#NN" iteration overlay. ALL clips are
    re-rendered (different filter chain) but cached parts for the
    other label state are kept on disk for quick flipping back."""
    folder = await _load_compilation_folder(compilation_id)

    def mutator(s: dict) -> tuple[dict, set[str]]:
        return spec_set_clip_numbers(s, body.enabled)

    summary = await asyncio.to_thread(
        _do_edit,
        folder,
        mutator,
        compilation_id,
        action=f"labels:{'on' if body.enabled else 'off'}",
    )
    await _update_compilation_output(compilation_id, summary.get("output"))
    return summary


class InsertClipBody(BaseModel):
    """Body for inserting a brand-new clip into an existing compilation."""

    asset_id: str
    start_seconds: float = Field(..., ge=0.0)
    end_seconds: float = Field(..., gt=0.0)
    # 1-based reel position; omit for chronological-by-source within asset.
    position: int | None = Field(None, ge=1)
    event_type: str = "manual"
    # Caption override. When omitted we auto-pull transcript segments
    # overlapping [start, end] for `asset_id` (same as compile).
    text: str | None = None


@router.post("/compile/{compilation_id}/insert")
async def insert_compilation_clip(compilation_id: str, body: InsertClipBody):
    """Insert a clip from an arbitrary source range into an existing reel.

    The compilation isn't restricted to its 'primary' asset — you can
    insert from any indexed recording. Caption segments auto-pull from
    that asset's transcript unless `text` is supplied.
    """
    if body.end_seconds <= body.start_seconds:
        raise HTTPException(400, "end_seconds must be greater than start_seconds")

    folder = await _load_compilation_folder(compilation_id)

    db = await get_db()
    try:
        asset = await _load_asset(db, body.asset_id)
        if body.text is None:
            rows = await db.execute_fetchall(
                "SELECT start_seconds, end_seconds, text FROM transcripts "
                "WHERE video_id = ? AND end_seconds >= ? AND start_seconds <= ? "
                "ORDER BY start_seconds",
                (body.asset_id, body.start_seconds, body.end_seconds),
            )
            segments = [dict(r) for r in rows]
        else:
            segments = [
                {
                    "start_seconds": body.start_seconds,
                    "end_seconds": body.end_seconds,
                    "text": body.text,
                }
            ]
    finally:
        await db.close()

    def mutator(s: dict) -> tuple[dict, set[str]]:
        return spec_insert_clip(
            s,
            asset_id=asset["id"],
            asset_path=asset["path"],
            asset_filename=asset.get("filename"),
            start_seconds=body.start_seconds,
            end_seconds=body.end_seconds,
            caption_segments=segments,
            event_type=body.event_type,
            position=body.position,
        )

    summary = await asyncio.to_thread(
        _do_edit,
        folder,
        mutator,
        compilation_id,
        action="insert_clip",
        details={
            "asset_id": body.asset_id,
            "start_seconds": body.start_seconds,
            "end_seconds": body.end_seconds,
            "position": body.position,
            "event_type": body.event_type,
        },
    )
    await _update_compilation_output(compilation_id, summary.get("output"))
    return summary


@router.post("/compile/{compilation_id}/remove")
async def remove_compilation_clip(compilation_id: str, body: _ClipRefBody):
    """Drop a clip from the reel and re-concat."""
    folder = await _load_compilation_folder(compilation_id)
    spec = load_spec(folder)
    try:
        idx = resolve_clip_ref(spec, body.clip_ref)
    except KeyError as e:
        raise HTTPException(400, str(e)) from None

    # Snapshot the removed clip's metadata before mutation (it'll be gone
    # afterward). Negative signal: "ranker over-rated this kind of moment."
    target_clip = (spec.get("clips") or [])[idx] if 0 <= idx < len(spec.get("clips") or []) else {}
    target_clip_id = target_clip.get("id")
    target_event_type = target_clip.get("event_type")

    def mutator(s: dict) -> tuple[dict, set[str]]:
        return spec_remove_clip(s, idx)

    summary = await asyncio.to_thread(
        _do_edit,
        folder,
        mutator,
        compilation_id,
        action="remove_clip",
        details={"clip_ref": body.clip_ref},
    )
    await _update_compilation_output(compilation_id, summary.get("output"))

    fb_db = await get_db()
    try:
        await _log_feedback(
            fb_db,
            compilation_id=compilation_id,
            clip_id=target_clip_id,
            event_type=target_event_type,
            action="remove_clip",
            payload={"clip_ref": body.clip_ref},
        )
    finally:
        await fb_db.close()
    return summary


class SetIntroBody(BaseModel):
    # Omit to use the workspace's default intro (see /intros/default).
    intro_name: str | None = None


def _resolve_intro_or_404(intro_name: str | None) -> tuple[str, str, float]:
    """Resolve `intro_name` (falling back to the workspace default) to
    `(name, mp4_path, duration)`. Raises HTTPException on missing or
    not-yet-rendered intros — common helper for both prepend and
    arbitrary-position insertion endpoints."""
    name = intro_name or get_default_intro_name()
    if not name:
        raise HTTPException(
            400,
            "intro_name not provided and no default intro is set — "
            "either pass intro_name or POST /intros/default first",
        )
    if not intro_folder(name).is_dir():
        raise HTTPException(404, f"intro not found: {name}")
    cfg = load_intro(name)
    intro_mp4 = intro_output_path(name)
    if not intro_mp4.is_file():
        raise HTTPException(
            409,
            f"intro {name!r} has no rendered intro.mp4 — POST /intros/{name}/render first",
        )
    return name, intro_mp4.as_posix(), cfg.duration


@router.post("/compile/{compilation_id}/intro")
async def set_compilation_intro(compilation_id: str, body: SetIntroBody):
    """Prepend (or replace) a branded intro at the START of a reel.

    The intro must already exist under `WORKSPACE/intros/<name>/`
    (created via `POST /intros`). Applying a different intro to a
    reel that already has one REPLACES it rather than stacking.

    `intro_name` is optional — when omitted, the workspace's default
    intro (set via `POST /intros/default`) is used.
    """
    name, mp4_path, duration = _resolve_intro_or_404(body.intro_name)
    folder = await _load_compilation_folder(compilation_id)

    def mutator(s: dict) -> tuple[dict, set[str]]:
        return spec_set_intro_clip(
            s,
            intro_name=name,
            intro_path=mp4_path,
            duration=duration,
        )

    summary = await asyncio.to_thread(
        _do_edit,
        folder,
        mutator,
        compilation_id,
        action="set_intro",
        details={"intro_name": name},
    )
    await _update_compilation_output(compilation_id, summary.get("output"))
    return summary


class InsertIntroBody(BaseModel):
    """Body for inserting an intro between specific clips.

    - `intro_name` is optional; falls back to the workspace default.
    - EITHER pass `after_clip` (insert AFTER that clip — natural for
      "add the intro after clip #3") OR `position` (1-based reel
      position; position=1 means prepend).
    """

    intro_name: str | None = None
    after_clip: str | None = None
    position: int | None = Field(None, ge=1)


class CaptionWord(BaseModel):
    word: str
    start: float
    end: float


class CaptionSegment(BaseModel):
    start_seconds: float
    end_seconds: float
    text: str
    # When omitted, the renderer even-splits the new text across the
    # segment's duration. Preserve only when the segment's TEXT is
    # unchanged from the original Whisper output.
    words: list[CaptionWord] | None = None


class SetClipCaptionsBody(BaseModel):
    clip_ref: str
    segments: list[CaptionSegment]


@router.post("/compile/{compilation_id}/clip_captions")
async def set_compilation_clip_captions(compilation_id: str, body: SetClipCaptionsBody):
    """Replace a clip's caption text with user-edited segments.

    The caller sends the FULL updated segment list (not a patch) so
    additions, deletions, and edits are unambiguous. Each segment
    carries its own time range plus the new text; word timings are
    optional (drop them when the text changed — renderer will
    even-split).

    The clip re-renders with the new captions burnt in. Whisper's
    master transcript in the DB is NOT modified — this edit is local
    to the compilation's spec.json. Different reels of the same
    source can have independently-edited captions.
    """
    folder = await _load_compilation_folder(compilation_id)
    spec = load_spec(folder)
    try:
        idx = resolve_clip_ref(spec, body.clip_ref)
    except KeyError as e:
        raise HTTPException(400, str(e)) from None

    segments = [s.model_dump(exclude_none=True) for s in body.segments]

    def mutator(s: dict) -> tuple[dict, set[str]]:
        return spec_set_clip_captions(s, idx, segments)

    summary = await asyncio.to_thread(
        _do_edit,
        folder,
        mutator,
        compilation_id,
        action="edit_captions",
        details={"clip_ref": body.clip_ref, "segment_count": len(segments)},
    )
    await _update_compilation_output(compilation_id, summary.get("output"))
    return summary


class CaptionStyleBody(BaseModel):
    """Per-segment style — preset name + optional overrides. All fields
    are optional; renderer fills defaults from the preset table."""

    preset: Literal["default", "tiktok"] | None = None
    fontsize: int | None = None
    y_position: str | None = None
    color: str | None = None
    border_width: int | None = None
    border_color: str | None = None


class AddCaptionBody(BaseModel):
    clip_ref: str
    start_seconds: float
    end_seconds: float
    text: str
    style: CaptionStyleBody | None = None


@router.post("/compile/{compilation_id}/clip_captions/add")
async def add_caption_to_clip(compilation_id: str, body: AddCaptionBody):
    """Insert a single caption segment into a clip.

    Specify start/end in CLIP source seconds (the same coordinate space
    as the clip's existing caption_segments). The new segment is
    inserted in sorted order and the clip re-renders.

    `style` is optional — omit for the default look (bottom-center),
    or pass `{preset: "tiktok"}` for big top-center text. Override
    individual fields for fine control.
    """
    folder = await _load_compilation_folder(compilation_id)
    spec = load_spec(folder)
    try:
        idx = resolve_clip_ref(spec, body.clip_ref)
    except KeyError as e:
        raise HTTPException(400, str(e)) from None
    style = body.style.model_dump(exclude_none=True) if body.style else None

    def mutator(s: dict) -> tuple[dict, set[str]]:
        return spec_add_caption_segment(
            s,
            idx,
            start_seconds=body.start_seconds,
            end_seconds=body.end_seconds,
            text=body.text,
            style=style,
        )

    summary = await asyncio.to_thread(
        _do_edit,
        folder,
        mutator,
        compilation_id,
        action="add_caption",
        details={
            "clip_ref": body.clip_ref,
            "start_seconds": body.start_seconds,
            "end_seconds": body.end_seconds,
            "text_preview": body.text[:32],
        },
    )
    await _update_compilation_output(compilation_id, summary.get("output"))
    return summary


class RemoveCaptionBody(BaseModel):
    clip_ref: str
    segment_index: int = Field(..., ge=0)


@router.post("/compile/{compilation_id}/clip_captions/remove")
async def remove_caption_from_clip(compilation_id: str, body: RemoveCaptionBody):
    """Delete a single caption segment from a clip by its 0-based index.

    Out-of-range indexes are silently ignored (idempotent — a stale UI
    index doesn't crash the edit).
    """
    folder = await _load_compilation_folder(compilation_id)
    spec = load_spec(folder)
    try:
        idx = resolve_clip_ref(spec, body.clip_ref)
    except KeyError as e:
        raise HTTPException(400, str(e)) from None

    def mutator(s: dict) -> tuple[dict, set[str]]:
        return spec_remove_caption_segment(s, idx, body.segment_index)

    summary = await asyncio.to_thread(
        _do_edit,
        folder,
        mutator,
        compilation_id,
        action="remove_caption",
        details={"clip_ref": body.clip_ref, "segment_index": body.segment_index},
    )
    await _update_compilation_output(compilation_id, summary.get("output"))
    return summary


@router.post("/compile/{compilation_id}/clip_captions/tiktokify")
async def tiktokify_compilation_clip(compilation_id: str, body: _ClipRefBody):
    """Transform a clip's captions into TikTok-style word segments.

    Explodes each existing segment into one-segment-per-word and tags
    each with the `tiktok` style preset. The renderer treats them like
    any other styled segments — no separate code path.

    Replaces the legacy `set_caption_mode` flow for going INTO tiktok
    mode. To go back to "normal" captions, edit individual segments or
    use `set_clip_captions` to replace the whole list.
    """
    folder = await _load_compilation_folder(compilation_id)
    spec = load_spec(folder)
    try:
        idx = resolve_clip_ref(spec, body.clip_ref)
    except KeyError as e:
        raise HTTPException(400, str(e)) from None

    def mutator(s: dict) -> tuple[dict, set[str]]:
        return spec_tiktokify_clip(s, idx)

    summary = await asyncio.to_thread(
        _do_edit,
        folder,
        mutator,
        compilation_id,
        action="tiktokify",
        details={"clip_ref": body.clip_ref},
    )
    await _update_compilation_output(compilation_id, summary.get("output"))
    return summary


class ReorderBody(BaseModel):
    mode: Literal["chronological", "hype", "hook", "funny", "story"]


@router.post("/compile/{compilation_id}/reorder")
async def reorder_compilation_clips(compilation_id: str, body: ReorderBody):
    """Reorder the non-intro clips in a compilation by a scoring mode.

    Intro clips (event_type=="intro") stay in their original positions
    — they're chapter-card markers, not gameplay. The reorder shuffles
    only the clips between/around them.

    Modes:
      - chronological: story order (by source start_seconds)
      - hype: highest-hype first, descending
      - funny: highest funny_score first
      - story: highest story_score first
      - hook: top-hype clip first, rest in chronological after

    The clip cards stay byte-identical (cache hit) when labels are
    off; with labels on, every shuffled clip re-encodes to get the
    new #NN overlay. Either way the concat reflects the new order.
    """
    folder = await _load_compilation_folder(compilation_id)

    def mutator(s: dict) -> tuple[dict, set[str]]:
        return spec_reorder_clips(s, body.mode)

    summary = await asyncio.to_thread(
        _do_edit,
        folder,
        mutator,
        compilation_id,
        action=f"reorder:{body.mode}",
        details={"mode": body.mode},
    )
    await _update_compilation_output(compilation_id, summary.get("output"))
    return summary


@router.post("/compile/{compilation_id}/insert_intro")
async def insert_compilation_intro(compilation_id: str, body: InsertIntroBody):
    """Insert a branded intro at an arbitrary position in the reel.

    Use this for chapter cards / transitions between gameplay clips
    (e.g. "add the intro after clip #3"). For the standard
    prepend-at-start case use `POST /intro` instead — it has
    different replace-vs-stack semantics.

    Resolution order for the target position:
      1. If `position` is given, insert at that 1-based reel position.
      2. Else if `after_clip` is given, resolve via `clip_ref` rules
         (index / UUID prefix / time string) and insert AFTER it.
      3. Else default to appending to the end of the reel.
    """
    if body.after_clip and body.position is not None:
        raise HTTPException(400, "pass only one of `after_clip` or `position`")

    name, mp4_path, duration = _resolve_intro_or_404(body.intro_name)
    folder = await _load_compilation_folder(compilation_id)
    spec = load_spec(folder)

    if body.position is not None:
        position = body.position
    elif body.after_clip:
        try:
            idx = resolve_clip_ref(spec, body.after_clip)
        except KeyError as e:
            raise HTTPException(400, str(e)) from None
        position = idx + 2  # idx is 0-based; +1 to make 1-based, +1 for "after"
    else:
        position = len(spec.get("clips") or []) + 1

    def mutator(s: dict) -> tuple[dict, set[str]]:
        return spec_insert_intro_at_position(
            s,
            intro_name=name,
            intro_path=mp4_path,
            duration=duration,
            position=position,
        )

    summary = await asyncio.to_thread(
        _do_edit,
        folder,
        mutator,
        compilation_id,
        action="insert_intro_at_position",
        details={
            "intro_name": name,
            "position": position,
            "after_clip": body.after_clip,
        },
    )
    await _update_compilation_output(compilation_id, summary.get("output"))
    return summary


@router.delete("/compile/{compilation_id}/intro")
async def clear_compilation_intro(compilation_id: str):
    """Remove the intro clip (if one is present). No-op if there isn't one."""
    folder = await _load_compilation_folder(compilation_id)

    def mutator(s: dict) -> tuple[dict, set[str]]:
        return spec_clear_intro_clip(s)

    summary = await asyncio.to_thread(
        _do_edit, folder, mutator, compilation_id, action="clear_intro"
    )
    await _update_compilation_output(compilation_id, summary.get("output"))
    return summary


@router.get("/compile/{compilation_id}/history")
async def list_compilation_history(compilation_id: str):
    """Show the spec edit journal — every change made since the
    initial compile, oldest first. Each entry has version, timestamp,
    action, action details, and the clip count at that point.

    Use this to find a `version` to pass to `/revert` when you want
    to roll back a specific change.
    """
    folder = await _load_compilation_folder(compilation_id)
    return {
        "compilation_id": compilation_id,
        "history": summarise_journal(folder),
    }


class RevertBody(BaseModel):
    """Revert can target either an explicit version (1-based) OR walk
    back N steps. Exactly one of `to_version` / `steps` should be set;
    if both are provided `to_version` wins."""

    to_version: int | None = Field(None, ge=1)
    steps: int = Field(1, ge=1)


@router.post("/compile/{compilation_id}/revert")
async def revert_compilation(compilation_id: str, body: RevertBody):
    """Undo edits by restoring a previous spec snapshot from the journal.

    Re-renders after restoration so `compilation.mp4` matches the
    restored spec. The revert is itself journaled, so the revert is
    also undoable.
    """
    folder = await _load_compilation_folder(compilation_id)
    try:
        if body.to_version is not None:
            restored = await asyncio.to_thread(revert_to_version, folder, body.to_version)
            target_version = body.to_version
        else:
            restored, target_version = await asyncio.to_thread(revert_steps, folder, body.steps)
    except RevertError as exc:
        raise HTTPException(400, str(exc)) from None

    # Re-render the restored spec. Mark every clip dirty since the
    # cached parts may not match the restored spec's effects/order.
    dirty = {c.get("id") for c in (restored.get("clips") or []) if c.get("id")}
    summary = await asyncio.to_thread(render_spec, restored, folder, dirty)
    summary["clips"] = _summarise_clips(restored)
    summary["reverted_to_version"] = target_version

    # Journal the revert itself so it's also undoable.
    append_journal(
        folder,
        restored,
        action="revert",
        details={"to_version": target_version},
    )
    await _update_compilation_output(compilation_id, summary.get("output"))

    # Feedback: a revert is a strong negative signal on whatever was undone.
    fb_db = await get_db()
    try:
        await _log_feedback(
            fb_db,
            compilation_id=compilation_id,
            action="revert",
            payload={"to_version": target_version},
        )
    finally:
        await fb_db.close()
    return summary


@router.post("/compile/{compilation_id}/thumbnail")
async def regenerate_thumbnail(compilation_id: str):
    """Re-extract the thumbnail from the current `compilation.mp4`.

    Auto-runs on every successful render; this endpoint exists for the
    case where you want to refresh the thumbnail without re-rendering
    (e.g. you edited the spec's `hype_score` values by hand).
    """
    from ..thumbnail import safe_extract_thumbnail

    folder = await _load_compilation_folder(compilation_id)
    spec = load_spec(folder)
    video = folder / "compilation.mp4"
    result = await asyncio.to_thread(safe_extract_thumbnail, folder, spec, video)
    return {"compilation_id": compilation_id, **result}


@router.post("/compile/{compilation_id}/thumbnails/regenerate")
async def regenerate_clip_thumbnails(compilation_id: str, force: bool = False):
    """Generate per-clip thumbnails for the webapp's filmstrip.

    Auto-runs on every render now, but compilations made BEFORE the
    filmstrip feature existed have no clip thumbnails yet — this
    endpoint backfills them on demand. `force=True` re-extracts even
    files that already exist (use after swapping a source asset).
    """
    from ..thumbnail import safe_extract_clip_thumbnails

    folder = await _load_compilation_folder(compilation_id)
    spec = load_spec(folder)
    result = await asyncio.to_thread(safe_extract_clip_thumbnails, folder, spec, force=force)
    return {"compilation_id": compilation_id, **result}


class CleanupBody(BaseModel):
    # When True, the response lists files that *would* be deleted without
    # touching disk — handy for previewing before a destructive call.
    dry_run: bool = False


@router.post("/compile/{compilation_id}/cleanup")
async def cleanup_compilation_workspace(compilation_id: str, body: CleanupBody | None = None):
    """Prune orphaned cached part files in `_parts/`.

    Auto-runs after every iterative edit via `render_spec`; this
    endpoint exposes the same operation for explicit invocation
    (a forced sweep after batch edits, or a `dry_run=True` preview
    of what auto-cleanup would remove).
    """
    folder = await _load_compilation_folder(compilation_id)
    dry_run = bool(body.dry_run) if body else False
    # Sync call — pure filesystem operations, no ffmpeg, microseconds.
    report = _cleanup_compilation(folder, dry_run=dry_run)
    return {"compilation_id": compilation_id, **report.to_dict()}


@router.get("/{edit_id}")
async def get_edit(edit_id: str):
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM edits WHERE id = ?", (edit_id,))
        if not rows:
            raise HTTPException(404, "Edit not found")
        d = dict(rows[0])
        d["params"] = json.loads(d["params"])
        return d
    finally:
        await db.close()
