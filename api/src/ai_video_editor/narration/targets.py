"""What can be narrated. Contract for AVE-68; `StoryRoundTarget` and the
registry bodies are implemented by Task A.

OCP: a new kind (montage, short, IRL video) is a new class with a unique
`kind`, registered once with `register_target`; nothing else changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Protocol, runtime_checkable


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


def register_target(target: NarrationTarget) -> None:
    raise NotImplementedError  # Task A


def get_target(kind: str) -> NarrationTarget:
    raise NotImplementedError  # Task A


def fingerprint(path: Path) -> str:
    """Stable id of a video file's current content (size + mtime_ns)."""
    raise NotImplementedError  # Task A
