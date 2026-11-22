"""Drive guard: a render whose index.json says music_folder == owner_recs never uploads."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_video_editor.grokbot import publish


class _Up:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def upload(self, path: Path, *, name: str, subfolder: str) -> str:
        self.calls.append(name)
        return "id1"


def _render(tmp_path: Path, music_folder: str | None) -> Path:
    mp4 = tmp_path / "m.mp4"
    mp4.write_bytes(b"x")
    side = {"music_folder": music_folder}
    (tmp_path / "index.json").write_text(json.dumps(side))
    return mp4


def test_owner_recs_render_is_refused(tmp_path) -> None:
    up = _Up()
    with pytest.raises(publish.OwnerRecsMusicError, match="refused: owner_recs music"):
        publish._upload(up, _render(tmp_path, "owner_recs"), "m.mp4", "clips/x")
    assert up.calls == []


def test_royalty_free_render_uploads(tmp_path) -> None:
    up = _Up()
    assert publish._upload(up, _render(tmp_path, "royalty_free"), "m.mp4", "clips/x") == "id1"
    assert up.calls == ["m.mp4"]


def test_list_style_index_json_is_not_a_montage_sidecar(tmp_path) -> None:
    mp4 = tmp_path / "m.mp4"
    mp4.write_bytes(b"x")
    (tmp_path / "index.json").write_text("[]")
    up = _Up()
    assert publish._upload(up, mp4, "m.mp4", "clips/x") == "id1"


def test_no_uploader_still_refuses_owner_recs(tmp_path) -> None:
    with pytest.raises(publish.OwnerRecsMusicError):
        publish._upload(None, _render(tmp_path, "owner_recs"), "m.mp4", "clips/x")


def test_custom_montage_sidecar_records_music_folder(tmp_path, monkeypatch) -> None:
    from ai_video_editor import shorts
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    song = tmp_path / "music" / "owner_recs" / "b.m4a"
    song.parent.mkdir(parents=True)
    song.write_bytes(b"x")
    shorts._write_custom_index_json(tmp_path, name="m", music_path=str(song))
    side = json.loads((tmp_path / "index.json").read_text())
    assert side["music_folder"] == "owner_recs"
    # ...and the guard then refuses the render sitting next to it.
    (tmp_path / "m.mp4").write_bytes(b"x")
    with pytest.raises(publish.OwnerRecsMusicError):
        publish._upload(_Up(), tmp_path / "m.mp4", "m.mp4", "clips/x")
