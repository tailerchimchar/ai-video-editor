"""Licensed music folders: royalty_free/ and owner_recs/, each with licence.json."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from ai_video_editor.config import settings
from ai_video_editor.music import library
from ai_video_editor.music.download import download_music


@pytest.fixture
def music_root(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    return tmp_path / "music"


def _put(root: Path, folder: str, name: str, licence: dict | None) -> None:
    d = root / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(b"x")
    if licence is not None:
        f = d / "licence.json"
        data = json.loads(f.read_text()) if f.is_file() else {}
        data[name] = licence
        f.write_text(json.dumps(data))


_LIC = {"title": "T", "source_url": "https://x/y", "licence": "CC-BY", "note": ""}


def test_list_tracks_returns_both_folders(music_root) -> None:
    _put(music_root, "royalty_free", "a.mp3", _LIC)
    _put(music_root, "owner_recs", "b.m4a", {**_LIC, "title": "B", "licence": "label"})
    tracks = library.list_tracks()
    assert {(t.path.name, t.folder, t.title) for t in tracks} == {
        ("a.mp3", "royalty_free", "T"),
        ("b.m4a", "owner_recs", "B"),
    }
    assert [t.licence for t in library.list_tracks("owner_recs")] == ["label"]


def test_file_without_licence_entry_is_skipped_with_warning(music_root, caplog) -> None:
    _put(music_root, "royalty_free", "ok.mp3", _LIC)
    _put(music_root, "royalty_free", "bare.mp3", None)
    with caplog.at_level(logging.WARNING):
        tracks = library.list_tracks("royalty_free")
    assert [t.path.name for t in tracks] == ["ok.mp3"]
    assert "bare.mp3" in caplog.text


def test_music_folder_of(music_root) -> None:
    _put(music_root, "royalty_free", "a.mp3", _LIC)
    assert library.music_folder_of(music_root / "owner_recs" / "b.m4a") == "owner_recs"
    assert library.music_folder_of(music_root / "royalty_free" / "a.mp3") == "royalty_free"
    # Provenance is the licence entry, not the location (R5, fail closed).
    assert library.music_folder_of(music_root / "royalty_free" / "x.mp3") == "unlicensed"
    assert library.music_folder_of(music_root / "legacy.mp3") == "unlicensed"
    assert library.music_folder_of(Path("elsewhere") / "s.mp3") == "unlicensed"


def test_download_writes_into_folder_argument(music_root, tmp_path, monkeypatch) -> None:
    from ai_video_editor.music import download as dl

    def fake(url: str, out: Path) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x")
        return out

    monkeypatch.setitem(dl._DOWNLOAD_STRATEGIES, "youtube", fake)
    a = download_music("https://youtu.be/a", name="a")
    b = download_music("https://youtu.be/b", name="b", folder="owner_recs", licence="label")
    assert a == music_root / "royalty_free" / "a.mp3"
    assert b == music_root / "owner_recs" / "b.mp3"
    entry = json.loads((music_root / "owner_recs" / "licence.json").read_text())["b.mp3"]
    assert entry["licence"] == "label" and entry["source_url"] == "https://youtu.be/b"
    assert [t.path.name for t in library.list_tracks("owner_recs")] == ["b.mp3"]


def test_legacy_flat_file_is_reused_as_royalty_free_with_warning(
    music_root, monkeypatch, caplog
) -> None:
    music_root.mkdir(parents=True)
    flat = music_root / "old.mp3"
    flat.write_bytes(b"x")
    with caplog.at_level(logging.WARNING):
        assert download_music("https://youtu.be/zzz", name="old") == flat
    assert "licence" in caplog.text


def test_corrupt_licence_json_warns(music_root, caplog) -> None:
    _put(music_root, "royalty_free", "a.mp3", _LIC)
    (music_root / "royalty_free" / "licence.json").write_text("{not json")
    with caplog.at_level(logging.WARNING):
        assert library.list_tracks("royalty_free") == []
    assert "not valid JSON" in caplog.text


def test_owner_recs_download_requires_licence(music_root) -> None:
    with pytest.raises(ValueError, match="licence"):
        download_music("https://youtu.be/a", folder="owner_recs")


def test_unknown_folder_rejected(music_root) -> None:
    with pytest.raises(ValueError, match="folder"):
        download_music("https://youtu.be/a", folder="mine")
