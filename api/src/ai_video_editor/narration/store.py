"""Where narration lives. Contract for AVE-68; `FileNarrationStore` is
implemented by Task A.

OCP/DIP: callers depend on `NarrationStore`; a database or per-user cloud
store is a new implementation, not an edit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .models import NarrationSession, TargetRef


class NarrationStore(Protocol):
    def load(self, ref: TargetRef) -> NarrationSession | None: ...

    def save(self, session: NarrationSession) -> None: ...

    def write_take(self, ref: TargetRef, take_id: str, data: bytes, suffix: str) -> str:
        """Persist take media; returns the `Take.media` name."""
        ...

    def take_path(self, ref: TargetRef, media: str) -> Path: ...

    def delete_take(self, ref: TargetRef, media: str) -> None: ...

    def export_path(self, ref: TargetRef) -> Path: ...
