"""apply: accepted suggestions -> effect requests in requests.json."""

from __future__ import annotations

import pytest

from ai_video_editor.story.requests import Drop, Effect, StoryRequests, load_requests, save_requests
from ai_video_editor.story.suggest.apply import apply_accepted, to_effects
from ai_video_editor.story.suggest.models import SuggestionItem, SuggestionSet
from ai_video_editor.story.suggest.store import FileSuggestionStore, UnknownSuggestionError


def _item(iid, kind, start, end, params, accepted) -> SuggestionItem:
    return SuggestionItem(
        id=iid,
        kind=kind,
        chapter_index=0,
        start_s=start,
        end_s=end,
        params=params,
        reason="r",
        backend="rules",
        accepted=accepted,
    )


ITEMS = [
    _item("lb", "letterbox_focus", 1900.0, 1930.0, {"bar": 0.12}, True),
    _item("sm", "slowmo", 1000.0, 1002.5, {"factor": 0.5}, True),
    _item("zm", "zoom", 500.0, 510.0, {"factor": 1.3, "roi": "center"}, False),
    _item("un", "zoom", 600.0, 610.0, {"factor": 1.3, "roi": "center"}, None),
]


def _store(tmp_path) -> FileSuggestionStore:
    store = FileSuggestionStore(tmp_path)
    store.save(SuggestionSet(round="r1", created_at="t", status="ready", items=ITEMS))
    return store


def test_letterbox_focus_is_two_effects_over_one_span():
    lb, fo = to_effects(ITEMS[0])
    assert (lb.effect, fo.effect) == ("letterbox", "focus")
    assert lb.start == fo.start == 1900.0 and lb.end == fo.end == 1930.0
    assert lb.params == {"bar": 0.12} and fo.params == {}
    assert 1900.0 <= float(lb.at) <= 1930.0


def test_slowmo_and_zoom_are_one_effect():
    (sm,) = to_effects(ITEMS[1])
    assert sm.effect == "slowmo" and sm.params == {"factor": 0.5}
    assert (sm.start, sm.end) == (1000.0, 1002.5)


def test_apply_appends_only_accepted_and_marks_applied(tmp_path):
    save_requests(tmp_path, StoryRequests(requests=[Drop(at="5:00")]))
    store = _store(tmp_path)
    result, added = apply_accepted(tmp_path, store)
    assert added == 3
    assert result.status == "applied" and store.load().status == "applied"
    reqs = load_requests(tmp_path).requests
    assert isinstance(reqs[0], Drop)
    assert [r.effect for r in reqs[1:]] == ["letterbox", "focus", "slowmo"]
    assert all(isinstance(r, Effect) for r in reqs[1:])


def test_apply_is_idempotent(tmp_path):
    store = _store(tmp_path)
    apply_accepted(tmp_path, store)
    _, added = apply_accepted(tmp_path, store)
    assert added == 0
    assert len(load_requests(tmp_path).requests) == 3


def test_apply_without_suggestions_raises(tmp_path):
    with pytest.raises(UnknownSuggestionError):
        apply_accepted(tmp_path, FileSuggestionStore(tmp_path))
