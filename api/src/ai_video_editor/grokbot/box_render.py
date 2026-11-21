"""Box render job: render edit plans from Drive clips on a Linux box.

This module runs on a headless Linux box (8 CPU cores, no GPU, limited
RAM). It downloads clips from Google Drive, renders both 9:16 and 16:9
outputs from the same edit plan, uploads results back to Drive, and
emits a video.rendered event.

Design constraints:
- One render at a time (file lock prevents concurrent jobs; fcntl/Linux)
- Fail fast: download/upload failures emit failure events, not partial
  success
- Scratch cleanup only after ALL outputs upload successfully (HIGH RISK)
- CPU-only ffmpeg settings (libx264 veryfast, capped resolution)
- Quality gate (optional, off by default): VLM vision + Jev scoring
  after render; never blocks delivery on errors/timeouts

Events emitted:
- video.rendered: success, includes both aspect ratio outputs + gate results
- render.failed: any failure (busy, download, render, upload)
- quality.revision_needed: when the gate requests a revision (max 2/criterion)

Entry point: ``box-render`` CLI (``main``) consumes a render_request JSON
file written by the Editor into the queue repo. The PC API does not
accept render submissions.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import settings
from ..edits import cropped_hud_9x16
from ..notify import Event, emit
from ..storage import BoxRenderStorage, DriveStorage, StorageError

if TYPE_CHECKING:
    from ..grokbot.drive import Uploader
    from ..grokbot.queue import RenderRequestItem

_log = logging.getLogger(__name__)

SCRATCH_PREFIX = "box-render-"
_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\"'\r\n]+|/[^\"'\r\n]+")
_DEFAULT_LOCK = "/tmp/ai-video-editor-box-render.lock"


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
    # Resolved from a library song_id only — never a raw queue path.
    music_path: str | None = None
    music_volume: float = 0.25
    captions_enabled: bool = True
    quality_criteria: list[dict] = field(default_factory=list)
    digest_moment_labels: list[str] = field(default_factory=list)
    captions: list[str] = field(default_factory=list)


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
    quality_gate: dict = field(default_factory=dict)


def _safe_error(detail: str | None, limit: int = 500) -> str | None:
    """Strip absolute paths from error text before it reaches events."""
    if detail is None:
        return None
    return _PATH_RE.sub("<path>", str(detail))[:limit]


def _lock_path() -> str:
    return settings.grokbot_box_lock_file or _DEFAULT_LOCK


def _render_timeout() -> float:
    return float(settings.grokbot_box_render_timeout or 600.0)


def _download_timeout() -> float:
    return float(settings.grokbot_box_download_timeout or 300.0)


class RenderLock:
    """Simple file lock for single-render concurrency (fcntl; Unix only)."""

    def __init__(self, lock_path: str | None = None):
        self._lock_path = lock_path or _lock_path()
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Try to acquire the lock. Returns True if acquired."""
        try:
            import fcntl
        except ImportError as e:
            raise RuntimeError("RenderLock requires fcntl (Linux/macOS)") from e
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
                import fcntl

                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
                self._fd.close()
            except (ImportError, OSError):
                pass
            self._fd = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("render job already running")
        return self

    def __exit__(self, *_):
        self.release()


def _ffprobe_bin() -> str:
    """Derive ffprobe from the ffmpeg binary name without rewriting directories."""
    p = Path(settings.ffmpeg_path)
    name = p.name
    if "ffmpeg" in name:
        return str(p.with_name(name.replace("ffmpeg", "ffprobe", 1)))
    return str(p.with_name("ffprobe"))


def _probe_duration(path: Path) -> float:
    """Get video duration via ffprobe."""
    cmd = [
        _ffprobe_bin(),
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


def _run_ffmpeg(cmd: list[str], timeout: float | None = None) -> tuple[bool, str | None]:
    """Run ffmpeg command with timeout. Returns (ok, error)."""
    limit = _render_timeout() if timeout is None else timeout
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=limit)
        if result.returncode != 0:
            return False, _safe_error(result.stderr[-1500:] if result.stderr else "ffmpeg failed")
        return True, None
    except subprocess.TimeoutExpired:
        return False, f"ffmpeg timed out after {limit}s"
    except FileNotFoundError:
        return False, "ffmpeg not found"


