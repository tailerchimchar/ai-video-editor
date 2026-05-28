"""KDA-change candidate source — CV / OCR over the scoreboard region.

The ranker's biggest blindspot for solo content is "did a kill actually
happen here?" Audio peaks fire when the player reacts; transcript_keyword
fires when they say "kill"; neither sees the in-game scoreboard. This
source reads the user's K/D/A digits straight off the HUD via tesseract,
sampling once every N seconds, and emits a candidate the moment any
digit increases.

Game-aware: reads the `scoreboard` region from the game's profile (League
+ Valorant both declare one). Falls back to the League shape if no
profile match — generic enough that the cost of "wrong region" is
"OCR sees nothing" rather than "crashes."

Tesseract is invoked via subprocess (same pattern as `league/ocr.py`); no
extra Python dep. If tesseract or ffmpeg is missing, OR the file is too
short to be a session recording, this source returns [] and the rest of
the pipeline continues unaffected.

Cost note: 1 frame per 5s on a 1.8hr file = ~1300 frame extractions +
OCR calls = ~5-10 min wall time. Bounded; not free. Cap via
`analyze_audio_max_seconds` (reused — same "is this a session
recording" guard).
"""

from __future__ import annotations

import contextlib
import logging
import re
import subprocess
import tempfile
from pathlib import Path

from ..config import settings
from ..profiles import region_box

_log = logging.getLogger(__name__)

# Sample one frame every N seconds. 5s balances recall (catch most
# kills, which take 3-5s of celebration animation) against OCR cost.
_SAMPLE_INTERVAL_SECONDS = 5.0

# Pad around the detected anchor when emitting the candidate window.
# The ranker can tighten via `suggested_start_seconds` later, and per-
# event-type widening (settings.event_window_overrides) extends further
# at compile time. This is just the initial guess.
_WINDOW_HALF_SECONDS = 5.0

# Cap each digit at this value — anything bigger is OCR noise (the
# scoreboard rarely shows >30 of anything in a normal League/Val game).
_MAX_DIGIT = 30

# Match patterns like "3/0/2", "3 / 0 / 2", "3|0|2", "3 0 2" — OCR
# isn't picky about the separator character, so we accept several.
_KDA_RE = re.compile(r"(\d{1,2})\s*[/|\\\s]+\s*(\d{1,2})\s*[/|\\\s]+\s*(\d{1,2})")


def _ffmpeg_available() -> bool:
    try:
        return subprocess.run(
            [settings.ffmpeg_path, "-version"],
            capture_output=True,
            text=True,
            timeout=3,
        ).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _tesseract_available() -> bool:
    try:
        return subprocess.run(
            [settings.tesseract_path, "--version"],
            capture_output=True,
            text=True,
            timeout=3,
        ).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _crop_expr_for_scoreboard(game: str | None) -> str:
    """ffmpeg crop expression for the game's scoreboard region.

    Region coords come from the per-game profile TOML (resolution-
    independent fractions). Fall back to the League scoreboard shape
    when no profile match — generic enough that "wrong region" yields
    "OCR sees nothing" rather than a crash.
    """
    region = region_box(game, "scoreboard")
    if region is None:
        # League shape: top-center bar, ~16% wide x 4% tall.
        return "crop=iw*0.16:ih*0.04:iw*0.42:0"
    return f"crop=iw*{region.w}:ih*{region.h}:iw*{region.x}:ih*{region.y}"


