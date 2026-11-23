from pathlib import Path

from ai_video_editor.league import plays_eval_cli
from ai_video_editor.league.play_models import (
    GameModel,
    Play,
    PlayFlags,
    PlayOwner,
    PlayResult,
    PlaySides,
    PlayTimes,
    PlayWindow,
)
from ai_video_editor.league.plays_eval import LabelRow, load_labels, match_plays, passes_gate

FIXTURE = Path(__file__).parent.parent / "fixtures" / "plays" / "labels_example.json"


def _play(pid: str, start: float, end: float, type_: str = "skirmish") -> Play:
    return Play(
        id=pid,
        type=type_,
        phase="mid",
        game_time=PlayTimes(first_kill_s=start, last_kill_s=end),
        recording_time=PlayTimes(first_kill_s=start + 8, last_kill_s=end - 3),
        window=PlayWindow(approach_s=start, outcome_s=end, source="vlm"),
        zone="mid_lane",
        sides=PlaySides(allies=["A"], enemies=["B"], numbers="1v1"),
        owner=PlayOwner(involved=True, role="initiator_kill", kills=1, assists=0, died=False),
        result=PlayResult(enemy_deaths=1, ally_deaths=0, outcome="won"),
        flags=PlayFlags(help_from_team=False),
    )


def _label(start: float, end: float, type_: str = "skirmish") -> LabelRow:
    return LabelRow(type=type_, approach_s=start, engage_s=start + 5, outcome_s=end)


def test_match_by_iou_and_start_tolerance():
    r = match_plays([_play("p1", 102, 120)], [_label(100, 120)])
    assert r.matched == [("p1", 0)]
    assert r.precision == 1.0 and r.recall == 1.0


def test_label_matches_at_most_one_play():
    r = match_plays([_play("a", 100, 120), _play("b", 101, 121)], [_label(100, 120)])
    assert r.matched == [("a", 0)]
    assert r.unmatched_detected == ["b"]
    assert r.precision == 0.5 and r.recall == 1.0


def test_start_off_by_six_seconds_is_unmatched():
    r = match_plays([_play("p", 106, 130)], [_label(100, 130)])
    assert r.matched == []
    assert r.unmatched_detected == ["p"] and r.unmatched_labels == [0]
    assert r.precision == 0.0 and r.recall == 0.0


def test_type_confusion_counted_when_window_matches_but_type_differs():
    r = match_plays([_play("p", 100, 120, "gank")], [_label(100, 120, "skirmish")])
    assert r.matched == [("p", 0)]
    assert r.precision == 1.0
    assert r.type_confusion == [("skirmish", "gank")]
    assert r.per_type["gank"]["precision"] == 0.0
    assert r.per_type["skirmish"]["recall"] == 0.0


def test_gate_at_0_8():
    labels = [_label(100 + 100 * i, 120 + 100 * i) for i in range(5)]
    dets = [_play(f"p{i}", 100 + 100 * i, 120 + 100 * i) for i in range(4)]
    dets.append(_play("junk", 900, 910))
    assert match_plays(dets, labels).precision == 0.8
    assert passes_gate(match_plays(dets, labels))
    dets.append(_play("junk2", 950, 960))
    assert not passes_gate(match_plays(dets, labels))


def test_labels_example_fixture_loads():
    labels = load_labels(FIXTURE)
    assert len(labels.rows) == 10
    assert labels.rows[0].approach_s < labels.rows[0].outcome_s


def test_fallback_window_from_kill_times():
    p = _play("p", 100, 120)
    p.window = PlayWindow()  # no VLM window: first_kill-8 .. last_kill+3
    r = match_plays([p], [_label(100, 120)])
    assert r.matched == [("p", 0)]


def test_cli_fps_harness_prints_line_per_fps(tmp_path, monkeypatch, capsys):
    import sys
    import types

    from ai_video_editor.league import riot_timeline

    labels = tmp_path / "labels.json"
    labels.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(
        riot_timeline,
        "load_cached",
        lambda _d: ({}, {}, {"owner_puuid": "x", "confidence": "high"}),
    )
    monkeypatch.setattr(riot_timeline, "parse_timeline", lambda *a, **k: object())
    calls = []

    def fake(asset, src, tl, **kw):
        calls.append(kw["fps"])
        plays = [_play("p0", 60, 74)]
        return GameModel(
            plays=plays, budget_spent_usd=0.25 * kw["fps"], budget_exhausted=kw["fps"] == 1.0
        )

    stub = types.ModuleType("ai_video_editor.league.understand")
    stub.understand_game = fake
    monkeypatch.setitem(sys.modules, "ai_video_editor.league.understand", stub)
    rc = plays_eval_cli.main(
        ["--asset", "a", "--labels", str(labels), "--fps", "0.25,1.0", "--source", "x.mp4"]
    )
    out = capsys.readouterr().out.splitlines()
    assert rc == 0 and calls == [0.25, 1.0]
    assert out[0].startswith("fps 0.25") and "precision 1.00" in out[0] and "PASS" in out[0]
    assert out[1].startswith("fps 1.0") and out[1].endswith("budget_exhausted")
    assert "budget_exhausted" not in out[0]


def test_absent_plays_are_not_scored():
    absent = _play("p9", 300, 320)
    absent.owner = PlayOwner(involved=False, role="absent", kills=0, assists=0, died=False)
    r = match_plays([_play("p1", 100, 120), absent], [_label(100, 120)])
    assert r.precision == 1.0 and r.recall == 1.0
    assert r.excluded_absent == ["p9"]
    assert r.unmatched_detected == []
    assert r.per_type["skirmish"]["n_detected"] == 1.0


def test_cli_prints_drive_cuts_line_only_when_gate_passes(tmp_path, monkeypatch, capsys):
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    labels = tmp_path / "labels.json"
    labels.write_text(
        '{"asset_id": "a", "rows": [{"type": "skirmish", "approach_s": 60, '
        '"engage_s": 65, "outcome_s": 74}]}',
        encoding="utf-8",
    )
    cache = tmp_path / "plays" / "a.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(GameModel(plays=[_play("p0", 60, 74)]).model_dump_json(), encoding="utf-8")
    assert plays_eval_cli.main(["--asset", "a", "--labels", str(labels)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "gate (>=0.8): PASS" in out and "PLAYS_DRIVE_CUTS=true" in out

    cache.write_text(GameModel(plays=[_play("p0", 200, 230)]).model_dump_json(), encoding="utf-8")
    plays_eval_cli.main(["--asset", "a", "--labels", str(labels)])
    out = capsys.readouterr().out.splitlines()
    assert "gate (>=0.8): FAIL" in out and "PLAYS_DRIVE_CUTS=true" not in out