def _render_single_aspect(
    scratch_dir: Path,
    clip_paths: dict[str, Path],
    trims: list[dict],
    concat_order: list[str],
    aspect: str,
    *,
    game: str | None = None,
    music_path: str | None = None,
    music_volume: float = 0.25,
) -> tuple[Path | None, str | None]:
    """Render a single aspect ratio output. Returns (output_path, error).

    Clips are keyed by clip_ref. Part files are named by trim index so
    shared id prefixes cannot overwrite each other.
    """
    parts_dir = scratch_dir / f"parts_{aspect.replace(':', 'x')}"
    parts_dir.mkdir(parents=True, exist_ok=True)

    parts: list[Path] = []
    trim_to_part: dict[str, Path] = {}

    for i, trim in enumerate(trims):
        clip_id = trim["clip_id"]
        src = clip_paths.get(clip_id)
        if not src or not src.exists():
            return None, f"missing clip: {clip_id}"

        part_path = parts_dir / f"part_{i:03d}.mp4"
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
        trim_to_part[clip_id] = part_path

    if not parts:
        return None, "no parts rendered"

    ordered_parts = []
    for clip_id in concat_order:
        part = trim_to_part.get(clip_id)
        if part is not None:
            ordered_parts.append(part)

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


def render_input_from_request(item: RenderRequestItem) -> RenderInput:
    """Build a RenderInput from a validated RenderRequestItem.

    Music is not resolved here: a future library lookup may set
    ``music_path`` from ``music_song_id``. Raw filesystem paths from the
    queue are never accepted.
    """
    clips = [
        {
            "clip_ref": c.clip_ref,
            "drive_id": c.drive_id,
            "start_s": c.start_s,
            "end_s": c.end_s,
        }
        for c in item.clips
    ]
    trims = [{"clip_id": c.clip_ref, "start_s": c.start_s, "end_s": c.end_s} for c in item.clips]
    return RenderInput(
        edit_plan_id=item.edit_plan_id,
        edit_plan_version=item.edit_plan_version,
        asset_id=item.asset_id,
        clips=clips,
        trims=trims,
        concat_order=list(item.concat_order),
        game=item.game,
        music_path=None,
    )


def _scratch_parent() -> Path:
    """Directory that owns job scratch dirs (configured or system temp)."""
    configured = settings.grokbot_box_scratch_dir
    if configured is not None:
        parent = Path(configured)
        parent.mkdir(parents=True, exist_ok=True)
        return parent.resolve()
    return Path(tempfile.gettempdir()).resolve()


def _is_safe_scratch(scratch_dir: Path) -> bool:
    """True only for a non-symlink job dir we created under an allowed parent."""
    try:
        resolved = scratch_dir.resolve()
    except OSError:
        return False
    if scratch_dir.is_symlink() or resolved.is_symlink():
        return False
    if not resolved.name.startswith(SCRATCH_PREFIX):
        return False
    allowed = {Path(tempfile.gettempdir()).resolve()}
    configured = settings.grokbot_box_scratch_dir
    if configured is not None:
        allowed.add(Path(configured).resolve())
    return resolved.parent in allowed


def _make_scratch_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX, dir=str(_scratch_parent())))


def _cleanup_scratch(scratch_dir: Path, *, created: bool) -> bool:
    """Delete scratch only if this job created it and the guard passes."""
    if not created or scratch_dir is None or not scratch_dir.exists():
        return False
    if not _is_safe_scratch(scratch_dir):
        _log.warning("scratch dir failed cleanup guard, not cleaning: %s", scratch_dir)
        return False
    try:
        shutil.rmtree(scratch_dir)
        _log.info("cleaned scratch dir: %s", scratch_dir)
        return True
    except OSError as e:
        _log.warning("scratch cleanup failed: %s", e)
        return False


