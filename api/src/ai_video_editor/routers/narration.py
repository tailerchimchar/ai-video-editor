"""Narration HTTP API (contract: docs/specs/spec-narration.md).

    GET    /narration/{kind}/{key}/frames           frame-strip sprite (cached per video)
    GET    /narration/{kind}/{key}/peaks            game-audio peaks (cached per video)
    GET    /narration/{kind}/{key}/markers          chapters, games, inserts and owner cues
    GET    /narration/{kind}/{key}                  session view (creates it on first call)
    POST   /narration/{kind}/{key}/takes            upload one take (raw webm body)
    DELETE /narration/{kind}/{key}/takes/{take_id}  remove a take
    PATCH  /narration/{kind}/{key}/takes/{take_id}  trim / nudge / gain / mute a take
    POST   /narration/{kind}/{key}/takes/{take_id}/split?at_s=  split a take in two
    PUT    /narration/{kind}/{key}/camera           set the camera box
    PUT    /narration/{kind}/{key}/mix              ducking and gains
    PUT    /narration/{kind}/{key}/game-regions     per-stretch game audio level
    PUT    /narration/{kind}/{key}/camera-regions   per-stretch camera box
    POST   /narration/{kind}/{key}/relock           accept the current video as the takes' video
    POST   /narration/{kind}/{key}/export           background job -> narrated.mp4

HTTP only (DIP): the target comes from the registry, the store and the
exporter come from FastAPI dependencies, so tests and a future per-user
store swap them with `dependency_overrides`. The use-cases live in
`narration.service`.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from ..database import get_db
from ..edits import _run_ffmpeg
from ..jobs import finish_job
from ..models import RankResponse
from ..narration import service, strips, targets
from ..narration.edits import OverlapError, TakeTooShortError
from ..narration.export import export_narration, probe_frame_size
from ..narration.models import (
    KEY_PATTERN,
    AudioMix,
    CameraBox,
    CameraRegion,
    FrameStrip,
    GameAudioRegion,
    Marker,
    NarrationSession,
    Peaks,
    Take,
    TakeEdit,
    TargetRef,
)
from ..narration.service import SessionView
from ..narration.store import NarrationStore, store_for
from ..narration.targets import NarrationTarget

router = APIRouter(tags=["narration"], prefix="/narration")

MAX_UPLOAD_BYTES = 500 * 1024 * 1024
_TAKE_TYPES = frozenset({"video/webm", "audio/webm"})
_JOB_TYPE = "narration_export"

Exporter = Callable[..., Path]
_background_tasks: set[asyncio.Task] = set()


def _track(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# -- dependencies -------------------------------------------------------
def get_ref(kind: str, key: str) -> TargetRef:
    """Validate the path and make sure the key resolves to a video."""
    if not re.fullmatch(KEY_PATTERN, key) or not re.fullmatch(KEY_PATTERN, kind):
        raise HTTPException(422, "kind and key may only contain letters, digits, '_' and '-'")
    try:
        targets.get_target(kind).video_path(key)
    except targets.UnknownTargetError as exc:
        raise HTTPException(404, str(exc)) from exc
    return TargetRef(kind=kind, key=key)


RefDep = Annotated[TargetRef, Depends(get_ref)]


def get_target(ref: RefDep) -> NarrationTarget:
    return targets.get_target(ref.kind)


TargetDep = Annotated[NarrationTarget, Depends(get_target)]


def get_store(target: TargetDep) -> NarrationStore:
    """Swap this dependency for another `NarrationStore` (DB, cloud, per user)."""
    return store_for(target)


StoreDep = Annotated[NarrationStore, Depends(get_store)]


def get_exporter() -> Exporter:
    """Swap this dependency to change how the export runs."""
    return export_narration


ExporterDep = Annotated[Exporter, Depends(get_exporter)]


FramesRenderer = Callable[..., strips.FrameStripFiles]
PeaksRenderer = Callable[..., Peaks]


def get_frames_renderer() -> FramesRenderer:
    """Swap this dependency to avoid running ffmpeg (tests)."""

    def render(video: Path, cache_dir: Path, fp: str) -> strips.FrameStripFiles:
        return strips.ensure_frames(
            video,
            cache_dir,
            fp,
            duration_s=strips.probe_duration(video),
            run=_run_ffmpeg,
            probe_size=probe_frame_size,
        )

    return render


def get_peaks_renderer() -> PeaksRenderer:
    def render(video: Path, cache_dir: Path, fp: str) -> Peaks:
        return strips.ensure_peaks(video, cache_dir, fp, run_capture=strips.run_capture)

    return render


FramesDep = Annotated[FramesRenderer, Depends(get_frames_renderer)]
PeaksDep = Annotated[PeaksRenderer, Depends(get_peaks_renderer)]


def _view(
    ref: TargetRef, target: NarrationTarget, store: NarrationStore, session: NarrationSession
) -> SessionView:
    return service.build_view(ref, target, store, session)


# -- session ------------------------------------------------------------
@router.get("/{kind}/{key}", response_model=SessionView)
async def get_session(
    ref: RefDep,
    target: TargetDep,
    store: StoreDep,
) -> SessionView:
    return _view(ref, target, store, service.load_or_create(ref, target, store))


@router.put("/{kind}/{key}/camera", response_model=SessionView)
async def put_camera(
    body: CameraBox,
    ref: RefDep,
    target: TargetDep,
    store: StoreDep,
) -> SessionView:
    return _view(ref, target, store, service.set_camera(ref, target, store, body))


@router.put("/{kind}/{key}/mix", response_model=SessionView)
async def put_mix(body: AudioMix, ref: RefDep, target: TargetDep, store: StoreDep) -> SessionView:
    return _view(ref, target, store, service.set_mix(ref, target, store, body))


@router.put("/{kind}/{key}/game-regions", response_model=SessionView)
async def put_game_regions(
    body: list[GameAudioRegion], ref: RefDep, target: TargetDep, store: StoreDep
) -> SessionView:
    try:
        session = service.set_game_regions(ref, target, store, body)
    except OverlapError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _view(ref, target, store, session)


@router.put("/{kind}/{key}/camera-regions", response_model=SessionView)
async def put_camera_regions(
    body: list[CameraRegion], ref: RefDep, target: TargetDep, store: StoreDep
) -> SessionView:
    try:
        session = service.set_camera_regions(ref, target, store, body)
    except OverlapError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _view(ref, target, store, session)


class RelockBody(BaseModel):
    shift_s: float = 0.0


@router.post("/{kind}/{key}/relock", response_model=SessionView)
async def relock(
    ref: RefDep,
    target: TargetDep,
    store: StoreDep,
    body: RelockBody | None = None,
) -> SessionView:
    shift = body.shift_s if body else 0.0
    return _view(ref, target, store, service.relock(ref, target, store, shift))


# -- timeline assets ------------------------------------------------------
@router.get("/{kind}/{key}/frames", response_model=FrameStrip)
async def get_frames(
    ref: RefDep, target: TargetDep, store: StoreDep, renderer: FramesDep
) -> FrameStrip:
    video = target.video_path(ref.key)
    fp = targets.fingerprint(video)
    try:
        files = await asyncio.to_thread(renderer, video, store.cache_dir(ref), fp)
    except Exception as exc:
        raise HTTPException(500, f"could not build the frame strip: {exc}"[:300]) from exc
    return FrameStrip(
        sprite_url=f"{service._narration_url(ref, target, store)}/cache/frames-{fp}.jpg",
        every_s=files.every_s,
        tile_w=files.tile_w,
        tile_h=files.tile_h,
        cols=files.cols,
        count=files.count,
    )


@router.get("/{kind}/{key}/peaks", response_model=Peaks)
async def get_peaks(ref: RefDep, target: TargetDep, store: StoreDep, renderer: PeaksDep) -> Peaks:
    video = target.video_path(ref.key)
    fp = targets.fingerprint(video)
    try:
        return await asyncio.to_thread(renderer, video, store.cache_dir(ref), fp)
    except Exception as exc:
        raise HTTPException(500, f"could not compute peaks: {exc}"[:300]) from exc


@router.get("/{kind}/{key}/markers", response_model=list[Marker])
async def get_markers(ref: RefDep, target: TargetDep, store: StoreDep) -> list[Marker]:
    merged = target.markers(ref.key) + service.load_cues(store, ref)
    return sorted(merged, key=lambda m: m.at_s)


# -- takes --------------------------------------------------------------
@router.post("/{kind}/{key}/takes", response_model=Take)
async def upload_take(
    request: Request,
    ref: RefDep,
    target: TargetDep,
    store: StoreDep,
    start_s: Annotated[float, Query(ge=0.0)],
    duration_s: Annotated[float, Query(gt=0.0)],
    has_camera: bool = False,
    replace: bool = True,
) -> Take:
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in _TAKE_TYPES:
        raise HTTPException(415, "take must be sent as video/webm or audio/webm")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "take is too large")
    data = await request.body()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "take is too large")
    return service.add_take(
        ref,
        target,
        store,
        data=data,
        suffix=".webm",
        start_s=start_s,
        duration_s=duration_s,
        has_camera=has_camera,
        replace=replace,
    )


@router.patch("/{kind}/{key}/takes/{take_id}", response_model=SessionView)
async def patch_take(
    take_id: str, body: TakeEdit, ref: RefDep, target: TargetDep, store: StoreDep
) -> SessionView:
    try:
        session = service.edit_take(ref, target, store, take_id, body)
    except service.TakeNotFoundError as exc:
        raise HTTPException(404, f"no take {take_id!r}") from exc
    except (TakeTooShortError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return _view(ref, target, store, session)


@router.post("/{kind}/{key}/takes/{take_id}/split", response_model=SessionView)
async def split_take(
    take_id: str,
    at_s: Annotated[float, Query(gt=0.0)],
    ref: RefDep,
    target: TargetDep,
    store: StoreDep,
) -> SessionView:
    try:
        session = service.split_take(ref, target, store, take_id, at_s)
    except service.TakeNotFoundError as exc:
        raise HTTPException(404, f"no take {take_id!r}") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _view(ref, target, store, session)


@router.delete("/{kind}/{key}/takes/{take_id}", response_model=SessionView)
async def delete_take(
    take_id: str,
    ref: RefDep,
    target: TargetDep,
    store: StoreDep,
) -> SessionView:
    try:
        session = service.remove_take(ref, target, store, take_id)
    except service.TakeNotFoundError as exc:
        raise HTTPException(404, f"no take {take_id!r}") from exc
    return _view(ref, target, store, session)


# -- export -------------------------------------------------------------
class ExportBody(BaseModel):
    force: bool = False


async def _run_export_job(
    job_id: str,
    exporter: Exporter,
    ref: TargetRef,
    target: NarrationTarget,
    store: NarrationStore,
    force: bool,
) -> None:
    db = await get_db()
    try:
        await db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        await db.commit()
        out = await asyncio.to_thread(exporter, ref, target=target, store=store, force=force)
        await finish_job(db, job_id, output_path=str(out), job_type=_JOB_TYPE)
    except Exception as exc:
        with contextlib.suppress(Exception):
            await finish_job(db, job_id, error=str(exc)[:1500], job_type=_JOB_TYPE)
    finally:
        await db.close()


@router.post("/{kind}/{key}/export", response_model=RankResponse)
async def export(
    ref: RefDep,
    target: TargetDep,
    store: StoreDep,
    exporter: ExporterDep,
    body: ExportBody | None = None,
) -> RankResponse:
    """Mix the takes into `narrated.mp4`. Job-based; 409 when stale (unless forced)."""
    force = body.force if body else False
    session = service.load_or_create(ref, target, store)
    if not session.takes:
        raise HTTPException(422, "no takes recorded yet")
    if service.is_stale(ref, target, session) and not force:
        raise HTTPException(
            409, "the video changed since the takes were recorded; relock, delete them or force"
        )
    job_id = str(uuid.uuid4())
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, ?, 'pending', ?)",
            (job_id, _JOB_TYPE, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()
    _track(_run_export_job(job_id, exporter, ref, target, store, force))
    return RankResponse(job_id=job_id)
