"""Narration export orchestration: load → stale check → argv → run →
`narrated.mp4`.

DIP: depends only on the `NarrationTarget` / `NarrationStore` protocols,
which are passed in. Every side effect (fingerprint, frame probe, camera
placement, ffmpeg) is an injectable callable with a real default, so the
flow is unit-tested with fakes and new targets/stores need no edit here.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from ..edits import _run_ffmpeg
from . import targets
from .mix import TakeInput, export_cmd
from .models import CameraBox, NarrationSession, PixelBox, TargetRef
from .store import NarrationStore
from .targets import NarrationTarget

Runner = Callable[[list[str]], tuple[bool, str | None]]
Probe = Callable[[Path], tuple[int, int]]
BoxFor = Callable[[CameraBox, str, int, int], PixelBox]
Fingerprint = Callable[[Path], str]


class StaleNarrationError(RuntimeError):
    """The video changed after the takes were recorded; re-lock or force."""


class NarrationExportError(RuntimeError):
    """ffmpeg failed; the message carries its stderr tail."""


def probe_frame_size(video: Path) -> tuple[int, int]:
    """(width, height) of the first video stream, via ffprobe."""
    r = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=s=x:p=0",
            video.as_posix(),
        ],
        capture_output=True,
        text=True,
    )
    try:
        w, h = (int(v) for v in r.stdout.strip().split("x")[:2])
    except ValueError as exc:
        raise NarrationExportError(f"cannot read frame size of {video}: {r.stderr}") from exc
    return w, h


def layout_box(camera: CameraBox, game: str, frame_w: int, frame_h: int) -> PixelBox:
    """Default camera placement: `layout.camera_box` (imported lazily so this
    module does not depend on the layout implementation at import time)."""
    from .layout import camera_box

    return camera_box(camera, game, frame_w, frame_h)


def export_narration(
    ref: TargetRef,
    *,
    target: NarrationTarget,
    store: NarrationStore,
    force: bool = False,
    run: Runner = _run_ffmpeg,
    fingerprint: Fingerprint | None = None,
    probe: Probe = probe_frame_size,
    box_for: BoxFor = layout_box,
) -> Path:
    """Mix the session's takes over the target video into
    `store.export_path(ref)`. Raises ValueError("no takes"),
    StaleNarrationError (unless `force`), NarrationExportError."""
    session = store.load(ref)
    if session is None or not session.takes:
        raise ValueError("no takes")
    video = target.video_path(ref.key)
    current = (fingerprint or targets.fingerprint)(video)
    if current != session.video_fingerprint and not force:
        raise StaleNarrationError(
            f"{ref.kind}/{ref.key}: the video changed since the takes were recorded"
        )
    takes = _take_inputs(ref, session, store)
    box = _camera_box(session, takes, video, target.game(ref.key), probe, box_for)
    out = store.export_path(ref)
    _render(
        export_cmd(
            video, takes, out, mix=session.mix, camera_box=box, rounded=session.camera.rounded
        ),
        run,
    )
    return out


def _take_inputs(
    ref: TargetRef, session: NarrationSession, store: NarrationStore
) -> list[TakeInput]:
    return [
        TakeInput(
            path=store.take_path(ref, t.media),
            start_s=t.start_s,
            duration_s=t.duration_s,
            has_camera=t.has_camera,
        )
        for t in sorted(session.takes, key=lambda t: t.start_s)
    ]


def _camera_box(
    session: NarrationSession,
    takes: list[TakeInput],
    video: Path,
    game: str,
    probe: Probe,
    box_for: BoxFor,
) -> PixelBox | None:
    """The box in output pixels, or None when no camera is shown."""
    if not session.camera.enabled or not any(t.has_camera for t in takes):
        return None
    w, h = probe(video)
    return box_for(session.camera, game, w, h)


def _render(cmd: list[str], run: Runner) -> None:
    """Run into a temp file and move it into place, so a failed export
    never leaves a half-written `narrated.mp4`."""
    out = Path(cmd[-1])
    part = out.with_name(f"{out.stem}.part{out.suffix}")
    ok, err = run([*cmd[:-1], part.as_posix()])
    if not ok:
        part.unlink(missing_ok=True)
        raise NarrationExportError(f"narration export failed: {err}")
    part.replace(out)
