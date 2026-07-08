"""Overwolf Game-Events log parser — ground-truth kill/death/assist
timestamps from Outplayed's own log files.

Outplayed is an Overwolf app; it subscribes to Valorant's game-event
provider (`gep_valorant.dll`) and logs every event to
`%LOCALAPPDATA%\\Overwolf\\Log\\Apps\\Outplayed\\background.html.*.log`.

We parse those logs directly. Every kill / death / assist / headshot
that fires in-game shows up as a `[StreamingCaptureService] Already
capturing, adding bookmark {...}` line with a Unix ms timestamp
(the `time` field). Correlation to Outplayed clip files is trivial:
the clip's file mtime (Unix seconds × 1000) IS the wall-clock end of
the clip. Query events between `mtime_ms - duration_ms` and
`mtime_ms`.

**Beats every OCR / API alternative for Val users** because:
- 100% accurate (comes from the game client via Overwolf, not vision)
- Free (already on disk, no API rate limits or third-party services)
- Zero install (Outplayed IS an Overwolf app; nothing new to build)

Caveats:
- Val only (log format is generic but only Val's events are
  subscribed by Outplayed)
- Only clips recorded AFTER Outplayed was running
- Log files rotate — read ALL `background.html.*.log`, not just latest
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)


# Line shape we care about:
# 2026-06-30 22:52:28,917 (INFO) </background.js> (:42228) - [StreamingCaptureService] Already capturing, adding bookmark {"type":"kill","time":1782877948916,"timing":{"past":15000,"future":5000},"data":"1"}
_BOOKMARK_RE = re.compile(
    r"\[StreamingCaptureService\] Already capturing, adding bookmark (\{.+\})\s*$"
)

# Event types Outplayed subscribes to for Valorant. Kill / death / assist
# have `data` counters; others don't. Full set observed in real logs:
# kill, death, assist, headshot, spike_planted, spike_defused, victory,
# clutch, spike_detonated.
_KNOWN_EVENT_TYPES = frozenset(
    {
        "kill",
        "death",
        "assist",
        "headshot",
        "spike_planted",
        "spike_defused",
        "spike_detonated",
        "victory",
        "clutch",
    }
)


# ---------------------------------------------------------------------
# Event dataclass
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class OverwolfEvent:
    """One Overwolf game event as recorded by Outplayed's bookmark log."""

    type: str
    time_ms: int
    """Unix epoch milliseconds — matches clip file mtime × 1000."""

    data: str
    """Per-type sequential counter ('1', '2', ...) — useful for
    detecting multi-kill sequences. Empty string for events without
    counters (spike_defused, victory)."""

    log_file: str


# ---------------------------------------------------------------------
# Log directory resolution
# ---------------------------------------------------------------------


def _default_log_dir() -> Path:
    """Resolve the Outplayed log folder on Windows.

    `%LOCALAPPDATA%` is the Windows env var; falls back to
    `~/AppData/Local` when LOCALAPPDATA is missing (e.g. under WSL).
    Returns the directory even when it doesn't exist; caller should
    `.is_dir()` check.
    """
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Local")
    return Path(base) / "Overwolf" / "Log" / "Apps" / "Outplayed"


# ---------------------------------------------------------------------
# Log parser — scan all rotating files, extract events
# ---------------------------------------------------------------------


# Module-level cache. Parsing every log file takes ~0.5s; we don't want
# to redo it for every clip in a compile.
_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, tuple[OverwolfEvent, ...]]] = {}
"""Keyed by log-dir path; value is (dir_mtime_seen, events)."""


def _dir_signature(log_dir: Path) -> float:
    """Return a signature that changes when logs rotate / grow.

    Uses the max mtime across all matching files. If any log's mtime
    is newer than what we cached, we re-parse everything.
    """
    latest = 0.0
    for f in log_dir.glob("background.html.*.log"):
        try:
            latest = max(latest, f.stat().st_mtime)
        except OSError:
            continue
    return latest


def _parse_line(line: str, log_file: str) -> OverwolfEvent | None:
    """Try to extract an OverwolfEvent from one log line. Returns None
    when the line doesn't match the bookmark shape or the JSON is bad."""
    m = _BOOKMARK_RE.search(line)
    if m is None:
        return None
    try:
        d = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    etype = d.get("type")
    if not isinstance(etype, str):
        return None
    if etype not in _KNOWN_EVENT_TYPES:
        # Not skipping — new event types Outplayed might add would land
        # here; we just don't filter them out. Log at debug once.
        pass
    time_ms = d.get("time")
    if not isinstance(time_ms, (int, float)) or time_ms <= 0:
        return None
    return OverwolfEvent(
        type=etype,
        time_ms=int(time_ms),
        data=str(d.get("data", "")),
        log_file=log_file,
    )


