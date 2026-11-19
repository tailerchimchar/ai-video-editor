"""Where the per-game hint files live and which one a game resolves to.

Its own module so both `prompts` (renders hints) and `corrections`
(hashes them into the cache key) depend on it instead of on each other.
"""

from __future__ import annotations

from pathlib import Path

HINTS_DIR = Path(__file__).parent / "game_hints"


def resolve_hint_file(game: str | None) -> tuple[Path | None, str]:
    """Locate the .md file for `game`. Returns (`path or None`,
    `resolved_name`). Never reads content."""
    if game:
        g = game.lower().strip()
        specific = HINTS_DIR / f"{g}.md"
        if specific.is_file():
            return specific, specific.stem
        for candidate in HINTS_DIR.glob("*.md"):
            stem = candidate.stem
            if stem.startswith("_"):
                continue
            if stem in g:
                return candidate, stem
    default = HINTS_DIR / "_default.md"
    if default.is_file():
        return default, "_default"
    return None, "_missing"
