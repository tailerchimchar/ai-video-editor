"""Overwolf Game-Events log parser — ground-truth kill/death/assist
timestamps from Outplayed's own log files.

Outplayed is an Overwolf app; it subscribes to Valorant's game-event
provider (`gep_valorant.dll`) and logs every event to
`%LOCALAPPDATA%\\Overwolf\\Log\\Apps\\Outplayed\\background.html*.log`.

We parse those logs directly. Every kill / death / assist / headshot
that fires in-game shows up as a `[StreamingCaptureService] Already
capturing, adding bookmark {...}` line with a Unix ms timestamp
(the `time` field). Correlation to Outplayed clip files is trivial:
the clip's file mtime (Unix seconds x 1000) IS the wall-clock end of
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
- Log files rotate — read ALL `background.html*.log`, not just latest
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PureWindowsPath

_log = logging.getLogger(__name__)


# Line shape we care about (wrapped for readability):
#   2026-06-30 22:52:28,917 (INFO) </background.js> (:42228) -
#   [StreamingCaptureService] Already capturing, adding bookmark
#   {"type":"kill","time":1782877948916,
#    "timing":{"past":15000,"future":5000},"data":"1"}
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
    """Unix epoch milliseconds — matches clip file mtime x 1000."""

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
    for f in log_dir.glob("background.html*.log"):
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
    """Parse every `background.html*.log` file into an event list.
    Sorted by time_ms ASC. Cached by log-dir signature."""
    key = log_dir.as_posix()
    sig = _dir_signature(log_dir)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached[0] >= sig:
            return cached[1]
    events: list[OverwolfEvent] = []
    for f in sorted(log_dir.glob("background.html*.log")):
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


# ---------------------------------------------------------------------
# Dispatch table — Outplayed writes one log entry per video file created,
# with the FILE PATH + a full list of in-video event offsets. This is
# ground-truth candidate data for the analyze_asset pipeline.
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchedVideo:
    """One video Outplayed wrote to disk, plus its embedded events.

    `start_time_ms` and `duration_ms` describe the video's wall-clock
    window (start = when recording began, end = start + duration).
    Each entry in `events` has:
      - type: kill / death / assist / headshot / spike_defused / …
      - time_ms_in_video: offset from the video's start (already in
        the video's own timeline — no clock math needed)
      - data: sequential counter
    """

    path: Path
    output_type: str
    """`fullMatch` for per-match recordings, `fullSession` for
    session-long, `event` for auto-clip highlights."""

    start_time_ms: int
    duration_ms: int
    events: tuple[dict, ...]
    """Each dict: {type, time_ms_in_video, data}"""


_DISPATCH_RE = re.compile(r"Dispatch new video created (\{.+\})\s*$")


def _load_dispatched(log_dir: Path) -> dict[str, DispatchedVideo]:
    """Parse all `Dispatch new video created` lines from every log
    file. Returns a dict keyed by normalized file path (lowercased,
    posix-style so Windows path variations don't miss)."""
    out: dict[str, DispatchedVideo] = {}
    for f in sorted(log_dir.glob("background.html*.log")):
        try:
            with open(f, encoding="utf-8", errors="ignore") as fp:
                for line in fp:
                    m = _DISPATCH_RE.search(line)
                    if m is None:
                        continue
                    try:
                        d = json.loads(m.group(1))
                    except json.JSONDecodeError:
                        continue
                    raw_path = d.get("path")
                    if not raw_path:
                        continue
                    events = []
                    for e in d.get("events", []) or []:
                        etype = e.get("type")
                        etime = e.get("time")
                        if not isinstance(etype, str) or not isinstance(etime, (int, float)):
                            continue
                        events.append(
                            {
                                "type": etype,
                                "time_ms_in_video": int(etime),
                                "data": str(e.get("data", "")),
                            }
                        )
                    key = str(Path(raw_path)).lower().replace("\\", "/")
                    out[key] = DispatchedVideo(
                        path=Path(raw_path),
                        # `type` in the JSON is the OUTPUT type
                        # (fullMatch / fullSession / event) — clashes
                        # with the `format` field so we alias to
                        # `output_type` on our dataclass.
                        output_type=str(d.get("type", "unknown")),
                        start_time_ms=int(d.get("startTime", 0)),
                        duration_ms=int(d.get("duration", 0)),
                        events=tuple(events),
                    )
        except OSError as exc:
            _log.info("overwolf log read failed on %s: %s", f, exc)
            continue
    return out


# Separate cache from the flat event cache — dispatch entries are a
# different shape and it's worth iterating them once.
_DISPATCH_CACHE_LOCK = threading.Lock()
_DISPATCH_CACHE: dict[str, tuple[float, dict[str, DispatchedVideo]]] = {}


def _get_dispatched_cached(log_dir: Path) -> dict[str, DispatchedVideo]:
    key = log_dir.as_posix()
    sig = _dir_signature(log_dir)
    with _DISPATCH_CACHE_LOCK:
        cached = _DISPATCH_CACHE.get(key)
        if cached is not None and cached[0] >= sig:
            return cached[1]
    parsed = _load_dispatched(log_dir)
    with _DISPATCH_CACHE_LOCK:
        _DISPATCH_CACHE[key] = (sig, parsed)
    _log.info(
        "overwolf log: indexed %d dispatched video(s) in %s",
        len(parsed),
        log_dir.as_posix(),
    )
    return parsed


def dispatched_video_for(
    clip_path: Path | str,
    log_dir: Path | None = None,
) -> DispatchedVideo | None:
    """Look up a video by its file path in the log's dispatch table.

    Returns the `DispatchedVideo` if the log recorded a "Dispatch new
    video created" entry for that exact path. Otherwise None (typical
    for user-manually-clipped shorts, which Outplayed doesn't dispatch).

    Path comparison is case-insensitive + separator-normalized so
    Windows / vs \\ variations don't miss.
    """
    log_dir = log_dir or _default_log_dir()
    if not log_dir.is_dir():
        return None
    dispatched = _get_dispatched_cached(log_dir)
    key = str(Path(clip_path)).lower().replace("\\", "/")
    return dispatched.get(key)


def events_for_video(
    clip_path: Path | str,
    *,
    log_dir: Path | None = None,
    event_types: tuple[str, ...] | None = None,
) -> tuple[dict, ...] | None:
    """Return the events for `clip_path` from the log's dispatch table.

    Each dict: `{type, time_ms_in_video, data}`. Times are OFFSETS from
    the video's start, in ms — feed to
    `metadata.anchor_seconds = time_ms_in_video / 1000`.

    Returns `None` when the video isn't in the dispatch table (typical
    for user-clipped shorts). Filter by `event_types` (default: all).
    """
    dv = dispatched_video_for(clip_path, log_dir=log_dir)
    if dv is None:
        return None
    if event_types is None:
        return dv.events
    keep = frozenset(event_types)
    return tuple(e for e in dv.events if e["type"] in keep)


def game_for_video(clip_path: Path | str, *, log_dir: Path | None = None) -> str | None:
    """Game name Outplayed filed `clip_path` under, e.g. "League of
    Legends" (the folder after `Outplayed` in the dispatched path), or
    None when the video isn't dispatched or the layout is unfamiliar."""
    dv = dispatched_video_for(clip_path, log_dir=log_dir)
    if dv is None:
        return None
    parts = PureWindowsPath(str(dv.path)).parts
    lowered = [p.lower() for p in parts]
    if "outplayed" not in lowered:
        return None
    idx = lowered.index("outplayed") + 1
    return parts[idx] if idx < len(parts) - 1 else None


def clear_dispatch_cache() -> None:
    """Testing / debug hook — force re-parse on next lookup."""
    with _DISPATCH_CACHE_LOCK:
        _DISPATCH_CACHE.clear()


_VALORANT_SESSION_RE = re.compile(r"\[GameService:Valorant\] Session start info (\{.+\})\s*$")
# League's game-info lines embed identity as a flat JSON object that
# starts with the summoner `name`. A sibling `summoner_info` carrying a
# nested `player_info` string has no top-level `name` and is skipped.
_LEAGUE_SUMMONER_RE = re.compile(r'"summoner_info":(\{"name":"[^"]*"[^{}]*\})')


def _valorant_identity(line: str) -> dict | None:
    m = _VALORANT_SESSION_RE.search(line)
    if not m:
        return None
    try:
        me = json.loads(m.group(1)).get("me")
    except json.JSONDecodeError:
        return None
    if isinstance(me, dict) and me.get("player_name"):
        return me
    return None


def _league_identity(line: str) -> dict | None:
    m = _LEAGUE_SUMMONER_RE.search(line)
    if not m:
        return None
    try:
        info = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return {
        "player_name": info["name"],
        "champion": info.get("champion") or None,
        "game": "league",
    }


# Canonical game key (profile `meta.game`) -> parser for one log line.
# Add a game by adding an entry; unknown games fall back to Valorant.
_IDENTITY_PARSERS: dict[str, Callable[[str], dict | None]] = {
    "valorant": _valorant_identity,
    "league": _league_identity,
}


_LINE_TIME_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3})")


