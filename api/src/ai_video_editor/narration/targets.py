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
from .models import KEY_PATTERN, Marker


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

    def intro_s(self, key: str) -> float | None:
        """Length of the intro before the first game, or None when unknown.
        Used to suggest a shift when a re-render moved the content."""
        ...

    def markers(self, key: str) -> list[Marker]:
        """Landmarks to show on the timeline (games, chapters, inserts)."""
        ...


class TargetDefaults:
    """Mixin: no intro length and no markers. A target inherits it and
    overrides only what it knows."""

    def intro_s(self, key: str) -> float | None:
        return None

    def markers(self, key: str) -> list[Marker]:
        return []


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


class StoryRoundTarget(TargetDefaults):
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

    def markers(self, key: str) -> list[Marker]:
        """One chapter marker per story.json chapter, at its video second."""
        from ..story.planner import StoryPlan
        from ..story.suggest import placement

        try:
            plan = StoryPlan.model_validate_json(
                (self.work_dir(key) / "story.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return []
        out: list[Marker] = []
        for idx, ch in enumerate(plan.chapters):
            at = placement.video_second(plan, idx, ch.start_s, settings.story_card_seconds)
            if at is not None:
                out.append(Marker(at_s=at, label=ch.title.split(",")[0], kind="chapter"))
        return out


class JoinedStoryTarget(TargetDefaults):
    """A story-join video: `WORKSPACE/story/<key>.mp4`, key e.g.
    `joined_2026-11-26_xayah-kaisa-yunara`. Takes live in
    `WORKSPACE/story/_joined/<key>/` (story/join.py `parts_dir`, next to the
    hook / outro parts). Backups such as `<key>.v1.mp4` are not keys."""

    kind: ClassVar[str] = "joined"

    def _root(self) -> Path:
        return Path(settings.workspace_dir) / "story"

    def work_dir(self, key: str) -> Path:
        return self._root() / "_joined" / _checked_key(key)

    def video_path(self, key: str) -> Path:
        path = self._root() / f"{_checked_key(key)}.mp4"
        if not path.is_file():
            raise UnknownTargetError(f"no joined video {key!r}")
        return path

    def game(self, key: str) -> str:
        return "league"

    def video_url(self, key: str) -> str:
        return f"/workspace/story/{_checked_key(key)}.mp4"

    def list_keys(self) -> list[str]:
        root = self._root()
        if not root.is_dir():
            return []
        keys = [
            p.stem
            for p in root.glob("joined_*.mp4")
            if p.is_file() and re.fullmatch(KEY_PATTERN, p.stem)
        ]
        return sorted(keys, reverse=True)

    def _join_map(self, key: str):
        from ..story.join_map import JoinMap

        path = self._root() / f"{_checked_key(key)}.join.json"
        try:
            return JoinMap.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def intro_s(self, key: str) -> float | None:
        jm = self._join_map(key)
        return None if jm is None else jm.intro_s

    def markers(self, key: str) -> list[Marker]:
        jm = self._join_map(key)
        if jm is None:
            return []
        out: list[Marker] = []
        for g in jm.games:
            out.append(Marker(at_s=g.video_start_s, label=g.label, kind="game"))
            out.extend(
                Marker(at_s=c.video_start_s, label=c.title.split(",")[0], kind="chapter")
                for c in g.chapters
            )
        out.extend(Marker(at_s=i.at_s, label=i.kind, kind="insert") for i in jm.inserts)
        return out


def default_targets() -> list[NarrationTarget]:
    """The targets shipped with the app; add new kinds here (or register them
    from your own module)."""
    return [StoryRoundTarget(), JoinedStoryTarget()]


for _target in default_targets():
    register_target(_target)
