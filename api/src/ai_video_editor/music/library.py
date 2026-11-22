"""Song library — a small catalog of every analysed song, plus the licensed
music folders.

Licensed folders: `WORKSPACE/music/royalty_free/` (safe to publish anywhere)
and `WORKSPACE/music/owner_recs/` (label releases: local only, never Drive).
Each holds a `licence.json`: `{"<filename>": {title, source_url, licence, note}}`.
`list_tracks()` returns only files with a licence entry.

`WORKSPACE/media_library/music/library.json` holds one row per song:
`{song_id, title, source_url, duration_s, analysis_path, tags[]}`. The
analysis itself lives in the content-hash sidecar; the row just points
at it, so the catalog stays cheap to scan and rank.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..config import settings
from .analysis import SongAnalysis, analysis_path, load_or_analyze

_log = logging.getLogger(__name__)

Row = dict

Folder = Literal["royalty_free", "owner_recs"]
FOLDERS: tuple[Folder, ...] = ("royalty_free", "owner_recs")
LICENCE_FILE = "licence.json"
_AUDIO_SUFFIXES = {".mp3", ".m4a", ".opus", ".flac", ".wav", ".ogg", ".aac"}


@dataclass(frozen=True)
class Track:
    path: Path
    title: str
    source_url: str | None
    licence: str
    folder: Folder


def music_root() -> Path:
    return settings.workspace_dir / "music"


def read_licences(folder: Folder) -> dict[str, dict]:
    f = music_root() / folder / LICENCE_FILE
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _log.warning("%s is unreadable or not valid JSON; treating as empty", f)
        return {}
    return data if isinstance(data, dict) else {}


def record_licence(
    path: Path, folder: Folder, *, title: str, source_url: str | None, licence: str, note: str = ""
) -> None:
    """Add/replace `path`'s entry in its folder's licence.json."""
    entries = read_licences(folder)
    entries[path.name] = {
        "title": title,
        "source_url": source_url,
        "licence": licence,
        "note": note,
    }
    f = music_root() / folder / LICENCE_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def list_tracks(folder: str | None = None) -> list[Track]:
    """Licensed tracks from one folder (or both). Unlicensed files are skipped."""
    if folder is not None and folder not in FOLDERS:
        raise ValueError(f"unknown music folder {folder!r}; expected one of {list(FOLDERS)}")
    tracks: list[Track] = []
    for fld in FOLDERS if folder is None else (folder,):
        d = music_root() / fld
        if not d.is_dir():
            continue
        licences = read_licences(fld)
        for p in sorted(d.iterdir()):
            if p.suffix.lower() not in _AUDIO_SUFFIXES:
                continue
            entry = licences.get(p.name)
            if not isinstance(entry, dict) or not entry.get("licence"):
                _log.warning("music %s has no licence entry in %s; skipped", p.name, fld)
                continue
            tracks.append(
                Track(
                    path=p,
                    title=str(entry.get("title") or p.stem),
                    source_url=entry.get("source_url"),
                    licence=str(entry["licence"]),
                    folder=fld,
                )
            )
    return tracks


def music_folder_of(path: str | Path) -> Folder | None:
    """Which licensed folder `path` lives in. A legacy flat file directly in
    `WORKSPACE/music/` counts as royalty_free; anything else is None."""
    p = Path(path)
    try:
        rel = p.resolve().relative_to(music_root().resolve())
    except ValueError:
        return None
    if len(rel.parts) == 1:
        return "royalty_free"
    return rel.parts[0] if rel.parts[0] in FOLDERS else None  # type: ignore[return-value]


def _default_library_path() -> Path:
    return settings.workspace_dir / "media_library" / "music" / "library.json"


class Library:
    """Read/write catalog. `analyze` is injectable so tests skip audio decode."""

    def __init__(
        self,
        path: Path | None = None,
        analyze: Callable[[Path], SongAnalysis] = load_or_analyze,
    ) -> None:
        self._path = path or _default_library_path()
        self._analyze = analyze

    def _rows(self) -> list[Row]:
        if not self._path.is_file():
            return []
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _save(self, rows: list[Row]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    def add(self, path: Path, title: str, source_url: str, tags: list[str]) -> Row:
        """Analyse `path` (cached) and record it. Re-adding the same bytes
        updates the existing row rather than duplicating it."""
        analysis = self._analyze(path)
        sidecar = str(analysis_path(path))
        rows = self._rows()
        existing = next((r for r in rows if r["analysis_path"] == sidecar), None)
        row = {
            "song_id": existing["song_id"] if existing else str(uuid.uuid4()),
            "title": title,
            "source_url": source_url,
            "duration_s": analysis.structure.falling[1],
            "analysis_path": sidecar,
            "audio_path": str(path),
            "tags": list(tags),
        }
        self._save([r for r in rows if r is not existing] + [row])
        return row

    def get(self, song_id: str) -> Row | None:
        return next((r for r in self._rows() if r["song_id"] == song_id), None)

    def suggest(self, tags: list[str], duration_s: float) -> list[Row]:
        """All songs, best first: most shared tags, then closest duration."""
        wanted = set(tags)

        def rank(row: Row) -> tuple[int, float]:
            overlap = len(wanted & set(row["tags"]))
            return (-overlap, abs(row["duration_s"] - duration_s))

        return sorted(self._rows(), key=rank)
