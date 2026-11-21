"""Storage abstraction for local and cloud-backed file operations.

This module provides a protocol-based storage interface that enables
the same render pipeline to operate on local disk (PC) or cloud
storage (box render). The compile/render code injects a storage
backend rather than hardcoding Path operations.

Implementations:
- LocalStorage: filesystem operations via Path (default on PC)
- DriveStorage: Google Drive operations via the grokbot/drive module
"""

from .backend import (
    BoxRenderStorage,
    LocalStorage,
    StorageBackend,
    StorageError,
)
from .drive_backend import DriveStorage

__all__ = [
    "BoxRenderStorage",
    "DriveStorage",
    "LocalStorage",
    "StorageBackend",
    "StorageError",
]
