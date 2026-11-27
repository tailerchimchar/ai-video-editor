"""Session use-cases: what the HTTP API (and any future surface) asks for.

SRP: no HTTP and no file paths here. Everything goes through the
`NarrationTarget` / `NarrationStore` protocols, so a new kind of video or a
new store needs no edit.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel

from ..config import settings
from . import targets
from .layout import camera_box
from .models import CameraBox, NarrationSession, PixelBox, Take, TargetRef
from .store import NarrationStore
from .targets import NarrationTarget

# Frame the browser lays the camera box out against; the page scales it.
REFERENCE_FRAME = (1920, 1080)


class TakeNotFoundError(LookupError):
    """No take with that id in the session."""


class SessionView(BaseModel):
    """The session plus what the page needs to draw it."""

    session: NarrationSession
    video_url: str
    game: str
    stale: bool
    camera_px: PixelBox  # the owner's box resolved for a 1920x1080 frame
    default_camera_px: PixelBox  # the game profile's box, ignoring overrides
    export_url: str | None = None  # narrated.mp4, once an export exists
    # The narration folder (takes/, narrated.mp4) under /workspace. Not always
    # next to the video: joined videos keep theirs in story/_joined/<key>/.
    narration_url: str | None = None


def _current_fingerprint(ref: TargetRef, target: NarrationTarget) -> str:
    return targets.fingerprint(target.video_path(ref.key))


def load_or_create(
    ref: TargetRef, target: NarrationTarget, store: NarrationStore
) -> NarrationSession:
    """The saved session; the first call saves an empty one pinned to the
    video as it is now."""
    session = store.load(ref)
    if session is None:
        session = NarrationSession(
            target=ref,
            game=target.game(ref.key),
            video_fingerprint=_current_fingerprint(ref, target),
        )
        store.save(session)
    return session


def is_stale(ref: TargetRef, target: NarrationTarget, session: NarrationSession) -> bool:
    """Takes were recorded over an older version of the video. A session
    with no takes is never stale (nothing can be out of line)."""
    return bool(session.takes) and session.video_fingerprint != _current_fingerprint(ref, target)


def _narration_url(ref: TargetRef, target: NarrationTarget, store: NarrationStore) -> str:
    """URL of the narration folder: from the store's folder when it is under
    the workspace, else the `narration/` folder next to the video."""
    folder = store.export_path(ref).parent
    try:
        rel = folder.resolve().relative_to(settings.workspace_dir.resolve())
    except ValueError:
        return target.video_url(ref.key).rsplit("/", 1)[0] + "/narration"
    return "/workspace/" + rel.as_posix()


def _export_url(ref: TargetRef, target: NarrationTarget, store: NarrationStore) -> str | None:
    """URL of `narrated.mp4` if it exists."""
    if not store.export_path(ref).is_file():
        return None
    return f"{_narration_url(ref, target, store)}/narrated.mp4"


def build_view(
    ref: TargetRef, target: NarrationTarget, store: NarrationStore, session: NarrationSession
) -> SessionView:
    w, h = REFERENCE_FRAME
    game = target.game(ref.key)
    return SessionView(
        session=session,
        video_url=target.video_url(ref.key),
        game=game,
        stale=is_stale(ref, target, session),
        camera_px=camera_box(session.camera, game, w, h),
        default_camera_px=camera_box(CameraBox(), game, w, h),
        export_url=_export_url(ref, target, store),
        narration_url=_narration_url(ref, target, store),
    )


def add_take(
    ref: TargetRef,
    target: NarrationTarget,
    store: NarrationStore,
    *,
    data: bytes,
    suffix: str,
    start_s: float,
    duration_s: float,
    has_camera: bool,
) -> Take:
    session = load_or_create(ref, target, store)
    if not session.takes:  # the first take pins the video as it is now
        session.video_fingerprint = _current_fingerprint(ref, target)
    take_id = uuid.uuid4().hex[:12]
    media = store.write_take(ref, take_id, data, suffix)
    take = Take(
        id=take_id,
        start_s=start_s,
        duration_s=duration_s,
        media=media,
        has_camera=has_camera,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    session.takes.append(take)
    store.save(session)
    return take


def remove_take(
    ref: TargetRef, target: NarrationTarget, store: NarrationStore, take_id: str
) -> NarrationSession:
    session = load_or_create(ref, target, store)
    take = next((t for t in session.takes if t.id == take_id), None)
    if take is None:
        raise TakeNotFoundError(take_id)
    session.takes.remove(take)
    store.save(session)  # forget the take first; a leftover file is harmless
    store.delete_take(ref, take.media)
    return session


def set_camera(
    ref: TargetRef, target: NarrationTarget, store: NarrationStore, camera: CameraBox
) -> NarrationSession:
    session = load_or_create(ref, target, store)
    session.camera = camera
    store.save(session)
    return session


def relock(ref: TargetRef, target: NarrationTarget, store: NarrationStore) -> NarrationSession:
    """Keep the takes as they are and pin them to the video as it is now."""
    session = load_or_create(ref, target, store)
    session.video_fingerprint = _current_fingerprint(ref, target)
    store.save(session)
    return session
