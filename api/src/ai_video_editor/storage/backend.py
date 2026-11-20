"""Storage backend protocol and local filesystem implementation.

The protocol defines the minimal surface needed by the render pipeline:
read/write bytes, create directories, check existence, list files, and
remove files. LocalStorage is the filesystem implementation used on
the PC today; DriveStorage wraps the grokbot/drive module.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Protocol, runtime_checkable

_log = logging.getLogger(__name__)


class StorageError(Exception):
    """Storage operation failed (upload, download, delete)."""


@runtime_checkable
class StorageBackend(Protocol):
    """Protocol for storage operations.

    Read/write paths are abstract identifiers: for LocalStorage they
    are filesystem paths, for DriveStorage they are Drive file IDs
    combined with local scratch paths.
    """

    def read_bytes(self, path: str) -> bytes:
        """Read file contents as bytes. Raises StorageError on failure."""
        ...

    def write_bytes(self, path: str, data: bytes) -> None:
        """Write bytes to a file, creating parent dirs. Raises StorageError."""
        ...

    def exists(self, path: str) -> bool:
        """Check if a file/folder exists."""
        ...

    def mkdir(self, path: str, parents: bool = True) -> None:
        """Create a directory, optionally with parents."""
        ...

    def listdir(self, path: str) -> list[str]:
        """List immediate children of a directory."""
        ...

    def remove(self, path: str) -> None:
        """Remove a file. No-op if missing."""
        ...

    def rmtree(self, path: str) -> None:
        """Remove a directory tree. No-op if missing."""
        ...

    def copy_to_local(self, remote_path: str, local_path: str) -> None:
        """Copy from storage to a local path. For LocalStorage, this is a copy."""
        ...

    def copy_from_local(self, local_path: str, remote_path: str) -> None:
        """Copy from a local path to storage. For LocalStorage, this is a copy."""
        ...


class LocalStorage:
    """Filesystem-backed storage (the default on the PC).

    All paths are interpreted as filesystem paths. This is the
    implementation that keeps PC behavior unchanged.
    """

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
        p = Path(path)
        if not p.exists():
            return
        try:
            shutil.rmtree(p)
        except OSError as e:
            raise StorageError(f"rmtree failed: {path}: {e}") from e

    def copy_to_local(self, remote_path: str, local_path: str) -> None:
        try:
            dst = Path(local_path)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(remote_path, dst)
        except OSError as e:
            raise StorageError(f"copy_to_local failed: {remote_path} -> {local_path}: {e}") from e

    def copy_from_local(self, local_path: str, remote_path: str) -> None:
        try:
            dst = Path(remote_path)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_path, dst)
        except OSError as e:
            raise StorageError(f"copy_from_local failed: {local_path} -> {remote_path}: {e}") from e
