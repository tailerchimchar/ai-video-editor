"""Killfeed OCR — extract kill-timestamp events by scanning the
killfeed HUD region for the user's name on the LEFT (killer) side of
each row.

Used as a VERIFIER over the VLM clip-analyzer's suggested payoff
moments (see shorts.py::_verify_moments_via_killfeed): the OCR
provides ground truth for "did user_name actually appear in the
killfeed near time T?" — the VLM's hallucination check.

Open/closed by design: `_OCR_BACKENDS` maps a backend name to a
frame-OCR function. Adding paddleocr or a custom backend = new
function + one dict entry. Backend picks from
`settings.killfeed_ocr_backend`.

Cost: at 1 fps on a 30s Outplayed clip = 30 OCR calls per clip = a few
seconds total. Cached via `<clip>.killfeed.json` sidecar so re-runs
are free.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz

from .config import settings
from .profiles import region_box

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Cache — sidecar next to the clip file
# ---------------------------------------------------------------------


_CACHE_VERSION = 1  # bump when the algorithm changes


def _cache_path(clip_path: Path) -> Path:
    return clip_path.with_suffix(clip_path.suffix + ".killfeed.json")


# ---------------------------------------------------------------------
# ffmpeg / tesseract availability
# ---------------------------------------------------------------------


def _ffmpeg_available() -> bool:
    try:
        return (
            subprocess.run(
                [settings.ffmpeg_path, "-version"],
                capture_output=True,
                text=True,
                timeout=3,
            ).returncode
            == 0
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _tesseract_available() -> bool:
    try:
        return (
            subprocess.run(
                [settings.tesseract_path, "--version"],
                capture_output=True,
                text=True,
                timeout=3,
            ).returncode
            == 0
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


# ---------------------------------------------------------------------
# ROI helpers
# ---------------------------------------------------------------------


def _killfeed_crop_expr(game: str | None) -> str:
    """ffmpeg crop expression for the killfeed ROI, LEFT HALF only.

    We only OCR the LEFT half because that's where the killer name
    sits in every Valorant killfeed row. Cuts OCR work in half AND
    excludes the victim name (avoids counting kills BY the enemy of
    someone named similarly to the user).
    """
    region = region_box(game, "killfeed")
    if region is None:
        # Valorant fallback: same coords as profiles/valorant.toml
        # [regions.killfeed] — top-right, tall, wide.
        rx, ry, rw, rh = 0.78, 0.08, 0.22, 0.28
    else:
        rx, ry, rw, rh = region.x, region.y, region.w, region.h
    # LEFT half only. Killer name is roughly at the left ~60% of a
    # killfeed row (name + weapon icon), so grab 60% width from left.
    half_w = rw * 0.6
    return f"crop=iw*{half_w}:ih*{rh}:iw*{rx}:ih*{ry}"


# ---------------------------------------------------------------------
# OCR backends (open/closed hook — register new implementations here)
# ---------------------------------------------------------------------


def _ocr_tesseract(png_path: Path) -> str:
    """Return raw OCR text from tesseract; empty string on failure."""
    cmd = [
        settings.tesseract_path,
        str(png_path),
        "stdout",
        "--psm", "11",  # sparse text — find text anywhere in the image
        "-c", "tessedit_char_whitelist="
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 _",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return out.stdout if out.returncode == 0 else ""


_OCR_BACKENDS: dict[str, Callable[[Path], str]] = {
    "tesseract": _ocr_tesseract,
}


def _resolve_backend() -> Callable[[Path], str] | None:
    """Pick the OCR backend from config. Returns None if the chosen
    backend is missing / unavailable (compile still ships without OCR)."""
    name = settings.killfeed_ocr_backend
    backend = _OCR_BACKENDS.get(name)
    if backend is None:
        _log.warning(
            "killfeed: unknown backend %r, expected one of %s",
            name,
            sorted(_OCR_BACKENDS),
        )
        return None
    if name == "tesseract" and not _tesseract_available():
        _log.info(
            "killfeed: tesseract not on PATH (%s) — skipping OCR verifier",
            settings.tesseract_path,
        )
        return None
    return backend


# ---------------------------------------------------------------------
# Frame extraction + OCR loop
# ---------------------------------------------------------------------


def _extract_frame(
    video_path: str, at_seconds: float, crop_expr: str, out_png: Path
) -> bool:
    """Pull one frame at `at_seconds`, crop to killfeed LEFT half,
    upscale 3x for better OCR, write PNG."""
    cmd = [
        settings.ffmpeg_path,
        "-y",
        "-ss", f"{max(0.0, at_seconds):.3f}",
        "-i", video_path,
        "-vframes", "1",
        "-vf", f"{crop_expr},scale=iw*3:ih*3",
        str(out_png),
    ]
    try:
        return (
            subprocess.run(cmd, capture_output=True, text=True, timeout=10).returncode
            == 0
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# Killfeed row detection: OCR sees multiple lines, one per row. We
# match the player name on each line INDEPENDENTLY. Any line where
# a name-token has fuzzy score >= threshold counts as "user is a
# killer in this frame."
def _line_matches_name(line: str, name: str, threshold: int) -> bool:
    """Fuzzy-match `name` against the tokens in `line`. Tolerates
    OCR noise like N00dlz / Noodl2."""
    if not line.strip() or not name.strip():
        return False
    tokens = re.findall(r"[A-Za-z0-9_]{3,}", line)
    return any(fuzz.ratio(name.lower(), t.lower()) >= threshold for t in tokens)


def _count_player_rows(text: str, name: str, threshold: int) -> int:
    """Count how many killfeed rows in this frame's OCR show the user
    as the KILLER (i.e. their name appears on this LEFT-half crop)."""
    if not text.strip():
        return 0
    lines = [line for line in text.splitlines() if line.strip()]
    return sum(1 for line in lines if _line_matches_name(line, name, threshold))


# ---------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class KillfeedTrack:
    """OCR-derived kill timestamps for one clip file."""

    clip_path: Path
    player_name: str
    game: str | None
    kill_timestamps: tuple[float, ...]
    """Seconds (from clip start) at which a NEW killfeed row appeared
    showing the player as killer. Rising-edge detection over per-frame
    counts."""
    frame_count: int


def _load_cached(
    cache: Path,
    *,
    expected_version: int,
    expected_player: str,
    expected_backend: str,
) -> KillfeedTrack | None:
    if not cache.is_file():
        return None
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        data.get("version") != expected_version
        or data.get("player_name") != expected_player
        or data.get("backend") != expected_backend
    ):
        return None
    return KillfeedTrack(
        clip_path=Path(data["clip_path"]),
        player_name=data["player_name"],
        game=data.get("game"),
        kill_timestamps=tuple(float(t) for t in data.get("kill_timestamps", [])),
        frame_count=int(data.get("frame_count", 0)),
    )


def _save_cached(cache: Path, track: KillfeedTrack, backend: str) -> None:
    payload = {
        "version": _CACHE_VERSION,
        "clip_path": str(track.clip_path),
        "player_name": track.player_name,
        "game": track.game,
        "backend": backend,
        "kill_timestamps": list(track.kill_timestamps),
        "frame_count": track.frame_count,
    }
    with contextlib.suppress(OSError):
        cache.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def detect_killfeed_kills(
    clip_path: Path | str,
    duration_seconds: float,
    *,
    player_name: str,
    game: str | None,
    sample_interval_seconds: float = 1.0,
    use_cache: bool = True,
) -> KillfeedTrack | None:
    """Scan the killfeed area of `clip_path` for kills by `player_name`.

    Returns a KillfeedTrack with detected kill timestamps (seconds
    from clip start). Returns None if:
      - `player_name` is empty (verifier disabled)
      - OCR backend isn't available (tesseract missing)
      - ffmpeg missing
      - the clip file is missing

    Cached via sidecar `<clip>.killfeed.json` keyed by
    (version, player_name, backend).
    """
    path = Path(clip_path)
    if not path.is_file() or not player_name.strip():
        return None
    if not settings.killfeed_verifier_enabled:
        return None

    backend = _resolve_backend()
    if backend is None:
        return None
    if not _ffmpeg_available():
        _log.warning("killfeed: ffmpeg not found — skipping verifier")
        return None

    cache = _cache_path(path)
    if use_cache:
        cached = _load_cached(
            cache,
            expected_version=_CACHE_VERSION,
            expected_player=player_name,
            expected_backend=settings.killfeed_ocr_backend,
        )
        if cached is not None:
            return cached

    crop_expr = _killfeed_crop_expr(game)
    threshold = settings.killfeed_name_match_threshold

    tmpdir = Path(tempfile.gettempdir()) / f"killfeed_{path.stem}"
    tmpdir.mkdir(parents=True, exist_ok=True)

    prev_count = 0
    kill_times: list[float] = []
    frame_idx = 0
    t = 0.0

    try:
        while t <= duration_seconds:
            png = tmpdir / f"f_{frame_idx:06d}.png"
            if _extract_frame(str(path), t, crop_expr, png):
                text = backend(png)
                count = _count_player_rows(text, player_name, threshold)
                # Rising edge = new kill just appeared in the killfeed.
                # Killfeed rows appear ~500ms after the kill sfx so we
                # subtract 0.5s to get closer to the actual kill moment.
                if count > prev_count:
                    kill_times.append(max(0.0, round(t - 0.5, 3)))
                prev_count = count
            png.unlink(missing_ok=True)
            frame_idx += 1
            t += sample_interval_seconds
    finally:
        for f in tmpdir.glob("*.png"):
            f.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            tmpdir.rmdir()

    track = KillfeedTrack(
        clip_path=path,
        player_name=player_name,
        game=game,
        kill_timestamps=tuple(kill_times),
        frame_count=frame_idx,
    )
    if use_cache:
        _save_cached(cache, track, backend=settings.killfeed_ocr_backend)
    return track


# ---------------------------------------------------------------------
# Verifier — cross-check VLM-called anchors against killfeed kills
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class VerifiedMoment:
    """Result of verifying one VLM-called anchor against the killfeed."""

    original_anchor: float
    """The VLM analyzer's payoff_anchor_seconds (offset from clip start)."""

    verified: bool
    """True when a matching killfeed row exists within tolerance."""

    snapped_anchor: float
    """The killfeed-derived anchor (or original if unverified)."""

    matched_kill_time: float | None
    """The killfeed timestamp that matched, if any."""


