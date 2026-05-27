"""OCR cross-check for the per-recording Riot offset.

The audio↔kill alignment (`calibrate.align_offset`) is primary. When its
lock is weak (quiet game, sparse kills), read the in-game clock straight
off a sampled frame as an independent second opinion: that clock *is*
Riot's game-clock, so it pins the offset directly.

Best-effort and fully graceful: needs the `tesseract` binary (called as
a CLI, like ffmpeg — no extra Python dep). If it's missing, the HUD
isn't where we look, or the digits don't parse, it returns a reason and
the pipeline carries on with the audio offset. Never fatal.
"""

import re
import subprocess
import tempfile
from pathlib import Path

from ..config import settings

# LoL clock sits top-center. We can't know the user's resolution, so
# crop a generous top-center band and let tesseract find the digits.
_CROP = "crop=iw/5:ih/12:iw*2/5:0"  # w,h,x,y — middle fifth, top 1/12
_CLOCK_RE = re.compile(r"(\d{1,2}):(\d{2})")


def _frame_png(video_path: str, at_seconds: float, out_png: str) -> bool:
    cmd = [
        settings.ffmpeg_path,
        "-y",
        "-ss",
        str(max(0.0, at_seconds)),
        "-i",
        video_path,
        "-vframes",
        "1",
        "-vf",
        f"{_CROP},scale=iw*4:ih*4",  # upscale — small HUD digits OCR poorly
        out_png,
    ]
    try:
        return subprocess.run(cmd, capture_output=True, text=True).returncode == 0
    except FileNotFoundError:
        return False


def _ocr_clock_seconds(png_path: str) -> int | None:
    cmd = [
        settings.tesseract_path,
        png_path,
        "stdout",
        "--psm",
        "7",
        "-c",
        "tessedit_char_whitelist=0123456789:",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return None
    if out.returncode != 0:
        return None
    m = _CLOCK_RE.search(out.stdout)
    if not m:
        return None
    mm, ss = int(m.group(1)), int(m.group(2))
    if ss >= 60:
        return None
    return mm * 60 + ss


def verify_offset(video_path: str, sample_vod_seconds: float, expected_game_clock: float) -> dict:
    """Read the on-screen clock at `sample_vod_seconds` and derive the
    offset it implies. Returns a diagnostic dict; never raises.

    offset = vod_time - game_clock_shown (so anchor = game_clock + offset).
    """
    tmp = Path(tempfile.gettempdir()) / f"avo_{Path(video_path).stem}.png"
    try:
        if not _frame_png(video_path, sample_vod_seconds, str(tmp)):
            return {"ocr_available": False, "reason": "frame extract failed"}
        clock = _ocr_clock_seconds(str(tmp))
        if clock is None:
            return {"ocr_available": False, "reason": "clock not read (tesseract/HUD)"}
        ocr_offset = round(sample_vod_seconds - clock, 1)
        audio_offset = sample_vod_seconds - expected_game_clock
        return {
            "ocr_available": True,
            "ocr_clock_seconds": clock,
            "ocr_offset_seconds": ocr_offset,
            "agrees_within_s": round(abs(ocr_offset - audio_offset), 1),
        }
    finally:
        tmp.unlink(missing_ok=True)
