"""Extract a thumbnail JPG from a rendered compilation.

Strategy (v1): pick the single frame from the **middle of the highest-hype
non-intro clip** in the spec. Combined with hook ordering, that's
usually the first clip after the intro — so the thumbnail and the reel's
opening shot reinforce each other.

Tradeoffs:
- Single still-frame extraction is cheap (~100 ms via ffmpeg seek).
- "Midpoint of best clip" is a coarse heuristic; could be smarter (face
  detection, motion peak, brightest action frame) but those are sprint-
  level features. The v1 heuristic gets a usable thumbnail without ML.

Output: `<compilation>/thumbnail.jpg` (overwritten on each render).
Auto-runs after a successful concat in `render_spec` so every render
emits a fresh thumbnail. Tests opt out via `extract_thumbnail=False`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import settings

_THUMBNAIL_FILENAME = "thumbnail.jpg"
_CLIP_THUMBNAILS_DIRNAME = "_thumbnails"


def thumbnail_path(folder: Path) -> Path:
    return Path(folder) / _THUMBNAIL_FILENAME


def clip_thumbnails_dir(folder: Path) -> Path:
    """Per-clip thumbnails live under `<comp>/_thumbnails/<clip_id>.jpg`.

    Leading underscore keeps them visually separate from spec/log files
    and matches the convention used for `_parts/`."""
    return Path(folder) / _CLIP_THUMBNAILS_DIRNAME


def clip_thumbnail_path(folder: Path, clip_id: str) -> Path:
    return clip_thumbnails_dir(folder) / f"{clip_id}.jpg"


def _pick_thumbnail_clip(spec: dict) -> dict | None:
    """Choose the clip whose middle frame becomes the thumbnail.

    Rules, in order:
      1. Highest `hype_score` among non-intro clips.
      2. Tie-break: the chronologically-first clip (most likely the
         hook-ordered first non-intro clip).
      3. None if there are no non-intro clips.

    Intros are excluded because the thumbnail should reflect gameplay,
    not branding.
    """
    clips = [c for c in (spec.get("clips") or []) if c.get("event_type") != "intro"]
    if not clips:
        return None
    return max(
        clips,
        key=lambda c: (
            float(c.get("hype_score") or 0),
            -float(c.get("start_seconds") or 0),  # earlier clip wins ties
        ),
    )


def _reel_midpoint(spec: dict, target_clip: dict) -> float:
    """Reel-time seconds of the middle frame of `target_clip`.

    Walks the spec's clips in order, summing durations until we reach
    the target. The midpoint is at running_offset + clip_duration / 2.
    """
    running = 0.0
    target_id = target_clip.get("id")
    for clip in spec.get("clips") or []:
        dur = max(0.0, float(clip["end_seconds"]) - float(clip["start_seconds"]))
        if clip.get("id") == target_id:
            return running + dur / 2.0
        running += dur
    # Fallback: middle of reel if the clip somehow isn't in the spec
    return max(0.0, running / 2.0)


def extract_thumbnail(folder: Path, spec: dict, video_path: Path) -> dict:
    """Pull a single frame from the rendered compilation and save as JPG.

    Returns a structured summary dict (never raises) so it can be
    embedded in `render_spec`'s response without try/except gymnastics.

    `video_path` is the rendered `compilation.mp4`. We seek into THAT
    rather than re-encoding from source — it's already the final
    composition including intros, captions, and effects.
    """
    folder = Path(folder)
    if not video_path.is_file():
        return {"ok": False, "path": None, "reason": "video_not_found"}

    target = _pick_thumbnail_clip(spec)
    if target is None:
        return {"ok": False, "path": None, "reason": "no_gameplay_clips"}

    seek = _reel_midpoint(spec, target)
    out = thumbnail_path(folder)
    cmd = [
        settings.ffmpeg_path,
        "-y",
        "-ss",
        f"{seek:.3f}",
        "-i",
        video_path.as_posix(),
        "-frames:v",
        "1",
        "-q:v",
        "2",  # high-quality jpeg (1=best, 31=worst). 2 is a strong default.
        out.as_posix(),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return {"ok": False, "path": None, "reason": "ffmpeg_not_found"}
    if result.returncode != 0:
        return {
            "ok": False,
            "path": None,
            "reason": "ffmpeg_failed",
            "error": result.stderr[-500:],
        }
    return {
        "ok": True,
        "path": out.as_posix(),
        "source_clip_id": target.get("id"),
        "source_clip_hype": target.get("hype_score"),
        "seek_seconds": round(seek, 3),
    }


def safe_extract_thumbnail(folder: Path, spec: dict, video_path: Path) -> dict:
    """Wrap `extract_thumbnail` so any unexpected exception becomes a
    structured failure rather than breaking `render_spec`.

    Mirrors `safe_cleanup_for_render`'s contract: render success must
    not depend on this auxiliary step succeeding.
    """
    try:
        return extract_thumbnail(folder, spec, video_path)
    except Exception as exc:
        return {"ok": False, "path": None, "reason": "exception", "error": str(exc)[:500]}


# ----- per-clip thumbnails (filmstrip support) -------------------------
#
# Each clip in the spec gets its own thumbnail extracted from the
# midpoint of its SOURCE range. These power the horizontal filmstrip
# in the webapp — every tile shows a real frame of the clip's content,
# so the user can visually identify clips at a glance.
#
# Filename = `<folder>/_thumbnails/<clip_id>.jpg`. We use the full
# UUID (not the 8-char prefix the part files use) because thumbnails
# don't need filename-level cache invalidation by clip-position.


def _clip_source_midpoint(clip: dict) -> float:
    """Seek time (seconds) into a clip's source for the thumbnail frame."""
    start = float(clip.get("start_seconds") or 0.0)
    end = float(clip.get("end_seconds") or start)
    return max(0.0, start + (end - start) / 2.0)


