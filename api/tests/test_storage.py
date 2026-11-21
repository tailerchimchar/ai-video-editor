"""DriveStorage and DriveUploader.download tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from ai_video_editor.grokbot.drive import (
    DEFAULT_MAX_DOWNLOAD_BYTES,
    DriveError,
    DriveUploader,
)
from ai_video_editor.storage import BoxRenderStorage, DriveStorage, StorageError


class _FakeUploader:
    def __init__(self):
        self.downloads: list[tuple[str, Path]] = []
        self.uploads: list[tuple[str, str]] = []

    def download(self, file_id: str, dest: Path, *, max_bytes: int = 0, timeout: float = 300.0):
        self.downloads.append((file_id, dest))
        dest.write_bytes(b"clip-bytes")
        return dest

    def upload(self, path: Path, *, name: str, subfolder: str) -> str:
        self.uploads.append((name, subfolder))
        return "uploaded-id"

    def prune(self, *, subfolder: str, older_than_days: int) -> int:
        return 0


class TestDriveStorage:
    def test_implements_protocol(self, tmp_path):
        storage = DriveStorage(_FakeUploader(), tmp_path)
        assert isinstance(storage, BoxRenderStorage)

    def test_download_clip_uses_uploader(self, tmp_path):
        up = _FakeUploader()
        storage = DriveStorage(up, tmp_path, download_timeout=12.0)
        path = storage.download_clip("FAKE-drive-clip-0001", "clip_000.mp4")
        assert path == tmp_path / "clip_000.mp4"
        assert path.read_bytes() == b"clip-bytes"
        assert up.downloads == [("FAKE-drive-clip-0001", path)]

    def test_upload_file_uses_uploader(self, tmp_path):
        up = _FakeUploader()
        storage = DriveStorage(up, tmp_path)
        local = tmp_path / "out.mp4"
        local.write_bytes(b"x")
        assert storage.upload_file(local, "out.mp4", "finished") == "uploaded-id"
        assert up.uploads == [("out.mp4", "finished")]

    def test_download_maps_drive_error(self, tmp_path):
        class Boom:
            def download(self, *a, **k):
                raise DriveError("nope")

        storage = DriveStorage(Boom(), tmp_path)  # type: ignore[arg-type]
        with pytest.raises(StorageError, match="Drive download failed"):
            storage.download_clip("FAKE-drive-clip-0001", "c.mp4")


def test_drive_uploader_download_streams_and_caps(tmp_path):
    chunks = [b"a" * 100, b"b" * 100]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("alt") == "media"
        assert "/files/FAKE-drive-clip-0001" in str(request.url)
        return httpx.Response(200, content=b"".join(chunks))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    up = DriveUploader("cid", "sec", "rt", "folder", client=client)
    up._token = "tok"
    from datetime import datetime, timezone

    up._token_expiry = datetime.max.replace(tzinfo=timezone.utc)

    dest = tmp_path / "out.mp4"
    assert up.download("FAKE-drive-clip-0001", dest) == dest
    assert dest.read_bytes() == b"".join(chunks)


def test_drive_uploader_download_rejects_bad_id(tmp_path):
    up = DriveUploader("cid", "sec", "rt", "folder", client=httpx.Client())
    with pytest.raises(DriveError, match="invalid Drive file id"):
        up.download("../etc/passwd", tmp_path / "x")


def test_drive_uploader_download_enforces_byte_cap(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 50)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    up = DriveUploader("cid", "sec", "rt", "folder", client=client)
    up._token = "tok"
    from datetime import datetime, timezone

    up._token_expiry = datetime.max.replace(tzinfo=timezone.utc)

    dest = tmp_path / "out.mp4"
    with pytest.raises(DriveError, match="size cap"):
        up.download("FAKE-drive-clip-0001", dest, max_bytes=10)
    assert not dest.exists()
    assert DEFAULT_MAX_DOWNLOAD_BYTES > 0
