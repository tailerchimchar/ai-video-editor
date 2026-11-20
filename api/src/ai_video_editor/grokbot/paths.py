"""Path guard shared by the runner and the round publisher."""

from __future__ import annotations

from pathlib import Path


def inside(path: str | Path, *roots: Path) -> bool:
    """True when `path` resolves inside one of `roots` (symlinks and `..` resolved)."""
    try:
        p = Path(path).resolve()
    except (OSError, ValueError):
        return False
    return any(p.is_relative_to(Path(r).resolve()) for r in roots)
