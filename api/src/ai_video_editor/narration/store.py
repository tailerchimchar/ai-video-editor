"""Where narration lives.

OCP/DIP: callers depend on `NarrationStore`; a database or per-user cloud
store is a new implementation, not an edit.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .models import KEY_PATTERN, NarrationSession, TargetRef
from .targets import NarrationTarget

ALLOWED_SUFFIXES = frozenset({".webm", ".mp4", ".ogg", ".wav"})
_MEDIA_NAME = re.compile(r"[0-9A-Za-z_\-]+\.[0-9A-Za-z]+")


class NarrationStore(Protocol):
    def load(self, ref: TargetRef) -> NarrationSession | None: ...

    def save(self, session: NarrationSession) -> None: ...

    def write_take(self, ref: TargetRef, take_id: str, data: bytes, suffix: str) -> str:
        """Persist take media; returns the `Take.media` name."""
        ...

    def take_path(self, ref: TargetRef, media: str) -> Path: ...

    def delete_take(self, ref: TargetRef, media: str) -> None: ...

    def export_path(self, ref: TargetRef) -> Path: ...


class FileNarrationStore:
    """Files under `<root>/narration/`: `session.json`, `takes/`, `narrated.mp4`.

    `root_for` maps a target to its folder (a target's `work_dir`), so this
    class knows nothing about stories or any other kind of video.
    """

    def __init__(self, root_for: Callable[[TargetRef], Path]) -> None:
        self._root_for = root_for

    # -- paths ---------------------------------------------------------
    def _dir(self, ref: TargetRef) -> Path:
        return self._root_for(ref) / "narration"

    def _session_file(self, ref: TargetRef) -> Path:
        return self._dir(ref) / "session.json"

    def take_path(self, ref: TargetRef, media: str) -> Path:
        if not _MEDIA_NAME.fullmatch(media):
            raise ValueError(f"invalid media name: {media!r}")
        return self._dir(ref) / "takes" / media

    def export_path(self, ref: TargetRef) -> Path:
        return self._dir(ref) / "narrated.mp4"

    # -- session -------------------------------------------------------
    def load(self, ref: TargetRef) -> NarrationSession | None:
        path = self._session_file(ref)
        if not path.is_file():
            return None
        return NarrationSession.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, session: NarrationSession) -> None:
        """Write atomically: temp file in the same folder, then replace."""
        path = self._session_file(session.target)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".session-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(session.model_dump_json(indent=2))
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # -- takes ---------------------------------------------------------
    def write_take(self, ref: TargetRef, take_id: str, data: bytes, suffix: str) -> str:
        if not re.fullmatch(KEY_PATTERN, take_id):
            raise ValueError(f"invalid take id: {take_id!r}")
        if suffix not in ALLOWED_SUFFIXES:
            raise ValueError(f"unsupported take suffix: {suffix!r}")
        media = f"{take_id}{suffix}"
        path = self.take_path(ref, media)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return media

    def delete_take(self, ref: TargetRef, media: str) -> None:
        self.take_path(ref, media).unlink(missing_ok=True)


def store_for(target: NarrationTarget) -> FileNarrationStore:
    """A file store rooted at the target's own work folder."""
    return FileNarrationStore(lambda ref: target.work_dir(ref.key))
