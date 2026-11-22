"""Audit log of every typed-decision answer: all answers logged, text
fields stripped, excerpt truncated, never raises."""

from __future__ import annotations

import json

import pytest

from ai_video_editor import anthropic_surface, decisions
from ai_video_editor.config import settings
from ai_video_editor.decisions import BoolQuestion, ChoiceQuestion, audit
from ai_video_editor.taxonomy import Format


@pytest.fixture(autouse=True)
def _rules_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(anthropic_surface, "_calls", {})
    monkeypatch.setattr(settings, "typesafe_api_key", None)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "decisions_backend", "auto")
    monkeypatch.setattr(settings, "workspace_dir", tmp_path / "ws")


def _lines(tmp_path):
    files = list((tmp_path / "ws" / "decisions").glob("jev-*.jsonl"))
    assert len(files) == 1
    return [json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines()]


def test_every_ask_many_answer_is_logged(tmp_path):
    q = BoolQuestion(prompt="keep?")
    f = ChoiceQuestion(prompt="format?", options=Format)
    items = [(q, {"clip": {"ref": "a1"}}), (f, {"candidate": {"id": "c9"}}), (q, {"x": 1})]
    answers = decisions.ask_many(items)
    rows = _lines(tmp_path)
    assert len(answers) == len(rows) == 3
    assert {r["backend"] for r in rows} == {"rules"}
    assert [r["clip_ref"] for r in rows] == ["a1", "c9", None]
    assert [r["question"] for r in rows] == ["keep?", "format?", "keep?"]


def test_excerpt_strips_text_fields_recursively():
    state = {
        "clip": {"ref": "a", "transcript": "secret", "n": {"words": [1], "k": 2}},
        "voice": "v",
    }
    ex = audit.state_excerpt(state)
    assert ex == {"clip": {"ref": "a", "n": {"k": 2}}}


def test_excerpt_truncates_to_600_chars():
    ex = audit.state_excerpt({"blob": "x" * 5000})
    assert len(ex["excerpt"]) == 600


def test_log_answers_never_raises(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    rec = audit.make_record(
        "rules", "q", {}, decisions.Answer(value=None, probability=0, backend="rules")
    )
    audit.log_answers([rec], log_dir=blocker / "sub")  # parent is a file


def test_ask_many_result_unchanged_when_logging_breaks(monkeypatch):
    monkeypatch.setattr(decisions, "log_answers", lambda *a, **k: 1 / 0)
    answers = decisions.ask_many([(BoolQuestion(prompt="k"), {})])
    assert len(answers) == 1
