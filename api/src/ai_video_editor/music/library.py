"""Song library — a small catalog of every analysed song.

`WORKSPACE/media_library/music/library.json` holds one row per song:
`{song_id, title, source_url, duration_s, analysis_path, tags[]}`. The
analysis itself lives in the content-hash sidecar; the row just points
at it, so the catalog stays cheap to scan and rank.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path

from ..config import settings
from .analysis import SongAnalysis, analysis_path, load_or_analyze

Row = dict


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