def extract_clip_thumbnail(folder: Path, clip: dict, *, force: bool = False) -> dict:
    """Extract ONE clip's midpoint frame to `_thumbnails/<clip_id>.jpg`.

    Reads from `clip.asset_path` directly (the source recording, or the
    intro mp4 for intro clips). Idempotent: skips when the file already
    exists unless `force=True`. Returns a structured summary — never
    raises so the caller (render_spec) can swallow failures.
    """
    folder = Path(folder)
    clip_id = clip.get("id")
    if not clip_id:
        return {"ok": False, "reason": "missing_clip_id"}

    asset_path = clip.get("asset_path")
    if not asset_path or not Path(asset_path).is_file():
        return {"ok": False, "reason": "asset_path_missing", "clip_id": clip_id}

    out = clip_thumbnail_path(folder, clip_id)
    if out.exists() and not force:
        return {"ok": True, "reason": "already_exists", "path": out.as_posix(), "clip_id": clip_id}

    out.parent.mkdir(parents=True, exist_ok=True)
    seek = _clip_source_midpoint(clip)
    cmd = [
        settings.ffmpeg_path,
        "-y",
        # Put `-ss` BEFORE `-i` for fast keyframe seeking — critical when
        # extracting deep into a 30-minute VOD. The frame might land on
        # the nearest keyframe rather than the exact midpoint; acceptable
        # tradeoff for a thumbnail.
        "-ss",
        f"{seek:.3f}",
        "-i",
        asset_path,
        "-frames:v",
        "1",
        "-q:v",
        "3",  # slightly more compressed than the hero thumbnail
        # Scale to a fixed-ish width to keep file sizes small. -1 preserves
        # aspect ratio; ffmpeg picks the height.
        "-vf",
        "scale=320:-1",
        out.as_posix(),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return {"ok": False, "reason": "ffmpeg_not_found", "clip_id": clip_id}
    if result.returncode != 0:
        return {
            "ok": False,
            "reason": "ffmpeg_failed",
            "clip_id": clip_id,
            "error": result.stderr[-500:],
        }
    return {"ok": True, "path": out.as_posix(), "clip_id": clip_id, "seek_seconds": round(seek, 3)}


def extract_clip_thumbnails(folder: Path, spec: dict, *, force: bool = False) -> dict:
    """Extract a thumbnail for EVERY clip in the spec.

    Skips clips whose thumbnail file already exists unless `force=True`.
    Always returns a summary listing successes + failures; caller can
    decide how to surface them (the render_spec wrapper logs but
    doesn't fail).
    """
    folder = Path(folder)
    results: list[dict] = []
    for clip in spec.get("clips") or []:
        results.append(extract_clip_thumbnail(folder, clip, force=force))
    return {
        "total": len(results),
        "ok": sum(1 for r in results if r.get("ok")),
        "failed": [r for r in results if not r.get("ok")],
    }


def safe_extract_clip_thumbnails(folder: Path, spec: dict, *, force: bool = False) -> dict:
    """Same swallow-all-errors wrapper as `safe_extract_thumbnail`.

    Called from `render_spec` after a successful concat. Best-effort:
    a thumbnail-extraction hiccup must not fail the render the user
    already paid ffmpeg time for.
    """
    try:
        return extract_clip_thumbnails(folder, spec, force=force)
    except Exception as exc:
        return {"ok": False, "reason": "exception", "error": str(exc)[:500]}


# ----- per-source-asset thumbnails (assets gallery) --------------------
#
# Source recordings (raw Outplayed sessions, ingested Twitch VODs) live
# in OUTPLAYED_MEDIA_DIR — which is READ-ONLY per the source-files-
# immutable rule (see CLAUDE.md). So asset thumbnails live in
# WORKSPACE_DIR/asset_thumbnails/<asset_id>.jpg, keeping the source
# folder pristine.
#
# A midpoint frame is "good enough" — gameplay sessions are statistically
# in-game by the middle of the file. No spec to walk; we just need the
# file's duration and seek to half.


_ASSET_THUMBNAILS_DIRNAME = "asset_thumbnails"


def asset_thumbnails_dir() -> Path:
    """`<workspace>/asset_thumbnails/` — created on first use."""
    return settings.workspace_dir / _ASSET_THUMBNAILS_DIRNAME


def asset_thumbnail_path(asset_id: str) -> Path:
    return asset_thumbnails_dir() / f"{asset_id}.jpg"


def extract_asset_thumbnail(asset_id: str, asset_path: str, *, force: bool = False) -> dict:
    """Pull a midpoint frame from a source recording.

    Output: `<workspace>/asset_thumbnails/<asset_id>.jpg`. Idempotent —
    skips when the file already exists unless `force=True`. Returns a
    structured summary, never raises.

    We DON'T duration-probe the file (one ffprobe per asset adds up on
    libraries with 100+ recordings). Instead we let ffmpeg seek to a
    fixed early-ish position (60s in) — for Outplayed clips this lands
    mid-action; for full sessions it lands mid-loading-screen or early
    laning, which is still recognizable. Trade-off: simpler + 1 fewer
    process per asset.
    """
    out = asset_thumbnail_path(asset_id)
    if out.exists() and not force:
        return {"ok": True, "reason": "already_exists", "path": out.as_posix()}

    if not Path(asset_path).is_file():
        return {"ok": False, "reason": "asset_path_missing", "asset_id": asset_id}

    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        settings.ffmpeg_path,
        "-y",
        # Fast keyframe seek BEFORE -i for speed. Lands near the request
        # rather than exactly on it — fine for a thumbnail.
        "-ss",
        "60.0",
        "-i",
        asset_path,
        "-frames:v",
        "1",
        "-q:v",
        "3",
        "-vf",
        "scale=480:-1",  # bigger than per-clip thumbs since asset tiles are bigger
        out.as_posix(),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"ok": False, "reason": "ffmpeg_not_found_or_timeout", "asset_id": asset_id}
    if result.returncode != 0:
        return {
            "ok": False,
            "reason": "ffmpeg_failed",
            "asset_id": asset_id,
            "error": result.stderr[-500:],
        }
    return {"ok": True, "path": out.as_posix(), "asset_id": asset_id}


