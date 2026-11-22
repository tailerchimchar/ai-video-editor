"""Music download — URL → local mp3 in `WORKSPACE/music/<folder>/`.

`folder` is `royalty_free` (default) or `owner_recs` (label releases that
must never reach Drive). A flat file left in `WORKSPACE/music/` from before
the folders existed is still reused, as royalty_free, with a warning.

Open/closed: `_DOWNLOAD_STRATEGIES` maps a source name to a downloader
function. Adding SoundCloud, Bandcamp, local-upload, etc. = new
function + one dict entry.

Cache key: sha1(url)[:12] — same URL always resolves to the same file,
so re-invocation is idempotent and free.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from pathlib import Path

from ..config import settings

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Source detection — dispatch by URL pattern
# ---------------------------------------------------------------------


_YOUTUBE_HOST_PATTERN = re.compile(
    r"^(https?://)?(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)/",
    re.IGNORECASE,
)


def _detect_source(url: str) -> str:
    """Map a URL to a strategy key. Extend by adding more matchers."""
    if _YOUTUBE_HOST_PATTERN.search(url):
        return "youtube"
    # Local paths (already a file) bypass download entirely.
    if Path(url).exists():
        return "local_path"
    raise ValueError(f"no download strategy matches url={url!r}")


# ---------------------------------------------------------------------
# Strategies (open/closed hook — register new sources here)
# ---------------------------------------------------------------------


def _download_youtube(url: str, out_path: Path) -> Path:
    """yt-dlp wrapper. Extracts best audio, transcodes to mp3.

    Imports yt_dlp lazily so tests that mock this module don't pay the
    import cost.
    """
    import yt_dlp

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # yt-dlp appends the extension itself; strip our .mp3 and let its
    # postprocessor add it back cleanly.
    template = str(out_path.with_suffix(""))
    opts = {
        "format": "bestaudio/best",
        "outtmpl": f"{template}.%(ext)s",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    return out_path


def _download_local(url: str, out_path: Path) -> Path:
    """URL is already a local file path — just return it unchanged."""
    return Path(url)


_DOWNLOAD_STRATEGIES: dict[str, Callable[[str, Path], Path]] = {
    "youtube": _download_youtube,
    "local_path": _download_local,
}


# ---------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------


def _music_dir(folder: str = "royalty_free") -> Path:
    """`WORKSPACE/music/<folder>/` — created lazily."""
    from .library import FOLDERS

    if folder not in FOLDERS:
        raise ValueError(f"unknown music folder {folder!r}; expected one of {list(FOLDERS)}")
    d = settings.workspace_dir / "music" / folder
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_key(url: str) -> str:
    """Deterministic 12-char hash of the URL — filename-safe cache key."""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def download_music(
    url: str,
    *,
    name: str | None = None,
    folder: str = "royalty_free",
    licence: str | None = None,
) -> Path:
    """Download `url` (YouTube or local path) to `WORKSPACE/music/<folder>/`.

    `licence`, when given, is recorded in the folder's licence.json (a track
    without an entry is invisible to `list_tracks`).

    Returns the local mp3 path. Cached: repeat calls with the same URL
    return the existing file without re-downloading.

    `name` optionally sets the filename stem (e.g. `name="beat_drop"` →
    `beat_drop.mp3`); when omitted, a sha1 hash of the URL is used so
    each URL has a stable, deterministic path.

    Raises `ValueError` if no strategy matches the URL pattern.
    """
    if folder == "owner_recs" and not licence:
        raise ValueError("downloading into owner_recs requires a licence entry")
    stem = name or _cache_key(url)
    out_path = _music_dir(folder) / f"{stem}.mp3"
    # Local-path shortcut — no cache to check.
    source = _detect_source(url)
    if source == "local_path":
        return _DOWNLOAD_STRATEGIES[source](url, out_path)
    # Cache hit.
    if out_path.is_file() and out_path.stat().st_size > 0:
        _log.debug("music cache hit: %s", out_path)
        return out_path
    legacy = settings.workspace_dir / "music" / f"{stem}.mp3"
    if folder == "royalty_free" and legacy.is_file() and legacy.stat().st_size > 0:
        _log.warning(
            "music %s is a legacy flat file with no licence entry; treating as royalty_free",
            legacy,
        )
        return legacy
    strategy = _DOWNLOAD_STRATEGIES.get(source)
    if strategy is None:
        raise ValueError(f"no download strategy for source={source!r}")
    result = strategy(url, out_path)
    if licence:
        from .library import record_licence

        record_licence(
            result,
            folder,  # type: ignore[arg-type]
            title=stem,
            source_url=url,
            licence=licence,
        )
    return result
