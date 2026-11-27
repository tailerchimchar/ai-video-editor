"""story/suggest/models.py: the pinned shapes shared with the web UI."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai_video_editor.story.suggest.models import (
    Candidate,
    EffectChoice,
    SuggestionItem,
    SuggestionSet,
)


def _item(**kw) -> SuggestionItem:
    base = dict(
        id="s1",
        kind="slowmo",
        chapter_index=2,
        start_s=100.0,
        end_s=102.5,
        params={"factor": 0.5},
        reason="4v5 won",
        play_score=5,
        pop_p=0.9,
        backend="jev",
        accepted=None,
    )
    return SuggestionItem(**{**base, **kw})


def test_effect_choice_members():
    assert [e.value for e in EffectChoice] == ["none", "letterbox_focus", "slowmo", "zoom"]


def test_candidate_facts_round_trip():
    c = Candidate(
        id="c1",
        chapter_index=0,
        start_s=10.0,
        end_s=20.0,
        facts={"minute": 0, "kills": 1, "enemy_champions": ["Diana"]},
    )
    assert Candidate.model_validate_json(c.model_dump_json()) == c
    assert c.anchor_s is None


def test_item_kind_and_backend_are_pinned():
    with pytest.raises(ValidationError):
        _item(kind="none")
    with pytest.raises(ValidationError):
        _item(backend="haiku")
    assert _item(play_score=None, pop_p=None).play_score is None


def test_set_defaults_and_round_trip():
    s = SuggestionSet(
        round="r1", created_at="2026-11-25T00:00:00Z", status="ready", items=[_item()]
    )
    assert s.error is None
    assert SuggestionSet.model_validate_json(s.model_dump_json()) == s
    with pytest.raises(ValidationError):
        SuggestionSet(round="r1", created_at="x", status="done")
