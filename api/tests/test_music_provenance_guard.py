"""R5 Drive guard, fail closed: provenance comes from a licence.json entry, and
one choke point (the Drive uploader itself) refuses owner_recs / unlicensed /
unknown-provenance renders for every caller (publish and the Grok runner)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_video_editor.config import settings
from ai_video_editor.grokbot import drive
from ai_video_editor.music import library

_LIC = {"title": "T", "source_url": "https://x/y", "licence": "CC0", "note": ""}


@pytest.fixture
def ws(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(settings, "workspace_dir", tmp_path / "ws")
    return tmp_path / "ws"


def _song(ws: Path, folder: str, name: str, *, licensed: bool) -> Path:
    d = ws / "music" / folder if folder else ws / "music"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(b"x")
    if licensed:
        f = d / "licence.json"
        data = json.loads(f.read_text()) if f.is_file() else {}
        data[name] = _LIC
        f.write_text(json.dumps(data))
    return p


class _Inner:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.prunes: list[str] = []

    def upload(self, path: Path, *, name: str, subfolder: str) -> str:
        self.calls.append(name)
        return "id1"

    def prune(self, *, subfolder: str, older_than_days: int) -> int:
        self.prunes.append(subfolder)
        return 0


# ----- (a) provenance by licence entry -----


def test_music_folder_of_needs_a_licence_entry(ws) -> None:
    licensed = _song(ws, "royalty_free", "a.mp3", licensed=True)
    bare = _song(ws, "royalty_free", "bare.mp3", licensed=False)
    legacy = _song(ws, "", "legacy.mp3", licensed=False)
    owner = _song(ws, "owner_recs", "b.m4a", licensed=True)
    assert library.music_folder_of(licensed) == "royalty_free"
    assert library.music_folder_of(bare) == "unlicensed"
    assert library.music_folder_of(legacy) == "unlicensed"
    assert library.music_folder_of(owner) == "owner_recs"
    assert library.music_folder_of(Path("elsewhere") / "s.mp3") == "unlicensed"


def test_provenance_writes_unlicensed_for_bare_royalty_free_file(ws, tmp_path) -> None:
    from ai_video_editor import shorts

    bare = _song(ws, "royalty_free", "bare.mp3", licensed=False)
    out = tmp_path / "out"
    out.mkdir()
    shorts._write_music_provenance(out, str(bare))
    assert json.loads((out / "index.json").read_text())["music_folder"] == "unlicensed"


# ----- (b) guard rules -----


def _render(ws: Path, side: dict | None, sub: str = "shorts/_custom/x") -> Path:
    d = ws / sub
    d.mkdir(parents=True, exist_ok=True)
    mp4 = d / "m.mp4"
    mp4.write_bytes(b"x")
    if side is not None:
        (d / "index.json").write_text(json.dumps(side))
    return mp4


@pytest.mark.parametrize(
    "side",
    [
        {"music_folder": "unlicensed", "music_path": "/m/x.mp3"},
        {"music_folder": None, "music_path": "/m/x.mp3"},
        {"music_folder": "weird", "music_path": None},
    ],
)
def test_guard_refuses_unlicensed_and_null_with_music(ws, side) -> None:
    with pytest.raises(drive.OwnerRecsMusicError, match="refused"):
        drive.check_music_provenance(_render(ws, side))


def test_guard_passes_licensed_and_no_music(ws) -> None:
    drive.check_music_provenance(_render(ws, {"music_folder": "royalty_free", "music_path": "x"}))
    drive.check_music_provenance(
        _render(ws, {"music_folder": None, "music_path": None}, "shorts/y")
    )


def test_guard_refuses_unlicensed_sidecar_outside_shorts(ws) -> None:
    mp4 = _render(ws, {"music_folder": "unlicensed", "music_path": "x"}, "elsewhere")
    with pytest.raises(drive.OwnerRecsMusicError, match="unlicensed"):
        drive.check_music_provenance(mp4)


# ----- (c) one choke point for every Drive upload -----


def test_uploader_from_settings_is_guarded(ws, monkeypatch) -> None:
    for k in ("client_id", "client_secret", "refresh_token", "folder_id"):
        monkeypatch.setattr(settings, f"grokbot_drive_{k}", "x")
    up = drive.uploader_from_settings()
    assert isinstance(up, drive.GuardedUploader)


def test_guarded_uploader_refuses_before_inner_upload(ws) -> None:
    inner = _Inner()
    up = drive.GuardedUploader(inner)
    with pytest.raises(drive.OwnerRecsMusicError):
        up.upload(_render(ws, {"music_folder": "owner_recs"}), name="m.mp4", subfolder="renders")
    assert inner.calls == []
    assert up.prune(subfolder="previews", older_than_days=3) == 0
    assert inner.prunes == ["previews"]


def test_runner_compilation_upload_is_refused_for_owner_recs_spec(ws, tmp_path) -> None:
    """The Grok runner's render upload goes through the guarded uploader: a
    compilation whose spec.json uses owner_recs music never reaches Drive."""
    from ai_video_editor.grokbot.runner import Runner

    folder = ws / "compilations" / "c1"
    folder.mkdir(parents=True)
    out = folder / "out.mp4"
    out.write_bytes(b"x")
    (folder / "spec.json").write_text(
        json.dumps({"music_path": "/m/b.m4a", "music_folder": "owner_recs"})
    )
    inner = _Inner()
    runner = Runner.__new__(Runner)
    runner.uploader = drive.GuardedUploader(inner)
    assert runner._upload(out, "p.mp4", "renders") is None
    assert inner.calls == []


def test_compilation_spec_with_music_but_no_folder_is_refused(ws) -> None:
    folder = ws / "compilations" / "c2"
    folder.mkdir(parents=True)
    out = folder / "out.mp4"
    out.write_bytes(b"x")
    (folder / "spec.json").write_text(json.dumps({"music_path": "/m/x.mp3"}))
    with pytest.raises(drive.OwnerRecsMusicError, match="unknown music provenance"):
        drive.check_music_provenance(out)


def test_compile_spec_records_music_folder(ws) -> None:
    from ai_video_editor.compile import spec_from_rankings

    song = _song(ws, "royalty_free", "a.mp3", licensed=True)
    spec = spec_from_rankings({"id": "a", "filename": "f.mp4"}, [], [], music_path=str(song))
    assert spec["music_folder"] == "royalty_free"
    assert spec_from_rankings({"id": "a", "filename": "f.mp4"}, [], [])["music_folder"] is None


# ----- (d) plan executor -----


def _executor(tmp_path: Path, rows: list[dict]):
    from ai_video_editor.music.library import Library
    from ai_video_editor.routers.plan_executor import EditsPlanExecutor

    lib = tmp_path / "library.json"
    lib.write_text(json.dumps(rows), encoding="utf-8")
    return EditsPlanExecutor(tmp_path, "comp", library=Library(path=lib))


@pytest.mark.parametrize(
    ("folder", "licensed", "label"),
    [("owner_recs", True, "owner_recs"), ("royalty_free", False, "unlicensed")],
)
def test_plan_executor_refuses_owner_recs_and_unlicensed_music(
    ws, tmp_path, folder, licensed, label
) -> None:
    song = _song(ws, folder, "s.mp3", licensed=licensed)
    ex = _executor(tmp_path, [{"song_id": "s", "audio_path": str(song), "tags": []}])
    assert ex._music_file("s") is None
    assert ex._music_problem("s") == f"music: song 's' is {label}; refused"


def test_plan_executor_accepts_licensed_royalty_free(ws, tmp_path) -> None:
    song = _song(ws, "royalty_free", "s.mp3", licensed=True)
    ex = _executor(tmp_path, [{"song_id": "s", "audio_path": str(song), "tags": []}])
    assert ex._music_file("s") == song
    assert ex._music_problem("s") is None


# ----- (e) download without a licence -----


def test_unlicensed_download_is_invisible_and_counted_unlicensed(ws, monkeypatch) -> None:
    from ai_video_editor.music import download as dl

    def fake(url: str, out: Path) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x")
        return out

    monkeypatch.setitem(dl._DOWNLOAD_STRATEGIES, "youtube", fake)
    p = dl.download_music("https://youtu.be/nolicence", name="nl")
    assert library.list_tracks() == []
    assert library.music_folder_of(p) == "unlicensed"
