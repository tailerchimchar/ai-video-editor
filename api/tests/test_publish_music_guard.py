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


@pytest.fixture(autouse=True)
def _ws(tmp_path, monkeypatch) -> None:
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path / "ws")


def _render(tmp_path: Path, music_folder: str | None) -> Path:
    tmp_path = tmp_path / "ws" / "shorts" / "_custom" / "x"
    tmp_path.mkdir(parents=True, exist_ok=True)
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


def test_null_music_folder_uploads(tmp_path) -> None:
    up = _Up()
    assert publish._upload(up, _render(tmp_path, None), "m.mp4", "clips/x") == "id1"


def test_shorts_tree_render_without_sidecar_is_refused(tmp_path) -> None:
    mp4 = _render(tmp_path, None)
    (mp4.parent / "index.json").unlink()
    up = _Up()
    with pytest.raises(publish.OwnerRecsMusicError, match="unknown music provenance"):
        publish._upload(up, mp4, "m.mp4", "clips/x")
    assert up.calls == []


def test_shorts_tree_list_sidecar_is_refused(tmp_path) -> None:
    mp4 = _render(tmp_path, None)
    (mp4.parent / "index.json").write_text("[]")
    with pytest.raises(publish.OwnerRecsMusicError):
        publish._upload(_Up(), mp4, "m.mp4", "clips/x")


def test_shorts_tree_sidecar_missing_key_is_refused(tmp_path) -> None:
    mp4 = _render(tmp_path, None)
    (mp4.parent / "index.json").write_text("{}")
    with pytest.raises(publish.OwnerRecsMusicError):
        publish._upload(_Up(), mp4, "m.mp4", "clips/x")


def test_list_sidecar_in_highlights_folder_passes(tmp_path) -> None:
    mp4 = tmp_path / "highlights" / "m.mp4"
    mp4.parent.mkdir()
    mp4.write_bytes(b"x")
    (mp4.parent / "index.json").write_text("[]")
    assert publish._upload(_Up(), mp4, "m.mp4", "clips/x") == "id1"


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
    out = tmp_path / "shorts" / "_custom" / "m"
    out.mkdir(parents=True)
    shorts._write_music_provenance(out, str(song))
    side = json.loads((out / "index.json").read_text())
    assert side["music_folder"] == "owner_recs"
    # ...and the guard then refuses the render sitting next to it.
    (out / "m.mp4").write_bytes(b"x")
    with pytest.raises(publish.OwnerRecsMusicError):
        publish._upload(_Up(), out / "m.mp4", "m.mp4", "clips/x")


def test_provenance_helper_merges_into_dict_and_nulls_without_music(tmp_path) -> None:
    from ai_video_editor import shorts

    (tmp_path / "index.json").write_text(json.dumps({"keep": 1}))
    shorts._write_music_provenance(tmp_path, None)
    side = json.loads((tmp_path / "index.json").read_text())
    assert side == {"keep": 1, "music_path": None, "music_folder": None}
    (tmp_path / "index.json").write_text("[1]")
    shorts._write_music_provenance(tmp_path, None)
    assert json.loads((tmp_path / "index.json").read_text())["music_folder"] is None


@pytest.mark.parametrize(
    "writer",
    [
        "_write_index_md",
        "_write_cross_asset_index_md",
        "_write_outplayed_index_md",
        "_write_custom_index_md",
    ],
)
def test_every_montage_index_writer_records_provenance(writer) -> None:
    import inspect

    from ai_video_editor import shorts

    assert "_write_music_provenance(folder, music_path)" in inspect.getsource(
        getattr(shorts, writer)
    )
