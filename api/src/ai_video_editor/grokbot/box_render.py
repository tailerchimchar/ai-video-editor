"""Box render job: render edit plans from Drive clips on a Linux box.

This module runs on a headless Linux box (8 CPU cores, no GPU, limited
RAM). It downloads clips from Google Drive, renders both 9:16 and 16:9
outputs from the same edit plan, uploads results back to Drive, and
emits a video.rendered event.

Design constraints:
- One render at a time (file lock prevents concurrent jobs)
- Fail fast: download/upload failures emit failure events, not partial
  success
- Scratch cleanup only after ALL outputs upload successfully (HIGH RISK)
- CPU-only ffmpeg settings (libx264 veryfast, capped resolution)
- No LLM or vision calls: pure ffmpeg render, deterministic

Events emitted:
- video.rendered: success, includes both aspect ratio outputs
- render.failed: any failure (busy, download, render, upload)
"""

from __future__ import annotations

import fcntl
import json
import logging
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import settings
from ..edits import cropped_hud_9x16
from ..notify import Event, emit
from ..storage import DriveStorage, StorageError

if TYPE_CHECKING:
    from ..grokbot.drive import Uploader

_log = logging.getLogger(__name__)

LOCK_FILE = "/tmp/ai-video-editor-box-render.lock"
SCRATCH_PREFIX = "box-render-"


@dataclass
class RenderInput:
    """Input specification for a box render job."""

    edit_plan_id: str
    edit_plan_version: str
    asset_id: str
    clips: list[dict]
    trims: list[dict]
    concat_order: list[str]
    game: str | None = None
    music_path: str | None = None
    music_volume: float = 0.25
    captions_enabled: bool = True


@dataclass
class RenderOutput:
    """Output of a successful render."""

    aspect: str
    local_path: Path
    drive_id: str | None = None
    duration_seconds: float = 0.0
    file_size_bytes: int = 0


@dataclass
class RenderResult:
    """Result of a box render job."""

    ok: bool
    edit_plan_id: str
    edit_plan_version: str
    asset_id: str
    outputs: list[RenderOutput] = field(default_factory=list)
    error: str | None = None
    scratch_cleaned: bool = False
    timings: dict = field(default_factory=dict)


class RenderLock:
    """Simple file lock for single-render concurrency."""

    def __init__(self, lock_path: str = LOCK_FILE):
        self._lock_path = lock_path
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Try to acquire the lock. Returns True if acquired."""
        try:
            self._fd = open(self._lock_path, "w")  # noqa: SIM115
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd.write(f"{datetime.now(timezone.utc).isoformat()}\n")
            self._fd.flush()
            return True
        except (BlockingIOError, OSError):
            if self._fd:
                self._fd.close()
                self._fd = None
            return False

    def release(self) -> None:
        """Release the lock."""
        if self._fd:
            try:
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
                self._fd.close()
            except OSError:
                pass
            self._fd = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("render job already running")
        return self

    def __exit__(self, *_):
        self.release()


def _probe_duration(path: Path) -> float:
    """Get video duration via ffprobe."""
    cmd = [
        settings.ffmpeg_path.replace("ffmpeg", "ffprobe"),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return float(data.get("format", {}).get("duration", 0))
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError, KeyError):
        pass
    return 0.0


def _run_ffmpeg(cmd: list[str], timeout: float = 600.0) -> tuple[bool, str | None]:
    """Run ffmpeg command with timeout. Returns (ok, error)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return False, result.stderr[-1500:]
        return True, None
    except subprocess.TimeoutExpired:
        return False, f"ffmpeg timed out after {timeout}s"
    except FileNotFoundError:
        return False, "ffmpeg not found"