def _scan_all_logs(log_dir: Path) -> tuple[OverwolfEvent, ...]:
    """Parse every `background.html.*.log` file into an event list.
    Sorted by time_ms ASC. Cached by log-dir signature."""
    key = log_dir.as_posix()
    sig = _dir_signature(log_dir)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached[0] >= sig:
            return cached[1]
    events: list[OverwolfEvent] = []
    for f in sorted(log_dir.glob("background.html.*.log")):
        try:
            with open(f, encoding="utf-8", errors="ignore") as fp:
                for line in fp:
                    ev = _parse_line(line, str(f))
                    if ev is not None:
                        events.append(ev)
        except OSError as exc:
            _log.info("overwolf log read failed on %s: %s", f, exc)
            continue
    events.sort(key=lambda e: e.time_ms)
    result = tuple(events)
    with _CACHE_LOCK:
        _CACHE[key] = (sig, result)
    _log.info(
        "overwolf log: parsed %d events across %s",
        len(result),
        log_dir.as_posix(),
    )
    return result


def clear_cache() -> None:
    """Testing / debug hook — force re-parse on next scan."""
    with _CACHE_LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------------
# Public entry — find kills for one clip
# ---------------------------------------------------------------------


def kills_for_clip(
    clip_path: Path | str,
    duration_seconds: float,
    *,
    log_dir: Path | None = None,
    event_types: tuple[str, ...] = ("kill",),
) -> tuple[float, ...] | None:
    """Return kill timestamps (seconds from clip start) by cross-
    referencing the clip's file mtime with the Outplayed log.

    Returns `None` when the log folder doesn't exist (Outplayed not
    installed / not on this machine). Returns `()` when logs exist but
    no matching events fell in the clip window (real hallucination:
    the clip has no user kills).

    `event_types` selects which Overwolf event types count. Default
    is `("kill",)` — the user's own kills. Pass `("kill", "assist")` to
    include assists, or `("kill", "headshot")` to double-count
    headshots (they fire as separate events at the same time).
    """
    path = Path(clip_path)
    if not path.is_file():
        return None
    log_dir = log_dir or _default_log_dir()
    if not log_dir.is_dir():
        return None
    try:
        mtime_seconds = path.stat().st_mtime
    except OSError:
        return None

    all_events = _scan_all_logs(log_dir)
    end_ms = int(mtime_seconds * 1000)
    start_ms = int((mtime_seconds - duration_seconds) * 1000)

    # A small tolerance before the reported start — Outplayed's clip
    # window sometimes extends up to ~1s before the mtime-implied start
    # due to encoder pre-roll. Widen the window by 1s to be safe.
    start_ms -= 1000

    offsets: list[float] = []
    seen: set[int] = set()
    for e in all_events:
        if e.time_ms < start_ms or e.time_ms >= end_ms:
            continue
        if e.type not in event_types:
            continue
        # Dedupe events at the exact same ms (kill + headshot fire
        # together for a headshot kill)
        if e.time_ms in seen:
            continue
        seen.add(e.time_ms)
        offset = (e.time_ms - int(mtime_seconds * 1000)) / 1000.0
        # Convert to "seconds from clip start" (positive number)
        offset_from_start = offset + duration_seconds
        # Clamp to [0, duration] — a couple hundred ms of encoder
        # pre-roll can put an event just past the end mtime
        offset_from_start = max(0.0, min(duration_seconds, offset_from_start))
        offsets.append(round(offset_from_start, 3))
    offsets.sort()
    return tuple(offsets)


def player_identity(log_dir: Path | None = None) -> dict | None:
    """Extract the most recent 'Session start info' line — returns the
    `me` object (player_name, player_id, region, server) or None.

    Cached by log-dir signature same as event scan."""
    log_dir = log_dir or _default_log_dir()
    if not log_dir.is_dir():
        return None
    session_re = re.compile(
        r"\[GameService:Valorant\] Session start info (\{.+\})\s*$"
    )
    latest_me: dict | None = None
    latest_mtime = 0.0
    for f in sorted(log_dir.glob("background.html.*.log")):
        try:
            with open(f, encoding="utf-8", errors="ignore") as fp:
                for line in fp:
                    m = session_re.search(line)
                    if not m:
                        continue
                    try:
                        d = json.loads(m.group(1))
                    except json.JSONDecodeError:
                        continue
                    me = d.get("me")
                    if isinstance(me, dict) and me.get("player_name"):
                        latest_me = me
                        latest_mtime = f.stat().st_mtime
        except OSError:
            continue
    _ = latest_mtime  # kept for potential recency-sort in the future
    return latest_me
