"""Beat detection — mp3 path → tempo + list of beat timestamps.

Open/closed: `_BEAT_DETECTORS` maps a detector name to a function.
Adding aubio, madmom, or a custom detector = new function + one dict
entry.

Result cached to disk (`<mp3>.beats.json`) so a re-run doesn't pay the
librosa cost twice.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class BeatTrack:
    """Result of beat detection for one music file."""

    mp3_path: Path
    tempo_bpm: float
    beats: tuple[float, ...]
    """Beat onset times in seconds from the start of the file."""

    detector: str
    """Which detector produced this — for reproducibility."""

    extras: dict = field(default_factory=dict)

    @property
    def beat_period_seconds(self) -> float:
        """60 / bpm — the expected gap between beats."""
        return 60.0 / self.tempo_bpm if self.tempo_bpm > 0 else 0.0


# ---------------------------------------------------------------------
# Detectors (open/closed hook — register new implementations here)
# ---------------------------------------------------------------------


def _detect_librosa(mp3_path: Path) -> tuple[float, list[float]]:
    """librosa beat_track — returns (tempo_bpm, beat_times_seconds).

    librosa is imported lazily so unit tests can mock this whole
    function without paying its ~1s import cost.
    """
    import librosa

    y, sr = librosa.load(str(mp3_path), sr=None, mono=True)
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units="frames")
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    # librosa may return numpy scalar for tempo — coerce to plain float
    return float(tempo), [float(t) for t in beat_times]


_BEAT_DETECTORS: dict[str, Callable[[Path], tuple[float, list[float]]]] = {
    "librosa": _detect_librosa,
}


# ---------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------


def _cache_path(mp3_path: Path, detector: str) -> Path:
    """Sidecar `<mp3>.<detector>.beats.json` — deterministic path."""
    return mp3_path.with_suffix(f".{detector}.beats.json")


def _load_cached(cache: Path) -> BeatTrack | None:
    if not cache.is_file():
        return None
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return BeatTrack(
        mp3_path=Path(data["mp3_path"]),
        tempo_bpm=float(data["tempo_bpm"]),
        beats=tuple(float(b) for b in data["beats"]),
        detector=str(data.get("detector", "unknown")),
        extras=data.get("extras", {}),
    )


def _save_cached(cache: Path, track: BeatTrack) -> None:
    payload = {
        "mp3_path": str(track.mp3_path),
        "tempo_bpm": track.tempo_bpm,
        "beats": list(track.beats),
        "detector": track.detector,
        "extras": track.extras,
    }
    try:
        cache.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        _log.info("beat cache write failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------


def read_legacy_beats(mp3_path: Path, detector: str = "librosa") -> BeatTrack | None:
    """Read the old path-keyed `<mp3>.<detector>.beats.json`, if any.

    Only for migrating into the content-hash analysis sidecar; new code
    never writes this file.
    """
    return _load_cached(_cache_path(mp3_path, detector))


def detect_beats(
    mp3_path: Path | str,
    *,
    detector: str = "librosa",
    use_cache: bool = True,
) -> BeatTrack:
    """Detect beats in a music file and return them as a `BeatTrack`.

    Uses a sidecar JSON cache next to the mp3 so re-runs skip the
    ~500ms-1s librosa cost. Pass `use_cache=False` to force redetection.

    Raises `FileNotFoundError` if the mp3 doesn't exist, `ValueError`
    if the detector name isn't registered.
    """
    path = Path(mp3_path)
    if not path.is_file():
        raise FileNotFoundError(f"music file not found: {path}")
    if detector not in _BEAT_DETECTORS:
        raise ValueError(
            f"unknown beat detector {detector!r}; "
            f"expected one of {sorted(_BEAT_DETECTORS)}"
        )

    cache = _cache_path(path, detector)
    if use_cache:
        cached = _load_cached(cache)
        if cached is not None:
            return cached

    tempo, beats = _BEAT_DETECTORS[detector](path)
    track = BeatTrack(
        mp3_path=path,
        tempo_bpm=tempo,
        beats=tuple(beats),
        detector=detector,
    )
    if use_cache:
        _save_cached(cache, track)
    return track