def safe_extract_asset_thumbnail(asset_id: str, asset_path: str, *, force: bool = False) -> dict:
    """Swallow-all-exceptions wrapper, like other safe_extract_* helpers."""
    try:
        return extract_asset_thumbnail(asset_id, asset_path, force=force)
    except Exception as exc:
        return {"ok": False, "reason": "exception", "error": str(exc)[:500]}


def cleanup_orphan_clip_thumbnails(folder: Path, spec: dict) -> dict:
    """Delete `_thumbnails/<id>.jpg` files whose clip is no longer in the spec.

    Mirrors the `compile_cleanup` pattern. Safe + idempotent. Currently
    called manually (or could be wired into render_spec alongside parts
    cleanup later). Returns a summary listing what got deleted."""
    folder = Path(folder)
    thumbs_dir = clip_thumbnails_dir(folder)
    if not thumbs_dir.is_dir():
        return {"deleted": 0, "deleted_files": []}

    valid_ids = {clip.get("id") for clip in (spec.get("clips") or []) if clip.get("id")}
    deleted: list[str] = []
    for thumb in thumbs_dir.glob("*.jpg"):
        # Filename is the clip UUID. If that UUID isn't in the current
        # spec, it's an orphan from a removed clip.
        if thumb.stem not in valid_ids:
            try:
                thumb.unlink()
                deleted.append(thumb.name)
            except OSError:
                continue
    return {"deleted": len(deleted), "deleted_files": deleted}
