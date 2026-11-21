"""Music download tests. Uses monkeypatch to avoid actually hitting
YouTube in unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_video_editor.music import download as dl
from ai_video_editor.music.download import (
    _cache_key,
    _detect_source,
    download_music,
)


def test_youtube_url_detected() -> None:
    assert _detect_source("https://www.youtube.com/watch?v=abc") == "youtube"
    assert _detect_source("https://youtu.be/abc") == "youtube"
    assert _detect_source("https://music.youtube.com/watch?v=x") == "youtube"


def test_local_path_detected_when_file_exists(tmp_path) -> None:
    p = tmp_path / "song.mp3"
    p.write_bytes(b"fake")
    assert _detect_source(str(p)) == "local_path"


def test_unknown_url_raises() -> None:
    with pytest.raises(ValueError, match="no download strategy"):
        _detect_source("https://vimeo.com/12345")


def test_cache_key_is_deterministic() -> None:
    """Same URL → same hash. Always."""
    url = "https://www.youtube.com/watch?v=cixr-7fMWwU"
    assert _cache_key(url) == _cache_key(url)
    assert len(_cache_key(url)) == 12


def test_local_path_returns_url_verbatim(tmp_path, monkeypatch) -> None:
    """A local file URL should bypass download entirely."""
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    src = tmp_path / "existing.mp3"
    src.write_bytes(b"fake")
    result = download_music(str(src))
    assert result == src


def test_download_music_uses_cache_on_second_call(tmp_path, monkeypatch) -> None:
    """A YouTube URL that's already cached must NOT re-invoke the
    strategy. Guards against surprise network calls on re-run."""
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    music_dir = tmp_path / "music"
    music_dir.mkdir(parents=True)
    url = "https://youtu.be/test123"
    stem = _cache_key(url)
    cached = music_dir / f"{stem}.mp3"
    cached.write_bytes(b"fake mp3 bytes")

    calls = {"n": 0}

    def _boom(u: str, out: Path) -> Path:  # would signal a re-download
        calls["n"] += 1
        return out

    monkeypatch.setitem(dl._DOWNLOAD_STRATEGIES, "youtube", _boom)
    result = download_music(url)
    assert result == cached
    assert calls["n"] == 0  # strategy not invoked → cache hit


def test_download_music_names_file_by_name_kwarg(tmp_path, monkeypatch) -> None:
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)

    def _fake(url: str, out: Path) -> Path:
        out.write_bytes(b"x")
        return out

    monkeypatch.setitem(dl._DOWNLOAD_STRATEGIES, "youtube", _fake)
    result = download_music("https://youtu.be/x", name="my_beat")
    assert result.name == "my_beat.mp3"
