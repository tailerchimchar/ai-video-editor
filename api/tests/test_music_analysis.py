"""Content-hash analysis sidecar: caching, migration, manual override, trim."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from ai_video_editor.config import settings
from ai_video_editor.music import analysis as analysis_mod
from ai_video_editor.music import beats as beats_mod
from ai_video_editor.music import structure as structure_mod
from ai_video_editor.music.analysis import (
    SongAnalysis,
    analysis_path,
    load_or_analyze,
    offset_analysis,
)
from ai_video_editor.music.beats import BeatTrack
from ai_video_editor.music.structure import SongStructure


def auto_structure(version: str = structure_mod.STRUCTURE_ANALYZER_VERSION):
    return SongStructure(
        rising=(0.0, 4.0),
        climax=(4.0, 10.0),
        falling=(10.0, 16.0),
        drop_s=4.5,
        source="auto",
        analyzer_version=version,
    )


@pytest.fixture
def detectors(tmp_path, monkeypatch):
    """Isolated workspace plus counting fake beat/structure detectors."""
    monkeypatch.setattr(settings, "workspace_dir", tmp_path / "ws")
    calls = {"beats": 0, "structure": 0}

    def fake_beats(mp3_path: Path) -> tuple[float, list[float]]:
        calls["beats"] += 1
        return (120.0, [0.5, 1.0, 1.5])

    def fake_structure(path: Path) -> SongStructure:
        calls["structure"] += 1
        return auto_structure()

    monkeypatch.setitem(beats_mod._BEAT_DETECTORS, "librosa", fake_beats)
    monkeypatch.setattr(analysis_mod, "detect_structure", fake_structure)
    return calls


def make_song(tmp_path, content: bytes = b"song-bytes") -> Path:
    song = tmp_path / "song.mp3"
    song.write_bytes(content)
    return song


def test_analysis_path_is_content_hash_plus_version(tmp_path, detectors) -> None:
    song = make_song(tmp_path)
    digest = hashlib.sha256(b"song-bytes").hexdigest()
    name = f"{digest}.{structure_mod.STRUCTURE_ANALYZER_VERSION}.analysis.json"
    assert analysis_path(song) == settings.workspace_dir / "music" / name


def test_load_or_analyze_writes_one_sidecar_and_reuses_it(tmp_path, detectors) -> None:
    song = make_song(tmp_path)
    first = load_or_analyze(song)
    second = load_or_analyze(song)
    assert first == second
    assert first.beats.beats == (0.5, 1.0, 1.5)
    assert first.structure == auto_structure()
    assert detectors == {"beats": 1, "structure": 1}
    assert analysis_path(song).is_file()


def test_new_bytes_produce_a_new_sidecar(tmp_path, detectors) -> None:
    song = make_song(tmp_path)
    load_or_analyze(song)
    old_sidecar = analysis_path(song)
    song.write_bytes(b"trimmed-bytes")
    load_or_analyze(song)
    assert analysis_path(song) != old_sidecar
    assert old_sidecar.is_file() and analysis_path(song).is_file()
    assert detectors["beats"] == 2


def test_legacy_beats_json_is_migrated_once_then_ignored(tmp_path, detectors, monkeypatch) -> None:
    song = make_song(tmp_path)
    legacy = song.with_suffix(".librosa.beats.json")
    legacy.write_text(
        json.dumps(
            {
                "mp3_path": str(song),
                "tempo_bpm": 90.0,
                "beats": [0.1, 0.2],
                "detector": "librosa",
            }
        ),
        encoding="utf-8",
    )
    migrated = load_or_analyze(song)
    assert migrated.beats.beats == (0.1, 0.2)
    assert migrated.beats.tempo_bpm == 90.0
    assert detectors["beats"] == 0

    legacy.write_text("{}", encoding="utf-8")
    assert load_or_analyze(song) == migrated


def test_fresh_analysis_does_not_write_a_legacy_beats_file(tmp_path, detectors) -> None:
    song = make_song(tmp_path)
    load_or_analyze(song)
    assert not song.with_suffix(".librosa.beats.json").exists()


def test_manual_structure_survives_analyzer_version_change(tmp_path, detectors) -> None:
    song = make_song(tmp_path)
    digest = hashlib.sha256(b"song-bytes").hexdigest()
    manual = dataclasses.replace(auto_structure("v0"), source="manual", drop_s=5.0)
    old = SongAnalysis(
        beats=BeatTrack(song, 120.0, (0.5,), "librosa"),
        structure=manual,
        file_sha256=digest,
    )
    sidecar = settings.workspace_dir / "music" / f"{digest}.v0.analysis.json"
    analysis_mod.write_analysis(sidecar, old)

    loaded = load_or_analyze(song)
    assert loaded.structure == manual
    assert detectors == {"beats": 0, "structure": 0}


def test_stale_auto_structure_is_redetected_on_version_change(tmp_path, detectors) -> None:
    song = make_song(tmp_path)
    digest = hashlib.sha256(b"song-bytes").hexdigest()
    old = SongAnalysis(
        beats=BeatTrack(song, 120.0, (0.5,), "librosa"),
        structure=auto_structure("v0"),
        file_sha256=digest,
    )
    analysis_mod.write_analysis(
        settings.workspace_dir / "music" / f"{digest}.v0.analysis.json", old
    )
    loaded = load_or_analyze(song)
    assert loaded.structure.analyzer_version == structure_mod.STRUCTURE_ANALYZER_VERSION
    assert detectors["structure"] == 1


def test_offset_analysis_shifts_and_drops_outside_the_trim(tmp_path) -> None:
    analysis = SongAnalysis(
        beats=BeatTrack(tmp_path / "s.mp3", 120.0, (1.0, 3.0, 5.0, 7.0, 9.0, 15.0), "librosa"),
        structure=auto_structure(),
        file_sha256="abc",
    )
    trimmed = offset_analysis(analysis, trim_start_s=2.0, trim_end_s=12.0)

    assert trimmed.beats.beats == (1.0, 3.0, 5.0, 7.0)
    assert trimmed.beats.tempo_bpm == 120.0
    s = trimmed.structure
    assert s.rising == (0.0, 2.0)
    assert s.climax == (2.0, 8.0)
    assert s.falling == (8.0, 10.0)
    assert s.drop_s == 2.5
    assert trimmed.file_sha256 == "abc"


def test_offset_analysis_clamps_spans_that_fall_outside(tmp_path) -> None:
    analysis = SongAnalysis(
        beats=BeatTrack(tmp_path / "s.mp3", 120.0, (), "librosa"),
        structure=auto_structure(),
        file_sha256="abc",
    )
    s = offset_analysis(analysis, trim_start_s=11.0, trim_end_s=15.0).structure
    assert s.rising == (0.0, 0.0)
    assert s.climax == (0.0, 0.0)
    assert s.falling == (0.0, 4.0)
    assert 0.0 <= s.drop_s <= 4.0
