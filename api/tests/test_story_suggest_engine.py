"""JevPanelEngine: candidates -> one batched ask_many -> ranked items.
`ask_many` is always faked here; nothing touches the network."""

from __future__ import annotations

import re
from pathlib import Path

from ai_video_editor import decisions
from ai_video_editor.decisions import Answer
from ai_video_editor.story.planner import Chapter, StoryPlan
from ai_video_editor.story.suggest import engine as engine_mod
from ai_video_editor.story.suggest.candidates import register_candidate_builder
from ai_video_editor.story.suggest.engine import JevPanelEngine
from ai_video_editor.story.suggest.models import Candidate, EffectChoice


def _facts(**kw):
    base = {
        "minute": 10,
        "kills": 1,
        "deaths": 0,
        "numbers": None,
        "outnumbered_win": False,
        "multikill": 1,
        "objective": None,
        "is_last_fight": False,
        "enemy_champions": ["Ashe"],
    }
    return {**base, **kw}


CANDS = [
    Candidate(
        id="a", chapter_index=0, start_s=100, end_s=110, facts=_facts(multikill=2), anchor_s=104
    ),
    Candidate(
        id="b",
        chapter_index=1,
        start_s=300,
        end_s=320,
        facts=_facts(outnumbered_win=True, numbers="4v5"),
        anchor_s=310,
    ),
    Candidate(
        id="c",
        chapter_index=2,
        start_s=900,
        end_s=930,
        facts=_facts(is_last_fight=True),
        anchor_s=905,
    ),
]


class FixedBuilder:
    def build(self, plan, timeline):
        return list(CANDS)

    def chapter_spans(self, plan, timeline):
        return {0: (90.0, 120.0), 1: (290.0, 330.0), 2: (890.0, 940.0)}


register_candidate_builder("fixed_test", FixedBuilder())

PLAN = StoryPlan(
    asset_id="a1",
    chapters=[Chapter(title="t", start_s=0, end_s=1, kind="fight")],
    total_s=1,
    mic_stream=None,
    audio="mixed",
)


def _rules(items):
    from ai_video_editor.decisions.backends import rules

    return [rules.answer(q, s) for q, s in items]


def test_all_questions_go_in_one_ask_many_call():
    calls = []

    def fake(items):
        calls.append(items)
        return _rules(items)

    items = JevPanelEngine(ask=fake).suggest(PLAN, None, "fixed_test")
    assert len(calls) == 1
    assert len(calls[0]) == 3 * len(CANDS)
    # rules fallback: last fight -> letterbox_focus, 4v5 -> slowmo, double -> zoom
    assert [(i.kind, i.backend) for i in items] == [
        ("letterbox_focus", "rules"),
        ("slowmo", "rules"),
        ("zoom", "rules"),
    ]


def test_default_ask_is_decisions_ask_many_looked_up_at_call_time(monkeypatch):
    calls = []

    def fake(items):
        calls.append(items)
        return _rules(items)

    monkeypatch.setattr(decisions, "ask_many", fake)
    JevPanelEngine().suggest(PLAN, None, "fixed_test")
    assert len(calls) == 1


def test_jev_answers_drive_kind_and_reason():
    def fake(items):
        out = []
        for q, s in items:
            if q.__class__.__name__ == "ScoreQuestion":
                out.append(Answer(value=5, probability=0.82, backend="jev"))
            elif q.__class__.__name__ == "BoolQuestion":
                out.append(Answer(value=True, probability=0.91, backend="jev"))
            else:
                out.append(
                    Answer(
                        value=EffectChoice.none if s["multikill"] == 2 else EffectChoice.slowmo,
                        probability=0.7,
                        backend="jev",
                    )
                )
        return out

    items = JevPanelEngine(ask=fake).suggest(PLAN, None, "fixed_test")
    assert {i.kind for i in items} == {"slowmo"}  # "none" dropped
    b = next(i for i in items if i.chapter_index == 1)
    assert b.reason.startswith("4v5 won")
    assert "play 5/5 (p .82)" in b.reason and "pop (p .91)" in b.reason
    assert b.play_score == 5 and b.pop_p == 0.91 and b.backend == "jev"


def test_no_candidates_means_no_call():
    class Empty:
        def build(self, plan, timeline):
            return []

        def chapter_spans(self, plan, timeline):
            return {}

    register_candidate_builder("empty_test", Empty())
    calls = []
    assert (
        JevPanelEngine(ask=lambda i: calls.append(i) or []).suggest(PLAN, None, "empty_test") == []
    )
    assert calls == []


def test_no_anthropic_import_in_story_suggest():
    pkg = Path(engine_mod.__file__).parent
    pattern = re.compile(r"^\s*(import|from)\s+\S*anthropic", re.MULTILINE)
    offenders = [p.name for p in pkg.glob("*.py") if pattern.search(p.read_text("utf-8"))]
    assert offenders == []
