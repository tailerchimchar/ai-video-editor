"""FileNarrationStore (AVE-69)."""

from __future__ import annotations

import pytest

from ai_video_editor.config import settings
from ai_video_editor.narration.models import NarrationSession, Take, TargetRef
from ai_video_editor.narration.store import FileNarrationStore, NarrationStore, store_for
from ai_video_editor.narration.targets import StoryRoundTarget

REF = TargetRef(kind="story", key="r1")


@pytest.fixture
def store(tmp_path) -> FileNarrationStore:
    return FileNarrationStore(lambda ref: tmp_path / ref.key)


def _session() -> NarrationSession:
    take = Take(
        id="t1", start_s=1.5, duration_s=2.0, media="t1.webm", created_at="2026-11-25T00:00:00Z"
    )
    return NarrationSession(target=REF, game="league", video_fingerprint="1-2", takes=[take])


def test_satisfies_protocol(store):
    as_protocol: NarrationStore = store  # type-level check; runtime: methods exist
    for name in ("load", "save", "write_take", "take_path", "delete_take", "export_path"):
        assert callable(getattr(as_protocol, name))


def test_load_missing_is_none(store):
    assert store.load(REF) is None


def test_save_load_round_trip(store, tmp_path):
    s = _session()
    store.save(s)
    assert (tmp_path / "r1" / "narration" / "session.json").is_file()
    assert store.load(REF) == s


def test_save_is_atomic_no_temp_left(store, tmp_path):
    store.save(_session())
    store.save(_session())
    names = {p.name for p in (tmp_path / "r1" / "narration").iterdir()}
    assert names == {"session.json"}


def test_save_failure_keeps_old_file(store, tmp_path, monkeypatch):
    store.save(_session())

    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr("ai_video_editor.narration.store.os.replace", boom)
    with pytest.raises(OSError):
        store.save(_session().model_copy(update={"game": "other"}))
    assert store.load(REF).game == "league"
    assert {p.name for p in (tmp_path / "r1" / "narration").iterdir()} == {"session.json"}


def test_write_take_and_path_and_delete(store, tmp_path):
    media = store.write_take(REF, "t1", b"abc", ".webm")
    assert media == "t1.webm"
    p = store.take_path(REF, media)
    assert p == tmp_path / "r1" / "narration" / "takes" / "t1.webm"
    assert p.read_bytes() == b"abc"
    store.delete_take(REF, media)
    assert not p.exists()
    store.delete_take(REF, media)  # missing file ignored


def test_export_path(store, tmp_path):
    assert store.export_path(REF) == tmp_path / "r1" / "narration" / "narrated.mp4"


@pytest.mark.parametrize("suffix", [".exe", "webm", ".mp3", "", ".webm/../x"])
def test_bad_suffix(store, suffix):
    with pytest.raises(ValueError):
        store.write_take(REF, "t1", b"x", suffix)


@pytest.mark.parametrize("tid", ["../t", "a/b", "a\\b", "", "a b"])
def test_bad_take_id(store, tid):
    with pytest.raises(ValueError):
        store.write_take(REF, tid, b"x", ".webm")


@pytest.mark.parametrize("media", ["../x.webm", "a/b.webm", "a\\b.webm", "", ".."])
def test_bad_media_name(store, media):
    with pytest.raises(ValueError):
        store.take_path(REF, media)
    with pytest.raises(ValueError):
        store.delete_take(REF, media)


@pytest.mark.parametrize("suffix", [".webm", ".mp4", ".ogg", ".wav"])
def test_allowed_suffixes(store, suffix):
    assert store.write_take(REF, "t", b"x", suffix) == f"t{suffix}"


def test_store_for_target(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    st = store_for(StoryRoundTarget())
    assert st.export_path(REF) == tmp_path / "story" / "r1" / "narration" / "narrated.mp4"
