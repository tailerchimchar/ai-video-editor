"""Box-render storage protocol.

Defines the minimal surface the box render job needs: download a Drive
clip into scratch, upload a finished file, and expose the scratch dir.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


class StorageError(Exception):
    """Storage operation failed (upload, download, delete)."""


@runtime_checkable
class BoxRenderStorage(Protocol):
    """Protocol for box render storage operations."""

    @property
    def scratch_dir(self) -> Path:
        """Local scratch directory for downloaded clips and render outputs."""
        ...

    def download_clip(self, drive_id: str, local_name: str) -> Path:
        """Download a clip from Drive to scratch. Returns the local path."""
        ...

    def upload_file(self, local_path: Path, name: str, subfolder: str) -> str:
        """Upload a local file to Drive. Returns the Drive file ID."""
        ...
