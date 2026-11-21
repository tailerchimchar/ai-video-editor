"""Google Drive-backed storage for box render jobs.

This module wraps the grokbot/drive uploader to provide a StorageBackend
implementation for the render pipeline running on a Linux box. Files
are downloaded from Drive to a scratch directory, processed locally,
and uploaded back to Drive.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .backend import StorageError

if TYPE_CHECKING:
    from ..grokbot.drive import Uploader

_log = logging.getLogger(__name__)


class DriveStorage:
    """Google Drive storage backend for box render jobs.

    Downloads clips from Drive by ID to a local scratch directory,
    uploads results back to Drive. The scratch directory is managed
    externally (by the box render job); this class only handles
    individual file operations.

    Note: This is NOT a full filesystem abstraction. It's designed
    for the specific render job workflow:
    1. Download input clips by Drive ID -> scratch
    2. Run ffmpeg locally on scratch files
    3. Upload output files from scratch -> Drive
    """

    def __init__(
        self,
        uploader: Uploader,
        scratch_dir: Path,
        *,
        download_timeout: float = 300.0,
    ) -> None:
        self._uploader = uploader
        self._scratch_dir = Path(scratch_dir)
        self._download_timeout = download_timeout
        self._id_to_local: dict[str, Path] = {}

    @property
    def scratch_dir(self) -> Path:
        return self._scratch_dir

    def download_clip(self, drive_id: str, local_name: str) -> Path:
        """Download a clip from Drive to scratch. Returns the local path."""
        import httpx

        from ..grokbot.drive import FILES_URL, DriveError

        local_path = self._scratch_dir / local_name
        local_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            uploader = self._uploader
            if not hasattr(uploader, "_access_token"):
                raise StorageError("uploader does not support download")

            token = uploader._access_token()
            headers = {"Authorization": f"Bearer {token}"}

            with httpx.Client(timeout=self._download_timeout) as client:
                url = f"{FILES_URL}/{drive_id}?alt=media"
                resp = client.get(url, headers=headers, follow_redirects=True)
                if not resp.is_success:
                    raise StorageError(
                        f"Drive download failed: {drive_id}: HTTP {resp.status_code}"
                    )
                local_path.write_bytes(resp.content)

            self._id_to_local[drive_id] = local_path
            _log.info(
                "downloaded %s -> %s (%d bytes)",
                drive_id,
                local_path.name,
                local_path.stat().st_size,
            )
            return local_path

        except DriveError as e:
            raise StorageError(f"Drive download failed: {drive_id}: {e}") from e
        except httpx.HTTPError as e:
            raise StorageError(f"Drive download network error: {drive_id}: {e}") from e
        except OSError as e:
            raise StorageError(f"Drive download IO error: {drive_id}: {e}") from e

    def upload_file(self, local_path: Path, name: str, subfolder: str) -> str:
        """Upload a local file to Drive. Returns the Drive file ID."""
        from ..grokbot.drive import DriveError

        try:
            file_id = self._uploader.upload(local_path, name=name, subfolder=subfolder)
            _log.info(
                "uploaded %s -> %s/%s (id=%s)",
                local_path.name,
                subfolder,
                name,
                file_id,
            )
            return file_id
        except DriveError as e:
            raise StorageError(f"Drive upload failed: {local_path}: {e}") from e

    def read_bytes(self, path: str) -> bytes:
        try:
            return Path(path).read_bytes()
        except OSError as e:
            raise StorageError(f"read failed: {path}: {e}") from e

    def write_bytes(self, path: str, data: bytes) -> None:
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        except OSError as e:
            raise StorageError(f"write failed: {path}: {e}") from e

    def exists(self, path: str) -> bool:
        return Path(path).exists()

    def mkdir(self, path: str, parents: bool = True) -> None:
        try:
            Path(path).mkdir(parents=parents, exist_ok=True)
        except OSError as e:
            raise StorageError(f"mkdir failed: {path}: {e}") from e

    def listdir(self, path: str) -> list[str]:
        try:
            return [p.name for p in Path(path).iterdir()]
        except OSError as e:
            raise StorageError(f"listdir failed: {path}: {e}") from e

    def remove(self, path: str) -> None:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as e:
            raise StorageError(f"remove failed: {path}: {e}") from e

    def rmtree(self, path: str) -> None:
        import shutil

        p = Path(path)
        if not p.exists():
            return
        try:
            shutil.rmtree(p)
        except OSError as e:
            raise StorageError(f"rmtree failed: {path}: {e}") from e

    def copy_to_local(self, remote_path: str, local_path: str) -> None:
        import shutil

        try:
            dst = Path(local_path)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(remote_path, dst)
        except OSError as e:
            raise StorageError(f"copy_to_local failed: {remote_path} -> {local_path}: {e}") from e

    def copy_from_local(self, local_path: str, remote_path: str) -> None:
        import shutil

        try:
            dst = Path(remote_path)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_path, dst)
        except OSError as e:
            raise StorageError(f"copy_from_local failed: {local_path} -> {remote_path}: {e}") from e
