"""Panel questions per candidate, and reading the answers back (with the
rules-fallback defaults)."""

from __future__ import annotations

from ai_video_editor.decisions import Answer, BoolQuestion, ChoiceQuestion, ScoreQuestion
from ai_video_editor.story.suggest.defaults import default_effect, default_score
from ai_video_editor.story.suggest.models import Candidate, EffectChoice
from ai_video_editor.story.suggest.questions import (
    EDITOR_PROMPT,
    GAMING_PROMPT,
    VIEWER_PROMPT,
    questions_for,
)
from ai_video_editor.story.suggest.verdict import read_verdict


def _facts(**kw) -> dict:
    base = {
        "minute": 15,
        "kills": 1,
        "deaths": 0,
        "numbers": None,
        "outnumbered_win": False,
        "multikill": 1,
        "objective": None,
        "is_last_fight": False,
        "enemy_champions": ["Diana"],
    }
    return {**base, **kw}


def _cand(**kw) -> Candidate:
    return Candidate(id="c1", chapter_index=0, start_s=10.0, end_s=20.0, facts=_facts(**kw))


# --- questions ---


def test_three_questions_one_shared_state():
    c = _cand()
    items = questions_for(c)
    assert len(items) == 3
    (gq, gs), (vq, vs), (eq, es) = items
    assert isinstance(gq, ScoreQuestion) and (gq.lo, gq.hi) == (1, 5)
    assert isinstance(vq, BoolQuestion)
    assert isinstance(eq, ChoiceQuestion) and eq.options is EffectChoice
    assert set(eq.descriptions) == {e.value for e in EffectChoice}
    assert gs == vs == es == c.facts
    assert gs is not c.facts  # a copy: a backend can never mutate the candidate


def test_prompts_quote_the_panel_rubrics_and_mentor_rule():
    assert "Be stingy with 4 and 5" in GAMING_PROMPT
    assert "viewers do not grade mechanics" in VIEWER_PROMPT
    assert "should NOT be over-cut" in EDITOR_PROMPT
    assert "where it kills momentum" in EDITOR_PROMPT


# --- defaults (rules fallback) ---


def test_default_effects():
    assert default_effect(_facts(is_last_fight=True, outnumbered_win=True)) is (
        EffectChoice.letterbox_focus
    )
    assert default_effect(_facts(outnumbered_win=True, multikill=3)) is EffectChoice.slowmo
    assert default_effect(_facts(multikill=2)) is EffectChoice.zoom
    assert default_effect(_facts()) is EffectChoice.none


def test_default_scores():
    assert default_score(_facts(is_last_fight=True)) == 5
    assert default_score(_facts(outnumbered_win=True)) == 4
    assert default_score(_facts(multikill=3)) == 4
    assert default_score(_facts(multikill=2)) == 3
    assert default_score(_facts()) == 2


# --- reading answers ---


def _rules_answers():
    """Exactly what the rules backend says: score no opinion, bool True p 1,
    choice no opinion (EffectChoice has no rule)."""
    return [
        Answer(value=None, probability=0.0, backend="rules"),
        Answer(value=True, probability=1.0, backend="rules"),
        Answer(value=None, probability=0.0, backend="rules"),
    ]


def test_rules_answers_mean_no_opinion_and_use_defaults():
    v = read_verdict(_cand(outnumbered_win=True, numbers="4v5"), _rules_answers())
    assert v.effect is EffectChoice.slowmo and v.effect_p is None
    assert v.score == 4 and v.score_p is None
    assert v.pop_p is None
    assert v.backend == "rules"


def test_jev_answers_are_used():
    answers = [
        Answer(value=5, probability=0.82, backend="jev"),
        Answer(value=False, probability=0.7, backend="jev"),
        Answer(value=EffectChoice.zoom, probability=0.6, backend="jev"),
    ]
    v = read_verdict(_cand(is_last_fight=True), answers)
    assert v.score == 5 and v.score_p == 0.82
    assert abs(v.pop_p - 0.3) < 1e-9  # probability the viewer says yes
    assert v.effect is EffectChoice.zoom and v.effect_p == 0.6
    assert v.backend == "jev"


def test_jev_with_probability_zero_is_no_opinion_per_answer():
    answers = [
        Answer(value=None, probability=0.0, backend="jev"),
        Answer(value=True, probability=0.9, backend="jev"),
        Answer(value=None, probability=0.0, backend="jev"),
    ]
    v = read_verdict(_cand(multikill=2), answers)
    assert v.score == 3 and v.score_p is None
    assert v.pop_p == 0.9
    assert v.effect is EffectChoice.zoom and v.effect_p is None
    assert v.backend == "jev"
