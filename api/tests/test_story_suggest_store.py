"""FileSuggestionStore: suggestions.json round trip and accept/reject."""

from __future__ import annotations

import json

import pytest

from ai_video_editor.story.suggest.models import SuggestionItem, SuggestionSet
from ai_video_editor.story.suggest.store import (
    SUGGESTIONS_FILE,
    FileSuggestionStore,
    UnknownSuggestionError,
)


def _set(status="ready") -> SuggestionSet:
    item = SuggestionItem(
        id="s-a-zoom",
        kind="zoom",
        chapter_index=0,
        start_s=1.0,
        end_s=2.0,
        params={"factor": 1.3, "roi": "center"},
        reason="r",
        backend="rules",
    )
    return SuggestionSet(
        round="r1", created_at="2026-11-25T00:00:00+00:00", status=status, items=[item]
    )


def test_missing_is_none(tmp_path):
    assert FileSuggestionStore(tmp_path / "story" / "r1").load() is None


def test_round_trip(tmp_path):
    store = FileSuggestionStore(tmp_path / "r1")
    path = store.save(_set())
    assert path.name == SUGGESTIONS_FILE
    assert json.loads(path.read_text("utf-8"))["status"] == "ready"
    assert store.load() == _set()


def test_corrupt_file_is_value_error(tmp_path):
    (tmp_path / SUGGESTIONS_FILE).write_text("{nope", encoding="utf-8")
    with pytest.raises(ValueError):
        FileSuggestionStore(tmp_path).load()


def test_set_accepted(tmp_path):
    store = FileSuggestionStore(tmp_path)
    store.save(_set())
    assert store.set_accepted("s-a-zoom", True).items[0].accepted is True
    assert store.load().items[0].accepted is True
    assert store.set_accepted("s-a-zoom", None).items[0].accepted is None
    with pytest.raises(UnknownSuggestionError):
        store.set_accepted("nope", True)


def test_set_accepted_without_a_file(tmp_path):
    with pytest.raises(UnknownSuggestionError):
        FileSuggestionStore(tmp_path).set_accepted("x", True)