def _line_time_ms(line: str) -> int | None:
    """Wall-clock ms (local time, as Overwolf logs it) of a log line."""
    m = _LINE_TIME_RE.match(line)
    if m is None:
        return None
    stamp = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    return int(stamp.timestamp() * 1000) + int(m.group(2))


def player_identity(
    log_dir: Path | None = None,
    game: str | None = None,
    at_ms: int | None = None,
) -> dict | None:
    """Player identity the Outplayed logs record for `game`
    (Valorant when unset or unrecognised), or None.

    Several games a night can use different champions, so pass `at_ms`
    (Unix ms, e.g. the END of the video: a session's identity line is
    logged just after its recording starts) to get the latest identity
    logged at or before it. Without `at_ms`, or when nothing precedes
    it, the newest identity in the logs wins.

    Valorant: the `me` object (player_name, player_id, region, server).
    League: `{player_name, champion, game}`. The League killfeed shows
    the champion, so kill verification needs it, not the summoner name.
    """
    from .profiles import load_profile  # local import: avoids a cycle

    log_dir = log_dir or _default_log_dir()
    if not log_dir.is_dir():
        return None
    parse = _IDENTITY_PARSERS.get(load_profile(game).meta.game, _valorant_identity)
    newest: dict | None = None
    newest_before: dict | None = None
    for f in sorted(log_dir.glob("background.html*.log")):
        try:
            with open(f, encoding="utf-8", errors="ignore") as fp:
                for line in fp:
                    identity = parse(line)
                    if identity is None:
                        continue
                    newest = identity
                    logged_ms = _line_time_ms(line)
                    if at_ms is not None and logged_ms is not None and logged_ms <= at_ms:
                        newest_before = identity
        except OSError:
            continue
    return newest_before or newest