def _render_single_aspect(
    scratch_dir: Path,
    input_clips: list[Path],
    trims: list[dict],
    concat_order: list[str],
    aspect: str,
    *,
    game: str | None = None,
    music_path: str | None = None,
    music_volume: float = 0.25,
) -> tuple[Path | None, str | None]:
    """Render a single aspect ratio output. Returns (output_path, error).

    For 9:16, uses the existing cropped_hud_9x16 layout which places
    the minimap and score appropriately for the game. For 16:9, uses
    a simple scale to cap resolution (no crop).

    No LLM or vision calls: pure ffmpeg, deterministic.
    """
    parts_dir = scratch_dir / f"parts_{aspect.replace(':', 'x')}"
    parts_dir.mkdir(parents=True, exist_ok=True)

    clip_id_to_path = {t["clip_id"]: p for t, p in zip(trims, input_clips, strict=False)}
    parts: list[Path] = []

    for trim in trims:
        clip_id = trim["clip_id"]
        src = clip_id_to_path.get(clip_id)
        if not src or not src.exists():
            return None, f"missing clip: {clip_id}"

        part_path = parts_dir / f"part_{clip_id[:8]}.mp4"
        start_s = float(trim.get("start_s", 0))
        end_s = float(trim.get("end_s", 0))
        duration = max(0.0, end_s - start_s)

        codec_opts = _cpu_codec_opts()

        if aspect == "9:16":
            filter_complex = cropped_hud_9x16(game=game)
            cmd = [
                settings.ffmpeg_path,
                "-y",
                "-fflags",
                "+discardcorrupt",
                "-err_detect",
                "ignore_err",
                "-ss",
                str(start_s),
                "-i",
                str(src),
                "-t",
                str(duration),
                "-filter_complex",
                filter_complex,
                "-map",
                "[out]",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                *codec_opts,
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(part_path),
            ]
        else:
            vf = _16x9_filter()
            cmd = [
                settings.ffmpeg_path,
                "-y",
                "-fflags",
                "+discardcorrupt",
                "-err_detect",
                "ignore_err",
                "-ss",
                str(start_s),
                "-i",
                str(src),
                "-t",
                str(duration),
                "-vf",
                vf,
                "-c:v",
                "libx264",
                *codec_opts,
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(part_path),
            ]

        ok, err = _run_ffmpeg(cmd)
        if not ok:
            return None, f"clip render failed ({clip_id}): {err}"
        parts.append(part_path)

    if not parts:
        return None, "no parts rendered"

    ordered_parts = []
    for clip_id in concat_order:
        for trim, part in zip(trims, parts, strict=False):
            if trim["clip_id"] == clip_id:
                ordered_parts.append(part)
                break

    if not ordered_parts:
        ordered_parts = parts

    concat_list = scratch_dir / f"concat_{aspect.replace(':', 'x')}.txt"
    with concat_list.open("w", encoding="utf-8") as f:
        for p in ordered_parts:
            f.write(f"file '{p.as_posix()}'\n")

    aspect_slug = aspect.replace(":", "x")
    if music_path:
        concat_tmp = scratch_dir / f"concat_tmp_{aspect_slug}.mp4"
        final_path = scratch_dir / f"output_{aspect_slug}.mp4"
    else:
        final_path = scratch_dir / f"output_{aspect_slug}.mp4"
        concat_tmp = final_path

    cmd = [
        settings.ffmpeg_path,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list),
        "-c",
        "copy",
        str(concat_tmp),
    ]
    ok, err = _run_ffmpeg(cmd)
    if not ok:
        return None, f"concat failed: {err}"

    if music_path and Path(music_path).exists():
        cmd = [
            settings.ffmpeg_path,
            "-y",
            "-i",
            str(concat_tmp),
            "-i",
            music_path,
            "-filter_complex",
            f"[1:a]volume={music_volume:.2f}[m];"
            f"[0:a][m]amix=inputs=2:duration=first:dropout_transition=0[aout]",
            "-map",
            "0:v",
            "-map",
            "[aout]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(final_path),
        ]
        ok, err = _run_ffmpeg(cmd)
        if not ok:
            return None, f"music mix failed: {err}"
        concat_tmp.unlink(missing_ok=True)

    return final_path, None


def _16x9_filter() -> str:
    """Scale filter for 16:9 output: cap resolution, preserve aspect."""
    return (
        "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(1920-iw)/2:(1080-ih)/2:black,"
        "setsar=1"
    )


def _cpu_codec_opts() -> list[str]:
    """CPU-friendly codec options for box render (no GPU)."""
    return ["-preset", "veryfast", "-crf", "23"]


