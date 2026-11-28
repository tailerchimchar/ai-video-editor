"""Session use-cases: what the HTTP API (and any future surface) asks for.

SRP: no HTTP and no file paths here. Everything goes through the
`NarrationTarget` / `NarrationStore` protocols, so a new kind of video or a
new store needs no edit.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel

from ..config import settings
from . import targets
from .edits import apply_edit, check_no_overlap, media_refcount, punch_in, shift_session
from .edits import split_take as split_take_pure
from .layout import camera_box
from .models import (
    AudioMix,
    CameraBox,
    CameraRegion,
    GameAudioRegion,
    Marker,
    NarrationSession,
    PixelBox,
    Take,
    TakeEdit,
    TargetRef,
)
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
    # Shift that would re-align takes after the intro length changed.
    suggested_shift_s: float | None = None
    markers: list[Marker] = []  # chapters/games/inserts + owner cues, by time


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


def load_cues(store: NarrationStore, ref: TargetRef) -> list[Marker]:
    """Owner cue markers from `cues.json`; a missing or invalid file is `[]`."""
    try:
        raw = json.loads(store.cues_path(ref).read_text(encoding="utf-8"))
        return [
            Marker(at_s=float(c["at_s"]), label=str(c.get("label", ""))[:80], kind="cue")
            for c in raw
        ]
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return []


def _export_url(ref: TargetRef, target: NarrationTarget, store: NarrationStore) -> str | None:
    """URL of `narrated.mp4` if it exists."""
    if not store.export_path(ref).is_file():
        return None
    return f"{_narration_url(ref, target, store)}/narrated.mp4"


def _suggested_shift(
    ref: TargetRef, target: NarrationTarget, session: NarrationSession
) -> float | None:
    current = target.intro_s(ref.key)
    pinned = session.pinned_intro_s
    if pinned is None or current is None or abs(current - pinned) <= 0.01:
        return None
    return round(current - pinned, 3)


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
        suggested_shift_s=_suggested_shift(ref, target, session),
        markers=sorted(target.markers(ref.key) + load_cues(store, ref), key=lambda m: m.at_s),
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
    replace: bool = True,
) -> Take:
    session = load_or_create(ref, target, store)
    if not session.takes:  # the first take pins the video as it is now
        session.video_fingerprint = _current_fingerprint(ref, target)
        session.pinned_intro_s = target.intro_s(ref.key)
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
    before = session.takes
    if replace:
        session.takes = punch_in(before, take, new_id=lambda: uuid.uuid4().hex[:12])
    else:
        session.takes = [*before, take]
    store.save(session)
    _delete_orphans(ref, store, before, session.takes)
    return take


def _delete_orphans(
    ref: TargetRef, store: NarrationStore, before: list[Take], after: list[Take]
) -> None:
    """Delete media files that no remaining take plays from."""
    for media in {t.media for t in before}:
        if media_refcount(after, media) == 0:
            store.delete_take(ref, media)


def remove_take(
    ref: TargetRef, target: NarrationTarget, store: NarrationStore, take_id: str
) -> NarrationSession:
    session = load_or_create(ref, target, store)
    take = next((t for t in session.takes if t.id == take_id), None)
    if take is None:
        raise TakeNotFoundError(take_id)
    before = session.takes
    session.takes = [t for t in before if t.id != take_id]
    store.save(session)  # forget the take first; a leftover file is harmless
    _delete_orphans(ref, store, before, session.takes)
    return session


def set_camera(
    ref: TargetRef, target: NarrationTarget, store: NarrationStore, camera: CameraBox
) -> NarrationSession:
    session = load_or_create(ref, target, store)
    session.camera = camera
    store.save(session)
    return session


def _find(session: NarrationSession, take_id: str) -> int:
    for i, t in enumerate(session.takes):
        if t.id == take_id:
            return i
    raise TakeNotFoundError(take_id)


def edit_take(
    ref: TargetRef,
    target: NarrationTarget,
    store: NarrationStore,
    take_id: str,
    edit: TakeEdit,
) -> NarrationSession:
    session = load_or_create(ref, target, store)
    idx = _find(session, take_id)
    edited = apply_edit(session.takes[idx], edit)
    if edited.start_effective_s < 0:
        raise ValueError("offset would start the take before 0:00")
    session.takes[idx] = edited
    store.save(session)
    return session


def split_take(
    ref: TargetRef, target: NarrationTarget, store: NarrationStore, take_id: str, at_s: float
) -> NarrationSession:
    session = load_or_create(ref, target, store)
    idx = _find(session, take_id)
    first, second = split_take_pure(session.takes[idx], at_s, new_id=uuid.uuid4().hex[:12])
    session.takes[idx : idx + 1] = [first, second]
    store.save(session)
    return session


def set_mix(
    ref: TargetRef, target: NarrationTarget, store: NarrationStore, mix: AudioMix
) -> NarrationSession:
    session = load_or_create(ref, target, store)
    session.mix = mix
    store.save(session)
    return session


def set_game_regions(
    ref: TargetRef,
    target: NarrationTarget,
    store: NarrationStore,
    regions: list[GameAudioRegion],
) -> NarrationSession:
    check_no_overlap(regions)
    session = load_or_create(ref, target, store)
    session.game_regions = sorted(regions, key=lambda r: r.start_s)
    store.save(session)
    return session


def set_camera_regions(
    ref: TargetRef,
    target: NarrationTarget,
    store: NarrationStore,
    regions: list[CameraRegion],
) -> NarrationSession:
    check_no_overlap(regions)
    session = load_or_create(ref, target, store)
    session.camera_regions = sorted(regions, key=lambda r: r.start_s)
    store.save(session)
    return session


def relock(
    ref: TargetRef, target: NarrationTarget, store: NarrationStore, shift_s: float = 0.0
) -> NarrationSession:
    """Keep the takes (moved by `shift_s`) and pin them to the video as it is now."""
    session = load_or_create(ref, target, store)
    if shift_s != 0:
        session = shift_session(session, shift_s)
    session.video_fingerprint = _current_fingerprint(ref, target)
    session.pinned_intro_s = target.intro_s(ref.key)
    store.save(session)
    return session