def run_box_render(
    uploader: Uploader,
    render_input: RenderInput,
    *,
    emit_fn=emit,
    lock: RenderLock | None = None,
    storage: BoxRenderStorage | None = None,
) -> RenderResult:
    """Run a box render job end-to-end.

    1. Acquire lock (fail fast if busy)
    2. Create scratch directory (unless storage is injected)
    3. Download clips from Drive keyed by clip_ref
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

    if not settings.grokbot_box_render_enabled:
        result.error = "box render disabled"
        _emit_failure(emit_fn, result, "disabled")
        return result

    _lock = lock or RenderLock()
    if not _lock.acquire():
        result.error = "render job already running (locked)"
        _emit_failure(emit_fn, result, "busy")
        return result

    scratch_dir: Path | None = None
    created_scratch = False
    _storage: BoxRenderStorage
    try:
        if storage is not None:
            _storage = storage
            scratch_dir = _storage.scratch_dir
        else:
            scratch_dir = _make_scratch_dir()
            created_scratch = True
            _storage = DriveStorage(uploader, scratch_dir, download_timeout=_download_timeout())
        _log.info(
            "box render started: plan=%s scratch=%s",
            render_input.edit_plan_id,
            scratch_dir,
        )
        download_start = time.monotonic()
        clip_paths: dict[str, Path] = {}

        for i, clip in enumerate(render_input.clips):
            drive_id = clip.get("drive_id")
            clip_ref = clip.get("clip_ref") or str(i + 1)
            if not drive_id:
                result.error = f"clip {i} missing drive_id"
                _emit_failure(emit_fn, result, "download")
                return result
            try:
                local_path = _storage.download_clip(drive_id, f"clip_{i:03d}.mp4")
                clip_paths[clip_ref] = local_path
            except StorageError as e:
                result.error = _safe_error(f"download failed: clip {i}: {e}")
                _emit_failure(emit_fn, result, "download")
                return result

        result.timings["download_seconds"] = round(time.monotonic() - download_start, 2)
        dl_secs = result.timings["download_seconds"]
        _log.info("downloaded %d clips in %.1fs", len(clip_paths), dl_secs)

        render_start = time.monotonic()
        for aspect in ["9:16", "16:9"]:
            output_path, err = _render_single_aspect(
                scratch_dir,
                clip_paths,
                render_input.trims,
                render_input.concat_order,
                aspect,
                game=render_input.game,
                music_path=render_input.music_path,
                music_volume=render_input.music_volume,
            )
            if err or not output_path or not output_path.exists():
                result.error = _safe_error(f"render failed ({aspect}): {err}")
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
                drive_id = _storage.upload_file(
                    output.local_path, name, settings.grokbot_box_finished_folder
                )
                output.drive_id = drive_id
            except StorageError as e:
                result.error = _safe_error(f"upload failed ({output.aspect}): {e}")
                _emit_failure(emit_fn, result, "upload")
                return result

        result.timings["upload_seconds"] = round(time.monotonic() - upload_start, 2)
        up_secs = result.timings["upload_seconds"]
        _log.info("uploaded %d outputs in %.1fs", len(result.outputs), up_secs)

        # Quality gate: optional post-render check (None when flag off / no criteria)
        gate_result, revision_requests = _run_quality_gate(render_input, result, scratch_dir)
        result.quality_gate = gate_result.model_dump() if gate_result is not None else {}

        result.ok = True
        result.timings["total_seconds"] = round(time.monotonic() - start_time, 2)

        # Emit revision requests if the gate failed
        for req in revision_requests:
            _emit_revision_request(emit_fn, result, req)

        _emit_success(emit_fn, result, gate_result)

        # HIGH RISK: Scratch cleanup only after ALL uploads succeeded.
        result.scratch_cleaned = _cleanup_scratch(scratch_dir, created=created_scratch)

        return result

    except Exception as e:
        result.error = _safe_error(f"unexpected error: {type(e).__name__}")
        _log.exception("box render failed: %s", e)
        _emit_failure(emit_fn, result, "internal")
        return result

    finally:
        _lock.release()


def run_render_request(
    item: RenderRequestItem,
    uploader: Uploader,
    *,
    emit_fn=emit,
    lock: RenderLock | None = None,
    storage: BoxRenderStorage | None = None,
) -> RenderResult:
    """Queue-consumer entry: convert a RenderRequestItem and run the job."""
    return run_box_render(
        uploader,
        render_input_from_request(item),
        emit_fn=emit_fn,
        lock=lock,
        storage=storage,
    )


def _output_clip_windows(trims: list[dict], concat_order: list[str]) -> list[dict]:
    """Build output-timeline windows (back-to-back concat) from source trims."""
    by_id = {t.get("clip_id"): t for t in trims}
    ordered = [by_id[cid] for cid in concat_order if cid in by_id] or list(trims)
    windows: list[dict] = []
    cursor = 0.0
    for trim in ordered:
        start_s = float(trim.get("start_s", 0))
        end_s = float(trim.get("end_s", 0))
        duration = max(0.0, end_s - start_s)
        window = {
            "clip_id": trim.get("clip_id"),
            "start_s": start_s,
            "end_s": end_s,
            "output_start_s": cursor,
            "output_end_s": cursor + duration,
        }
        if "has_hook" in trim:
            window["has_hook"] = trim["has_hook"]
        windows.append(window)
        cursor += duration
    return windows


def _run_quality_gate(
    render_input: RenderInput,
    result: RenderResult,
    scratch_dir: Path,
):
    """Run the quality gate if enabled and criteria exist.

    Returns ``(None, [])`` when the flag is off or there are no criteria so
    ``video.rendered`` stays byte-identical. Otherwise returns
    ``(GateResult | None-on-hard-skip, revisions)``.
    """
    from .quality_gate import (
        GateResult,
        RenderMetadata,
        load_retry_state,
        run_quality_gate,
        save_retry_state,
    )

    if not settings.grokbot_quality_gate_enabled:
        return None, []
    if not render_input.quality_criteria:
        return None, []

    primary_output = next(
        (o for o in result.outputs if o.aspect == "9:16"),
        result.outputs[0] if result.outputs else None,
    )
    if not primary_output or not primary_output.local_path.exists():
        _log.warning("quality_gate: no primary output to check")
        return GateResult(passed=True, skipped=True, skip_reason="no output"), []

    gate_frames_dir = scratch_dir / "gate_frames"
    gate_frames_dir.mkdir(parents=True, exist_ok=True)

    try:
        from ..edit_plan import QualityCriterion

        criteria = [
            QualityCriterion(**c) if isinstance(c, dict) else c
            for c in render_input.quality_criteria
        ]
        retry_counts, already_passed = load_retry_state(render_input.edit_plan_id)
        metadata = RenderMetadata(
            duration_seconds=primary_output.duration_seconds,
            aspect_ratio=primary_output.aspect,
            clip_windows=_output_clip_windows(render_input.trims, render_input.concat_order),
            captions=render_input.captions,
            digest_moment_labels=render_input.digest_moment_labels,
            game=render_input.game,
        )
        gate_result, revisions = run_quality_gate(
            primary_output.local_path,
            criteria,
            metadata,
            gate_frames_dir,
            retry_counts=retry_counts,
            already_passed=already_passed,
        )

        # Persist retry/pass state for the next render of this plan.
        new_counts = dict(retry_counts)
        new_passed = set(already_passed)
        for cr in gate_result.criteria_results:
            if cr.verdict.value == "pass":
                new_passed.add(cr.criterion_id)
            elif cr.verdict.value == "fail":
                new_counts[cr.criterion_id] = cr.retry_count + (
                    1 if any(r.criterion_id == cr.criterion_id for r in revisions) else 0
                )
        for req in revisions:
            new_counts[req.criterion_id] = max(
                new_counts.get(req.criterion_id, 0), req.retry_number
            )
        save_retry_state(
            render_input.edit_plan_id,
            retry_counts=new_counts,
            already_passed=new_passed,
        )
        return gate_result, revisions
    except Exception as exc:
        _log.exception("quality_gate error: %s", exc)
        return GateResult(passed=True, skipped=True, skip_reason=f"error: {type(exc).__name__}"), []


def _emit_revision_request(emit_fn, result: RenderResult, request) -> None:
    """Emit quality.revision_needed event for a failed criterion."""
    emit_fn(
        Event(
            event="quality.revision_needed",
            payload={
                "edit_plan_id": result.edit_plan_id,
                "edit_plan_version": result.edit_plan_version,
                "asset_id": result.asset_id,
                "criterion_id": request.criterion_id,
                "criterion_text": request.criterion_text,
                "reason": request.reason,
                "retry_number": request.retry_number,
            },
        )
    )


def _emit_success(emit_fn, result: RenderResult, gate_result=None) -> None:
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

    payload = {
        "edit_plan_id": result.edit_plan_id,
        "edit_plan_version": result.edit_plan_version,
        "asset_id": result.asset_id,
        "outputs": outputs_payload,
        "timings": result.timings,
    }
    if gate_result is not None:
        payload["quality_gate"] = gate_result.model_dump()

    emit_fn(Event(event="video.rendered", payload=payload))


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
                "error": _safe_error(result.error),
            },
        )
    )


def main(argv: list[str] | None = None) -> int:
    """CLI: ``box-render path/to/render_request.json`` on the Linux box."""
    parser = argparse.ArgumentParser(description="Run one box render_request JSON file")
    parser.add_argument("item", type=Path, help="path to a render_request queue JSON file")
    args = parser.parse_args(argv)

    if not settings.grokbot_box_render_enabled:
        print("box render disabled (set GROKBOT_BOX_RENDER_ENABLED=true)", file=sys.stderr)
        return 2

    from ..grokbot.drive import uploader_from_settings
    from ..grokbot.queue import parse_item

    text = args.item.read_text(encoding="utf-8")
    item = parse_item(text)
    if item.kind != "render_request":
        print(f"expected kind=render_request, got {item.kind}", file=sys.stderr)
        return 2

    uploader = uploader_from_settings()
    if uploader is None:
        print("Drive uploader not configured", file=sys.stderr)
        return 2

    result = run_render_request(item, uploader)
    if result.ok:
        print(json.dumps({"ok": True, "outputs": [o.drive_id for o in result.outputs]}))
        return 0
    print(json.dumps({"ok": False, "error": result.error}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