def run_box_render(
    uploader: Uploader,
    render_input: RenderInput,
    *,
    emit_fn=emit,
    lock: RenderLock | None = None,
) -> RenderResult:
    """Run a box render job end-to-end.

    1. Acquire lock (fail fast if busy)
    2. Create scratch directory
    3. Download clips from Drive
    4. Render both 9:16 and 16:9 outputs
    5. Upload outputs to Drive
    6. Emit video.rendered event
    7. Clean up scratch (HIGH RISK: only after all uploads succeed)

    Returns RenderResult with success/failure details.
    """
    start_time = time.monotonic()
    result = RenderResult(
        ok=False,
        edit_plan_id=render_input.edit_plan_id,
        edit_plan_version=render_input.edit_plan_version,
        asset_id=render_input.asset_id,
    )

    _lock = lock or RenderLock()
    if not _lock.acquire():
        result.error = "render job already running (locked)"
        _emit_failure(emit_fn, result, "busy")
        return result

    scratch_dir: Path | None = None
    try:
        scratch_dir = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX))
        _log.info("box render started: plan=%s scratch=%s", render_input.edit_plan_id, scratch_dir)

        storage = DriveStorage(uploader, scratch_dir)
        download_start = time.monotonic()
        local_clips = []

        for i, clip in enumerate(render_input.clips):
            drive_id = clip.get("drive_id")
            if not drive_id:
                result.error = f"clip {i} missing drive_id"
                _emit_failure(emit_fn, result, "download")
                return result
            try:
                local_path = storage.download_clip(drive_id, f"clip_{i:03d}.mp4")
                local_clips.append(local_path)
            except StorageError as e:
                result.error = f"download failed: clip {i}: {e}"
                _emit_failure(emit_fn, result, "download")
                return result

        result.timings["download_seconds"] = round(time.monotonic() - download_start, 2)
        dl_secs = result.timings["download_seconds"]
        _log.info("downloaded %d clips in %.1fs", len(local_clips), dl_secs)

        render_start = time.monotonic()
        for aspect in ["9:16", "16:9"]:
            output_path, err = _render_single_aspect(
                scratch_dir,
                local_clips,
                render_input.trims,
                render_input.concat_order,
                aspect,
                game=render_input.game,
                music_path=render_input.music_path,
                music_volume=render_input.music_volume,
            )
            if err or not output_path or not output_path.exists():
                result.error = f"render failed ({aspect}): {err}"
                _emit_failure(emit_fn, result, "render")
                return result

            duration = _probe_duration(output_path)
            file_size = output_path.stat().st_size
            result.outputs.append(
                RenderOutput(
                    aspect=aspect,
                    local_path=output_path,
                    duration_seconds=duration,
                    file_size_bytes=file_size,
                )
            )

        result.timings["render_seconds"] = round(time.monotonic() - render_start, 2)
        rnd_secs = result.timings["render_seconds"]
        _log.info("rendered %d outputs in %.1fs", len(result.outputs), rnd_secs)

        upload_start = time.monotonic()
        for output in result.outputs:
            aspect_slug = output.aspect.replace(":", "x")
            name = f"{render_input.edit_plan_id}_{aspect_slug}.mp4"
            try:
                drive_id = storage.upload_file(
                    output.local_path, name, settings.grokbot_box_finished_folder
                )
                output.drive_id = drive_id
            except StorageError as e:
                result.error = f"upload failed ({output.aspect}): {e}"
                _emit_failure(emit_fn, result, "upload")
                return result

        result.timings["upload_seconds"] = round(time.monotonic() - upload_start, 2)
        up_secs = result.timings["upload_seconds"]
        _log.info("uploaded %d outputs in %.1fs", len(result.outputs), up_secs)

        result.ok = True
        result.timings["total_seconds"] = round(time.monotonic() - start_time, 2)

        _emit_success(emit_fn, result)

        # HIGH RISK: Scratch cleanup only after ALL uploads succeeded
        # This is a deliberate design choice approved by the owner.
        # Scope: only the job's own scratch_dir, never anything else.
        if scratch_dir and scratch_dir.exists():
            if str(scratch_dir).startswith(tempfile.gettempdir()):
                shutil.rmtree(scratch_dir, ignore_errors=True)
                result.scratch_cleaned = True
                _log.info("cleaned scratch dir: %s", scratch_dir)
            else:
                _log.warning("scratch dir outside temp, not cleaning: %s", scratch_dir)

        return result

    except Exception as e:
        result.error = f"unexpected error: {e}"
        _log.exception("box render failed: %s", e)
        _emit_failure(emit_fn, result, "internal")
        return result

    finally:
        _lock.release()


def _emit_success(emit_fn, result: RenderResult) -> None:
    """Emit video.rendered event on success."""
    outputs_payload = []
    for out in result.outputs:
        outputs_payload.append(
            {
                "aspect": out.aspect,
                "drive_id": out.drive_id,
                "duration_seconds": out.duration_seconds,
                "file_size_bytes": out.file_size_bytes,
            }
        )

    emit_fn(
        Event(
            event="video.rendered",
            payload={
                "edit_plan_id": result.edit_plan_id,
                "edit_plan_version": result.edit_plan_version,
                "asset_id": result.asset_id,
                "outputs": outputs_payload,
                "timings": result.timings,
            },
        )
    )


def _emit_failure(emit_fn, result: RenderResult, stage: str) -> None:
    """Emit render.failed event on failure."""
    emit_fn(
        Event(
            event="render.failed",
            payload={
                "edit_plan_id": result.edit_plan_id,
                "edit_plan_version": result.edit_plan_version,
                "asset_id": result.asset_id,
                "stage": stage,
                "error": result.error,
            },
        )
    )
