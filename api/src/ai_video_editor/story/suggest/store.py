"""Where a round's suggestions live. `SuggestionStore` is the seam; the file
implementation keeps `WORKSPACE/story/<round>/suggestions.json`."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from .models import SuggestionSet

SUGGESTIONS_FILE = "suggestions.json"


class UnknownSuggestionError(LookupError):
    """No suggestion with that id (or no suggestions at all)."""


class SuggestionStore(Protocol):
    def load(self) -> SuggestionSet | None: ...

    def save(self, suggestions: SuggestionSet) -> object: ...

    def set_accepted(self, item_id: str, accepted: bool | None) -> SuggestionSet: ...


class FileSuggestionStore:
    def __init__(self, story_dir: Path) -> None:
        self.path = Path(story_dir) / SUGGESTIONS_FILE

    def load(self) -> SuggestionSet | None:
        """None when missing; ValueError when unreadable or invalid."""
        if not self.path.is_file():
            return None
        try:
            return SuggestionSet.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError) as e:
            raise ValueError(f"{SUGGESTIONS_FILE}: unreadable: {e}") from e

    def save(self, suggestions: SuggestionSet) -> Path:
        """Atomic replace, so a reader never sees half a file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(suggestions.model_dump_json(indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
        return self.path

    def set_accepted(self, item_id: str, accepted: bool | None) -> SuggestionSet:
        current = self.load()
        if current is None or not any(i.id == item_id for i in current.items):
            raise UnknownSuggestionError(f"no suggestion {item_id!r}")
        items = [
            i.model_copy(update={"accepted": accepted}) if i.id == item_id else i
            for i in current.items
        ]
        updated = current.model_copy(update={"items": items})
        self.save(updated)
        return updated
