"""Song analysis cache — beats + structure keyed by the file's bytes.

Sidecar: `<music cache dir>/<sha256-of-bytes>.<STRUCTURE_ANALYZER_VERSION>
.analysis.json`. Keying on content (not path) means a replaced or trimmed
file never reads a stale analysis, and the same song under two names
analyses once.

A sidecar whose structure is `source == "manual"` is a human correction:
it is returned as-is even when the analyzer version has moved on.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ..config import settings
from .beats import BeatTrack, detect_beats, read_legacy_beats
from .structure import (
    STRUCTURE_ANALYZER_VERSION,
    SongStructure,
    detect_structure,
)

_log = logging.getLogger(__name__)

_CHUNK_BYTES = 1 << 20


@dataclass(frozen=True)
class SongAnalysis:
    beats: BeatTrack
    structure: SongStructure
    file_sha256: str


# ---------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------


def _music_cache_dir() -> Path:
    return settings.workspace_dir / "music"


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _sidecar(file_sha256: str, version: str = STRUCTURE_ANALYZER_VERSION) -> Path:
    return _music_cache_dir() / f"{file_sha256}.{version}.analysis.json"


def analysis_path(path: Path) -> Path:
    """Sidecar path for the current bytes of `path` at the current version."""
    return _sidecar(_sha256_of(path))


# ---------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------


def write_analysis(sidecar: Path, analysis: SongAnalysis) -> None:
    """Persist `analysis`. A failed write is logged, never fatal."""
    payload = {
        "file_sha256": analysis.file_sha256,
        "beats": {
            "tempo_bpm": analysis.beats.tempo_bpm,
            "beats": list(analysis.beats.beats),
            "detector": analysis.beats.detector,
            "extras": analysis.beats.extras,
        },
        "structure": dataclasses.asdict(analysis.structure),
    }
    try:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        _log.info("analysis sidecar write failed (non-fatal): %s", exc)


def _read_analysis(sidecar: Path, song: Path) -> SongAnalysis | None:
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        beats = data["beats"]
        s = data["structure"]
        return SongAnalysis(
            beats=BeatTrack(
                mp3_path=song,
                tempo_bpm=float(beats["tempo_bpm"]),
                beats=tuple(float(b) for b in beats["beats"]),
                detector=str(beats["detector"]),
                extras=beats.get("extras", {}),
            ),
            structure=SongStructure(
                rising=(float(s["rising"][0]), float(s["rising"][1])),
                climax=(float(s["climax"][0]), float(s["climax"][1])),
                falling=(float(s["falling"][0]), float(s["falling"][1])),
                drop_s=float(s["drop_s"]),
                source=s["source"],
                analyzer_version=str(s["analyzer_version"]),
            ),
            file_sha256=str(data["file_sha256"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


# ---------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------


def _manual_sidecar(file_sha256: str, song: Path) -> SongAnalysis | None:
    """A human-corrected analysis written under any analyzer version."""
    for sidecar in sorted(_music_cache_dir().glob(f"{file_sha256}.*.analysis.json")):
        found = _read_analysis(sidecar, song)
        if found is not None and found.structure.source == "manual":
            return found
    return None


def _analyze(song: Path, file_sha256: str) -> SongAnalysis:
    beats = read_legacy_beats(song) or detect_beats(song, use_cache=False)
    return SongAnalysis(
        beats=beats,
        structure=detect_structure(song),
        file_sha256=file_sha256,
    )


def load_or_analyze(path: Path) -> SongAnalysis:
    """Return the analysis for `path`'s bytes, computing it once.

    Order: current-version sidecar, then any manual sidecar, then a fresh
    analysis (which adopts an old path-keyed `.beats.json` if one exists).
    """
    path = Path(path)
    file_sha256 = _sha256_of(path)
    sidecar = _sidecar(file_sha256)

    cached = _read_analysis(sidecar, path) if sidecar.is_file() else None
    if cached is not None:
        return cached
    manual = _manual_sidecar(file_sha256, path)
    if manual is not None:
        return manual

    analysis = _analyze(path, file_sha256)
    write_analysis(sidecar, analysis)
    return analysis


def _clip_span(span: tuple[float, float], start: float, length: float):
    def shift(t: float) -> float:
        return min(max(t - start, 0.0), length)

    return (shift(span[0]), shift(span[1]))


def offset_analysis(analysis: SongAnalysis, trim_start_s: float, trim_end_s: float) -> SongAnalysis:
    """Re-express `analysis` for a copy trimmed to [trim_start_s, trim_end_s].

    Pure arithmetic, so trimmed files reuse the untrimmed analysis with no
    decoding. Beats outside the trim are dropped; spans and the drop are
    clamped into the trimmed length.
    """
    length = trim_end_s - trim_start_s
    s = analysis.structure
    beats = tuple(b - trim_start_s for b in analysis.beats.beats if trim_start_s <= b <= trim_end_s)
    structure = dataclasses.replace(
        s,
        rising=_clip_span(s.rising, trim_start_s, length),
        climax=_clip_span(s.climax, trim_start_s, length),
        falling=_clip_span(s.falling, trim_start_s, length),
        drop_s=min(max(s.drop_s - trim_start_s, 0.0), length),
    )
    return dataclasses.replace(
        analysis,
        beats=dataclasses.replace(analysis.beats, beats=beats),
        structure=structure,
    )
