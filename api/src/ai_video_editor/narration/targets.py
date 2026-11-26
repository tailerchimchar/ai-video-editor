"""What can be narrated.

OCP: a new kind (montage, short, IRL video) is a new class with a unique
`kind`, registered once with `register_target`; nothing else changes.

The registry is the only module-level state in the narration package.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar, Protocol, runtime_checkable

from ..config import settings
from .models import KEY_PATTERN


class UnknownTargetError(LookupError):
    """No registered kind, or the key does not resolve to a video."""


@runtime_checkable
class NarrationTarget(Protocol):
    kind: ClassVar[str]

    def video_path(self, key: str) -> Path:
        """The video the owner narrates over. Raises UnknownTargetError."""
        ...

    def work_dir(self, key: str) -> Path:
        """Folder that owns this video's narration (the store lives in
        `<work_dir>/narration/`)."""
        ...

    def game(self, key: str) -> str:
        """Game profile name for camera defaults ("league", "default", ...)."""
        ...

    def video_url(self, key: str) -> str:
        """Browser URL of the video (under the `/workspace` static mount)."""
        ...

    def list_keys(self) -> list[str]:
        """Keys whose video exists, newest first."""
        ...


_REGISTRY: dict[str, NarrationTarget] = {}


def register_target(target: NarrationTarget) -> None:
    """Add a target under its `kind`. A duplicate kind is a programming error."""
    if target.kind in _REGISTRY:
        raise ValueError(f"narration target kind already registered: {target.kind!r}")
    _REGISTRY[target.kind] = target


def get_target(kind: str) -> NarrationTarget:
    """The target registered for `kind`, or UnknownTargetError."""
    try:
        return _REGISTRY[kind]
    except KeyError:
        raise UnknownTargetError(f"unknown narration kind: {kind!r}") from None


def registered_targets() -> list[NarrationTarget]:
    """Every registered target, in registration order."""
    return list(_REGISTRY.values())


def fingerprint(path: Path) -> str:
    """Stable id of a video file's current content (size + mtime_ns)."""
    st = path.stat()
    return f"{st.st_size}-{st.st_mtime_ns}"


def _checked_key(key: str) -> str:
    """Reject anything that is not a plain folder name (no path traversal)."""
    if not re.fullmatch(KEY_PATTERN, key):
        raise UnknownTargetError(f"invalid key: {key!r}")
    return key


class StoryRoundTarget:
    """A rendered story round: `WORKSPACE/story/<round>/story.mp4`.

    Story rounds are League of Legends today, so `game` is always "league".
    """

    kind: ClassVar[str] = "story"
    _VIDEO_NAME = "story.mp4"

    def work_dir(self, key: str) -> Path:
        return Path(settings.workspace_dir) / "story" / _checked_key(key)

    def video_path(self, key: str) -> Path:
        path = self.work_dir(key) / self._VIDEO_NAME
        if not path.is_file():
            raise UnknownTargetError(f"no story video for round {key!r}")
        return path

    def game(self, key: str) -> str:
        return "league"

    def video_url(self, key: str) -> str:
        return f"/workspace/story/{_checked_key(key)}/{self._VIDEO_NAME}"

    def list_keys(self) -> list[str]:
        """Round folders that hold a story.mp4 (skipping `_*`), newest first.
        Keys start with their date, so a reverse name sort is newest first."""
        root = Path(settings.workspace_dir) / "story"
        if not root.is_dir():
            return []
        keys = [
            p.name
            for p in root.iterdir()
            if not p.name.startswith("_")
            and re.fullmatch(KEY_PATTERN, p.name)
            and (p / self._VIDEO_NAME).is_file()
        ]
        return sorted(keys, reverse=True)


def default_targets() -> list[NarrationTarget]:
    """The targets shipped with the app; add new kinds here (or register them
    from your own module)."""
    return [StoryRoundTarget()]


for _target in default_targets():
    register_target(_target)
