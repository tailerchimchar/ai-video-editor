"""Song library catalog: add + tag/duration suggestion ranking."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_video_editor.config import settings
from ai_video_editor.music.analysis import SongAnalysis
from ai_video_editor.music.beats import BeatTrack
from ai_video_editor.music.library import Library
from ai_video_editor.music.structure import SongStructure


def fake_analyze(path: Path) -> SongAnalysis:
    """Duration is encoded in the file bytes (e.g. b'180')."""
    duration = float(path.read_bytes())
    structure = SongStructure((0.0, 1.0), (1.0, 2.0), (2.0, duration), 1.0, "auto", "test")
    return SongAnalysis(BeatTrack(path, 120.0, (), "librosa"), structure, f"sha-{path.stem}")


@pytest.fixture
def library(tmp_path, monkeypatch) -> Library:
    monkeypatch.setattr(settings, "workspace_dir", tmp_path / "ws")
    return Library(analyze=fake_analyze)


def song(tmp_path, name: str, duration: int) -> Path:
    path = tmp_path / f"{name}.mp3"
    path.write_bytes(str(duration).encode())
    return path


def test_add_records_a_row_and_persists_it(tmp_path, library) -> None:
    row = library.add(song(tmp_path, "a", 180), "Song A", "https://y/a", ["hype"])
    assert row["title"] == "Song A"
    assert row["duration_s"] == 180.0
    assert row["tags"] == ["hype"]
    assert row["source_url"] == "https://y/a"
    assert row["analysis_path"].endswith(".analysis.json")

    on_disk = json.loads(
        (settings.workspace_dir / "media_library" / "music" / "library.json").read_text(
            encoding="utf-8"
        )
    )
    assert on_disk == [row]


def test_adding_the_same_song_again_updates_instead_of_duplicating(tmp_path, library) -> None:
    path = song(tmp_path, "a", 180)
    first = library.add(path, "Song A", "u", ["hype"])
    second = library.add(path, "Song A v2", "u", ["hype", "epic"])
    assert second["song_id"] == first["song_id"]
    assert Library(analyze=fake_analyze).suggest(["epic"], 180)[0]["title"] == ("Song A v2")


def test_suggest_ranks_by_tag_overlap_then_duration_closeness(tmp_path, library) -> None:
    library.add(song(tmp_path, "far", 300), "far", "u", ["hype", "epic"])
    library.add(song(tmp_path, "near", 95), "near", "u", ["hype", "epic"])
    library.add(song(tmp_path, "one", 60), "one", "u", ["hype"])
    library.add(song(tmp_path, "none", 61), "none", "u", ["sad"])

    ranked = library.suggest(["hype", "epic"], duration_s=60)
    assert [r["title"] for r in ranked] == ["near", "far", "one", "none"]


def test_empty_library_suggests_nothing(library) -> None:
    assert library.suggest(["hype"], 60) == []
