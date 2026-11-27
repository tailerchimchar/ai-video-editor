"""Videos hub: one summary per narratable video, across every registered kind.

OCP: this loops the target registry and knows no `kind`. Read-only; it never
creates a narration session.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from . import targets
from .models import TargetRef
from .store import store_for

SuggestionStatus = Literal["none", "pending", "ready", "applied"]
_STATUSES = ("pending", "ready", "applied")

_TITLE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}_([^_]+)_(\d+)-(\d+)-(\d+)$")
_DATE_RE = re.compile(r"^(?:joined_)?(\d{4}-\d{2}-\d{2})(?:_|$)")
# story-join keys: joined_<date>_<champ>-<champ>[-...]
_JOINED_RE = re.compile(r"^joined_\d{4}-\d{2}-\d{2}_([a-z0-9-]+)$")

# video path -> (fingerprint, duration_s); ffprobe runs once per file version.
_DURATIONS: dict[str, tuple[str, float | None]] = {}


class VideoSummary(BaseModel):
    kind: str
    key: str
    title: str
    game: str
    date: str | None
    duration_s: float | None
    video_url: str
    take_count: int
    stale: bool
    exported: bool
    suggestion_status: SuggestionStatus


def title_from_key(key: str) -> str:
    """`2026-11-20_2001_jhin_8-2-13` -> "Jhin 8/2/13";
    `joined_2026-11-26_xayah-kaisa` -> "Xayah · Kaisa"; anything else -> the key."""
    j = _JOINED_RE.match(key)
    if j:
        return " · ".join(c.capitalize() for c in j.group(1).split("-"))
    m = _TITLE_RE.match(key)
    if not m:
        return key
    champ, k, d, a = m.groups()
    return f"{champ.capitalize()} {k}/{d}/{a}"


def date_from_key(key: str) -> str | None:
    m = _DATE_RE.match(key)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1)).isoformat()
    except ValueError:
        return None


def suggestion_status(work_dir: Path) -> SuggestionStatus:
    """`status` of `<work_dir>/suggestions.json`; "none" if missing/unreadable."""
    try:
        data = json.loads((work_dir / "suggestions.json").read_text(encoding="utf-8"))
        status = data.get("status") if isinstance(data, dict) else None
    except (OSError, ValueError):
        return "none"
    return status if status in _STATUSES else "none"


def probe_duration(video: Path) -> float | None:
    """Container duration in seconds via ffprobe; None if it cannot be read."""
    try:
        r = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                video.as_posix(),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return float(r.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _duration(video: Path) -> float | None:
    fp = targets.fingerprint(video)
    cached = _DURATIONS.get(str(video))
    if cached and cached[0] == fp:
        return cached[1]
    value = probe_duration(video)
    _DURATIONS[str(video)] = (fp, value)
    return value


def build_summary(target: targets.NarrationTarget, key: str) -> VideoSummary:
    from .service import is_stale

    video = target.video_path(key)
    ref = TargetRef(kind=target.kind, key=key)
    store = store_for(target)
    session = store.load(ref)
    return VideoSummary(
        kind=target.kind,
        key=key,
        title=title_from_key(key),
        game=target.game(key),
        date=date_from_key(key),
        duration_s=_duration(video),
        video_url=target.video_url(key),
        take_count=len(session.takes) if session else 0,
        stale=bool(session and is_stale(ref, target, session)),
        exported=store.export_path(ref).is_file(),
        suggestion_status=suggestion_status(target.work_dir(key)),
    )


def list_videos() -> list[VideoSummary]:
    """Every video of every registered kind (registry order, newest first within a kind)."""
    out: list[VideoSummary] = []
    for target in targets.registered_targets():
        for key in target.list_keys():
            try:
                out.append(build_summary(target, key))
            except targets.UnknownTargetError:
                continue  # video vanished between list and build
    return out