def verify_anchor(
    original_anchor: float,
    track: KillfeedTrack | None,
    *,
    tolerance_seconds: float | None = None,
) -> VerifiedMoment:
    """Check `original_anchor` against the killfeed track.

    If any killfeed kill is within `tolerance_seconds`, the moment is
    verified and the anchor is snapped to the closest killfeed time.
    Otherwise unverified; caller can drop or downgrade.

    When `track` is None (verifier disabled / OCR missing), returns a
    verified passthrough — we don't want a missing verifier to drop
    real moments.
    """
    tol = (
        settings.killfeed_snap_tolerance_seconds
        if tolerance_seconds is None
        else tolerance_seconds
    )
    if track is None:
        return VerifiedMoment(
            original_anchor=original_anchor,
            verified=True,
            snapped_anchor=original_anchor,
            matched_kill_time=None,
        )
    best_delta = float("inf")
    best_time: float | None = None
    for kt in track.kill_timestamps:
        delta = abs(kt - original_anchor)
        if delta < best_delta:
            best_delta = delta
            best_time = kt
    if best_time is not None and best_delta <= tol:
        return VerifiedMoment(
            original_anchor=original_anchor,
            verified=True,
            snapped_anchor=best_time,
            matched_kill_time=best_time,
        )
    return VerifiedMoment(
        original_anchor=original_anchor,
        verified=False,
        snapped_anchor=original_anchor,
        matched_kill_time=None,
    )
