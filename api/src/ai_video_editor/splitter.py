"""Multi-game VOD splitter.

A 1.5hr Twitch scrim VOD often contains 3+ games separated by black
loading screens, "back to lobby" transitions, and queue waits. The
pipeline treats it as one continuous source — Riot correlation breaks,
candidate-source clusters span games, and the narrative compile mode
can't tell where one game ends and the next begins.

This module detects the boundaries via ffmpeg's `blackdetect` filter
and (optionally) slices the parent VOD into per-game child files. Each
child becomes its own asset, with `parent_asset_id` pointing back to
the source.

Pure parsing/heuristics (no I/O) sit at the top; the ffmpeg-touching
wrapper is at the bottom for testability + offloading via
`asyncio.to_thread`.
"""

from __future__ import annotations

import itertools
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import settings

_log = logging.getLogger(__name__)

# Match lines like:
#   [blackdetect @ 0x...] black_start:1234.5 black_end:1236.7 black_duration:2.2
_BLACK_RE = re.compile(
    r"black_start:(?P<start>[\d.]+)\s+black_end:(?P<end>[\d.]+)\s+black_duration:(?P<dur>[\d.]+)"
)

# Defaults for the blackdetect filter:
# - `d` (min duration): ignore brief fades — 2s catches loading screens
#   and post-game return-to-lobby sequences but skips quick scene cuts.
# - `pix_th` (per-pixel threshold): 0.10 = pixels darker than 10% of
#   white are considered "black." Higher catches near-black gradients.
DEFAULT_MIN_BLACK_SECONDS = 2.0
DEFAULT_PIX_THRESHOLD = 0.10

# Refuse to call a transition a "game boundary" unless the surrounding
# segments are each at least this long — a single 30-second segment in
# a 1.5hr VOD is almost certainly a UI glitch, not a real game.
MIN_GAME_LENGTH_SECONDS = 60.0

# Defensive cap on how long we wait for blackdetect to scan the VOD.
# ffmpeg can stream through a 1.5hr file in ~30-60s for blackdetect,
# but we don't want a hung subprocess to block the job forever.
DETECT_TIMEOUT_SECONDS = 600


@dataclass(frozen=True)
class BlackInterval:
    """One detected black region in the VOD."""

    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def midpoint(self) -> float:
        return (self.start + self.end) / 2.0


@dataclass(frozen=True)
class GameSegment:
    """One detected game — the segment BETWEEN consecutive black intervals
    (or between the start/end of the file and the nearest black)."""

    start: float
    end: float
    # 1-based index for filename construction ("game1", "game2", ...).
    index: int

    @property
    def duration(self) -> float:
        return self.end - self.start


def parse_blackdetect_output(stderr_text: str) -> list[BlackInterval]:
    """Pull every black interval out of ffmpeg's stderr.

    Pure — operates only on the captured stderr string. Returns intervals
    in the order ffmpeg emitted them (which is also chronological).
    """
    out: list[BlackInterval] = []
    for match in _BLACK_RE.finditer(stderr_text):
        out.append(
            BlackInterval(
                start=float(match.group("start")),
                end=float(match.group("end")),
            )
        )
    return out


def intervals_to_segments(
    intervals: list[BlackInterval],
    duration: float,
    *,
    min_segment_length: float = MIN_GAME_LENGTH_SECONDS,
) -> list[GameSegment]:
    """Convert a list of black intervals into game-segment ranges.

    The recording's full timeline is `[0, duration]`. Each black interval
    splits it at its midpoint. Segments shorter than `min_segment_length`
    are dropped — they're almost always UI artifacts (a flash of black
    on a death cam, a brief fade) rather than real game boundaries.

    Pure. No file I/O.
    """
    if duration <= 0:
        return []
    # Split points are the midpoints of the black intervals.
    split_points = sorted(i.midpoint for i in intervals if 0 < i.midpoint < duration)
    cuts = [0.0, *split_points, duration]
    segments: list[GameSegment] = []
    for start, end in itertools.pairwise(cuts):
        if end - start < min_segment_length:
            continue
        segments.append(GameSegment(start=round(start, 2), end=round(end, 2), index=0))
    # Renumber after filtering so the indexes are contiguous.
    return [
        GameSegment(start=s.start, end=s.end, index=i + 1)
        for i, s in enumerate(segments)
    ]


def detect_game_boundaries(
    video_path: str,
    *,
    min_black_seconds: float = DEFAULT_MIN_BLACK_SECONDS,
    pix_threshold: float = DEFAULT_PIX_THRESHOLD,
) -> list[BlackInterval]:
    """Run ffmpeg blackdetect on the file. Returns the raw intervals.

    No segmentation logic here — that's `intervals_to_segments` so we
    can unit-test each layer independently.

    Returns [] on any ffmpeg failure (subprocess error, timeout, missing
    binary). Never raises — boundary detection is best-effort, and the
    caller falls back to "treat the file as one segment."
    """
    cmd = [
        settings.ffmpeg_path,
        "-hide_banner",
        "-i", video_path,
        "-vf", f"blackdetect=d={min_black_seconds}:pix_th={pix_threshold}",
        "-an",   # ignore audio for speed
        "-sn",   # ignore subtitles
        "-f", "null",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=DETECT_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        _log.warning("detect_game_boundaries failed: %s", exc)
        return []
    # blackdetect lines are written to stderr regardless of returncode.
    return parse_blackdetect_output(result.stderr or "")


def split_segment(
    source_path: str,
    out_path: str,
    start: float,
    end: float,
) -> tuple[bool, str | None]:
    """Stream-copy a (start, end) range from `source_path` to `out_path`.

    Uses `-c copy` so there's no re-encode — the output keeps the source
    quality + finishes in seconds rather than minutes. Drawback: cuts
    snap to the nearest keyframe, so the start of a split segment might
    be 0-2s off from the requested time. Acceptable for our use case
    (game boundaries are coarse), and we save 10x ffmpeg time vs a
    re-encode.

    Returns (ok, error_message_or_none). Never raises.
    """
    cmd = [
        settings.ffmpeg_path,
        "-y",
        "-ss", f"{start:.3f}",
        "-i", source_path,
        "-to", f"{end - start:.3f}",  # -to with -ss BEFORE -i means duration
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        out_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, f"ffmpeg split failed: {exc}"
    if result.returncode != 0:
        return False, (result.stderr or "ffmpeg split failed")[-500:]
    return True, None


def child_filename(parent_filename: str, segment: GameSegment) -> str:
    """`scrim.mp4` + segment 2 → `scrim_game2.mp4`. Pure."""
    stem = Path(parent_filename).stem
    suffix = Path(parent_filename).suffix or ".mp4"
    return f"{stem}_game{segment.index}{suffix}"
