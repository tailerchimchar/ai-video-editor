"""Google Drive-backed storage for box render jobs.

Wraps the grokbot/drive uploader for the two operations the box needs:
download a clip by Drive id into scratch, and upload a finished file.
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
    """Google Drive storage for box render: download clips, upload outputs."""

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

    @property
    def scratch_dir(self) -> Path:
        return self._scratch_dir

    def download_clip(self, drive_id: str, local_name: str) -> Path:
        """Download a clip from Drive to scratch. Returns the local path."""
        from ..grokbot.drive import DriveError

        local_path = self._scratch_dir / local_name
        local_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            self._uploader.download(drive_id, local_path, timeout=self._download_timeout)
            _log.info(
                "downloaded %s -> %s (%d bytes)",
                drive_id,
                local_path.name,
                local_path.stat().st_size,
            )
            return local_path
        except DriveError as e:
            raise StorageError(f"Drive download failed: {e}") from e
        except AttributeError as e:
            raise StorageError("uploader does not support download") from e

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
            raise StorageError(f"Drive upload failed: {name}: {e}") from e
