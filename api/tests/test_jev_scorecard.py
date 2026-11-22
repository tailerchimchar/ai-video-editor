"""Scorecard on invented records and corrections; main() reads files."""

from __future__ import annotations

import json

from ai_video_editor.decisions.audit import AnswerRecord
from ai_video_editor.decisions.scorecard import main, score


def rec(question, value, ref, backend="jev"):
    return AnswerRecord("2026-01-01T00:00:00+00:00", backend, question, "h", {}, value, 0.9, ref)


CORRECTIONS = [
    {"rule": "x1 is not a highlight", "tags": [], "clip_ref": "x1"},
    {"rule": "y", "tags": ["no-event"], "clip_ref": "x2"},
    {"rule": "clip f1 is a funny short", "tags": [], "clip_ref": None},
    {"rule": "f2 format is story", "tags": [], "clip_ref": None},
]


def test_keep_precision_and_disagreements():
    records = [
        rec("keep?", True, "x1"),
        rec("keep?", True, "x2"),
        rec("keep?", True, "ok"),
        rec("keep?", True, "ok2"),
        rec("keep?", False, "x1"),
    ]
    card = score(records, CORRECTIONS)
    assert card.keep_precision == 0.5
    assert {d["clip_ref"] for d in card.disagreements} == {"x1", "x2"}
    assert card.n_records == 5 and card.n_corrections == 4


def test_format_agreement():
    records = [rec("fmt?", "funny", "f1"), rec("fmt?", "hype", "f2"), rec("fmt?", "hype", "zz")]
    card = score(records, CORRECTIONS)
    assert card.format_agreement == 0.5
    assert [d for d in card.disagreements if d["kind"] == "format"] == [
        {"kind": "format", "clip_ref": "f2", "jev": "hype", "owner": "story"}
    ]


def test_none_when_nothing_to_compare():
    card = score([], [])
    assert card.keep_precision is None and card.format_agreement is None


def test_main_prints_table(tmp_path, capsys):
    logs, hints = tmp_path / "d", tmp_path / "h"
    logs.mkdir()
    hints.mkdir()
    r = rec("keep?", True, "x1")
    (logs / "jev-2026-01-01.jsonl").write_text(json.dumps(r.__dict__) + "\n", encoding="utf-8")
    (logs / "jev-2026-01-02.jsonl").write_text(
        json.dumps(rec("keep?", True, "q", backend="rules").__dict__) + "\n", encoding="utf-8"
    )
    (hints / "league_examples.jsonl").write_text(
        json.dumps(CORRECTIONS[0]) + "\n", encoding="utf-8"
    )
    assert main(["--log-dir", str(logs), "--hints-dir", str(hints)]) == 0
    out = capsys.readouterr().out
    assert "Keep precision:       0%" in out and "x1" in out
    assert "scored:   1" in out


def test_file_name_corrections_match_bare_refs():
    correction = {
        "rule": "callout and assist are not events",
        "tags": ["no-event"],
        "clip_ref": "02_hype_callout_6m33s.mp4; 04_assist_9m41s.mp4",
    }
    records = [rec("keep?", True, "6m33s"), rec("keep?", True, "9m41s")]
    card = score(records, [correction])
    assert card.keep_precision == 0.0
    assert {d["clip_ref"] for d in card.disagreements} == {"6m33s", "9m41s"}


def test_round_joins_when_both_sides_have_one():
    correction = {"rule": "r", "tags": ["no-event"], "clip_ref": "01_kill_6m33s.mp4", "round": "a1"}
    same = rec("keep?", True, "6m33s")
    same.round = "a1"
    other = rec("keep?", True, "6m33s")
    other.round = "b2"
    unknown = rec("keep?", True, "6m33s")
    card = score([same, other, unknown], [correction])
    assert len(card.disagreements) == 2
    assert card.keep_precision == 1 / 3


def test_make_record_carries_round():
    from types import SimpleNamespace

    from ai_video_editor.decisions.audit import make_record

    ans = SimpleNamespace(value=True, probability=0.8)
    assert make_record("jev", "q", {"clip": {"ref": "6m33s", "round": "a1"}}, ans).round == "a1"
    assert make_record("jev", "q", {"round": 7, "clip": {"ref": "x"}}, ans).round == "7"
    assert make_record("jev", "q", {"clip": {"ref": "x"}}, ans).round is None