def _extract_frame(video_path: str, at_seconds: float, crop_expr: str, out_png: str) -> bool:
    """Pull one frame at `at_seconds`, crop to scoreboard, upscale, write PNG."""
    cmd = [
        settings.ffmpeg_path,
        "-y",
        "-ss", str(max(0.0, at_seconds)),
        "-i", video_path,
        "-vframes", "1",
        "-vf", f"{crop_expr},scale=iw*4:ih*4",
        out_png,
    ]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _ocr_kda(png_path: str) -> tuple[int, int, int] | None:
    """Tesseract on one frame; return (K, D, A) or None on failure."""
    cmd = [
        settings.tesseract_path,
        png_path,
        "stdout",
        "--psm", "7",  # single text line — matches the HUD strip
        "-c", "tessedit_char_whitelist=0123456789/|\\ ",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    m = _KDA_RE.search(out.stdout)
    if not m:
        return None
    k, d, a = int(m.group(1)), int(m.group(2)), int(m.group(3))
    # OCR sometimes hallucinates a digit (one becomes seventeen). Cap.
    if k > _MAX_DIGIT or d > _MAX_DIGIT or a > _MAX_DIGIT:
        return None
    return (k, d, a)


def _make_candidate(
    event_type: str,
    anchor: float,
    new_kda: tuple[int, int, int],
    prev_kda: tuple[int, int, int],
) -> dict:
    """Build a HighlightCandidate dict centered on the anchor."""
    return {
        "start_seconds": round(max(0.0, anchor - _WINDOW_HALF_SECONDS), 2),
        "end_seconds": round(anchor + _WINDOW_HALF_SECONDS, 2),
        "event_type": event_type,
        "confidence": 0.85,
        "metadata": {
            "anchor_seconds": round(anchor, 2),
            "kda_before": list(prev_kda),
            "kda_after": list(new_kda),
            "rationale": f"scoreboard OCR: {event_type} (K/D/A {prev_kda} -> {new_kda})",
        },
    }


def detect_kda_events(video_path: str, duration: float, game: str | None) -> list[dict]:
    """Sample the scoreboard, return one candidate per K/D/A increment.

    Each candidate's `event_type` reflects which value changed:
      - "kill"   when K increased
      - "death"  when D increased
      - "assist" when A increased

    A single sample interval can produce multiple events (kill + assist
    in the same teamfight), so one frame transition can emit 1-3
    candidates. The LLM ranker downstream collapses duplicates.

    `anchor_seconds` in metadata triggers `_window_anchor` strategy in
    highlights.cut_window, so the cut centers precisely on the
    transition midpoint with per-event-type pre/post widening from
    `settings.event_window_overrides`.

    Gracefully returns [] when tesseract / ffmpeg are missing, the
    video is too short, or the OCR finds nothing.
    """
    if duration <= settings.outplayed_clip_max_seconds:
        return []  # short Outplayed clips already have their own source
    if duration > settings.analyze_audio_max_seconds:
        return []  # bound resource use on pathologically long recordings

    if not _ffmpeg_available():
        _log.warning("cv_kda: ffmpeg not found, skipping")
        return []
    if not _tesseract_available():
        _log.warning("cv_kda: tesseract not found at %s, skipping", settings.tesseract_path)
        return []

    crop_expr = _crop_expr_for_scoreboard(game)
    tmpdir = Path(tempfile.gettempdir()) / f"cv_kda_{Path(video_path).stem}"
    tmpdir.mkdir(parents=True, exist_ok=True)

    out: list[dict] = []
    last_kda: tuple[int, int, int] | None = None
    last_t: float = 0.0
    t = 0.0
    frame_idx = 0

    try:
        while t < duration:
            png = tmpdir / f"f_{frame_idx:06d}.png"
            if _extract_frame(video_path, t, crop_expr, str(png)):
                kda = _ocr_kda(str(png))
                if kda is not None:
                    if last_kda is not None:
                        # Use the midpoint between samples as the anchor —
                        # the event happened SOMEWHERE in that 5-second
                        # window; centering on midpoint is the least-wrong
                        # guess without per-frame OCR.
                        anchor = (last_t + t) / 2.0
                        if kda[0] > last_kda[0]:
                            out.append(_make_candidate("kill", anchor, kda, last_kda))
                        if kda[1] > last_kda[1]:
                            out.append(_make_candidate("death", anchor, kda, last_kda))
                        if kda[2] > last_kda[2]:
                            out.append(_make_candidate("assist", anchor, kda, last_kda))
                    last_kda = kda
                    last_t = t
            png.unlink(missing_ok=True)
            frame_idx += 1
            t += _SAMPLE_INTERVAL_SECONDS
    finally:
        # Defensive cleanup if we bailed mid-loop.
        for f in tmpdir.glob("*.png"):
            f.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            tmpdir.rmdir()

    return out
