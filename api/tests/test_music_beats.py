"""Beat detection tests. Mocks librosa so the suite stays fast + hermetic."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_video_editor.music import beats as beats_mod
from ai_video_editor.music.beats import BeatTrack, detect_beats


def test_beat_track_period() -> None:
    """`beat_period_seconds` is 60/bpm."""
    track = BeatTrack(
        mp3_path=Path("/tmp/x.mp3"),
        tempo_bpm=120.0,
        beats=(0.0, 0.5, 1.0),
        detector="librosa",
    )
    assert abs(track.beat_period_seconds - 0.5) < 1e-6


def test_detect_beats_raises_on_missing_file() -> None:
    with pytest.raises(FileNotFoundError):
        detect_beats("/nonexistent.mp3")


def test_detect_beats_unknown_detector_raises(tmp_path) -> None:
    p = tmp_path / "x.mp3"
    p.write_bytes(b"fake")
    with pytest.raises(ValueError, match="unknown beat detector"):
        detect_beats(p, detector="nonexistent")


def test_detect_beats_uses_registered_strategy(tmp_path, monkeypatch) -> None:
    """The registered detector is called with the mp3 path and its
    result is packed into a BeatTrack."""
    p = tmp_path / "song.mp3"
    p.write_bytes(b"fake")

    def _fake_detector(mp3_path: Path) -> tuple[float, list[float]]:
        assert mp3_path == p
        return (140.0, [0.0, 0.42857, 0.85714, 1.28571])

    monkeypatch.setitem(beats_mod._BEAT_DETECTORS, "librosa", _fake_detector)
    track = detect_beats(p, use_cache=False)
    assert track.tempo_bpm == 140.0
    assert len(track.beats) == 4
    assert track.detector == "librosa"


def test_detect_beats_caches_result(tmp_path, monkeypatch) -> None:
    """After first detection the sidecar JSON exists and a second call
    returns identical data without re-running the detector."""
    p = tmp_path / "song.mp3"
    p.write_bytes(b"fake")

    calls = {"n": 0}

    def _once(mp3_path: Path) -> tuple[float, list[float]]:
        calls["n"] += 1
        return (100.0, [0.6, 1.2])

    monkeypatch.setitem(beats_mod._BEAT_DETECTORS, "librosa", _once)
    detect_beats(p)
    detect_beats(p)
    assert calls["n"] == 1  # 2nd call hit the cache
    assert (tmp_path / "song.librosa.beats.json").is_file()


def test_detect_beats_use_cache_false_bypasses(tmp_path, monkeypatch) -> None:
    p = tmp_path / "song.mp3"
    p.write_bytes(b"fake")

    calls = {"n": 0}

    def _each_time(mp3_path: Path) -> tuple[float, list[float]]:
        calls["n"] += 1
        return (100.0, [0.6])

    monkeypatch.setitem(beats_mod._BEAT_DETECTORS, "librosa", _each_time)
    detect_beats(p, use_cache=False)
    detect_beats(p, use_cache=False)
    assert calls["n"] == 2
