"""VLM play confirmer: pure window conversion, fake-backend confirm and sweep (Task 3)."""

import json

import pytest

from ai_video_editor.league import plays as plays_mod  # noqa: F401
from ai_video_editor.league.play_models import (
    Play,
    PlayFlags,
    PlayOwner,
    PlayResult,
    PlaySides,
    PlayTimes,
    PlayWindow,
)
from ai_video_editor.league.play_vocab import load_vocab
from ai_video_editor.vlm import play_confirm as pc
from ai_video_editor.vlm.backends.base import VLMUnavailableError

FIRST, LAST = 500.0, 510.0
VOCAB = load_vocab("league")
RULE = VOCAB.rules["skirmish"]


def _play(involved: bool = True, ptype: str = "skirmish") -> Play:
    return Play(
        id="p01",
        type=ptype,
        phase="mid",
        game_time=PlayTimes(first_kill_s=FIRST - 20, last_kill_s=LAST - 20),
        recording_time=PlayTimes(first_kill_s=FIRST, last_kill_s=LAST),
        window=PlayWindow(),
        zone="mid_lane",
        zone_relative="mid_lane",
        sides=PlaySides(allies=["Jhin"], enemies=["Zed"], numbers="1v1"),
        owner=PlayOwner(involved=involved, role="initiator_kill", kills=1, assists=0, died=False),
        result=PlayResult(enemy_deaths=1, ally_deaths=0, outcome="won"),
        flags=PlayFlags(help_from_team=False),
    )


def _conf(**kw) -> pc.PlayConfirmation:
    base = {"approach_s": 22.0, "engage_s": 28.0, "outcome_s": 42.0, "description": "d",
            "confidence": 0.8}  # fmt: skip
    base.update(kw)
    return pc.PlayConfirmation(**base)


START = FIRST - 30


def _apply(conf, play=None, game_end=None):
    return pc.apply_confirmation(play or _play(), conf, START, game_end_s=game_end, rule=RULE)


def test_window_from_confirmation_in_recording_seconds():
    w = _apply(_conf()).window
    assert (w.approach_s, w.engage_s, w.outcome_s) == (START + 22, START + 28, START + 42)
    assert w.source == "vlm"


def test_banner_offset_correction_stored():
    p = _apply(_conf(first_banner_s=31.5))
    assert p.window.offset_correction_s == pytest.approx(1.5)
    assert "offset_uncertain" not in p.window.flags


def test_banner_missing_flags_offset_uncertain():
    p = _apply(_conf(first_banner_s=None))
    assert p.window.offset_correction_s is None
    assert "offset_uncertain" in p.window.flags


def test_banner_far_from_riot_kill_flags_offset_uncertain():
    p = _apply(_conf(first_banner_s=45.0))
    assert p.window.offset_correction_s is None
    assert "offset_uncertain" in p.window.flags


def test_missing_approach_falls_back_8s_and_flags():
    p = _apply(_conf(approach_s=None, engage_s=None))
    assert p.window.approach_s == FIRST - 8
    assert p.window.engage_s == FIRST - 8
    assert p.window.source == "fallback"
    assert "approach_unconfirmed" in p.window.flags


def test_engage_defaults_to_approach():
    assert _apply(_conf(engage_s=None)).window.engage_s == START + 22


def test_outcome_before_engage_rejected():
    p = _apply(_conf(outcome_s=20.0))
    assert p.window.outcome_s == LAST + 3


def test_outcome_clamped_to_rule_max_after_last_kill():
    p = _apply(_conf(outcome_s=65.0))
    assert p.window.outcome_s == LAST + float(RULE.end["max_after_last_kill_s"])


def test_outcome_clamped_to_game_end_plus_one():
    p = _apply(_conf(outcome_s=50.0), game_end=LAST + 1.0)
    assert p.window.outcome_s == LAST + 2.0


def test_none_confirmation_is_fallback_window():
    p = _apply(None)
    assert p.window.source == "fallback"
    assert p.window.approach_s == FIRST - 8
    assert "approach_unconfirmed" in p.window.flags
    assert p.window.outcome_s == LAST + 3


def test_numbers_confirmed_only_with_both_counts():
    one = _apply(_conf(allies_seen=3, enemies_seen=None)).sides
    assert one.numbers_confirmed is None and one.numbers_source == "riot_lower_bound"
    both = _apply(_conf(allies_seen=3, enemies_seen=5)).sides
    assert both.numbers_confirmed == "3v5" and both.numbers_source == "vlm_confirmed"


def test_numbers_faced_uses_confirmed_counts():
    assert _apply(_conf(allies_seen=3, enemies_seen=5)).impressive.numbers_faced == 3
    absent = _apply(_conf(allies_seen=3, enemies_seen=5), play=_play(involved=False))
    assert absent.impressive.numbers_faced is None


def test_flank_copied():
    assert _apply(_conf(flank=True)).flags.flank is True


# --- fake backend -----------------------------------------------------------


class FakeBackend:
    name = "fake"

    def __init__(self, reply: str | Exception):
        self.reply = reply
        self.calls: list[dict] = []

    def call(self, *, frames, system_prompt, user_prompt):
        self.calls.append({"frames": frames, "system": system_prompt, "user": user_prompt})
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture
def fake_frames(tmp_path, monkeypatch):
    a, b = tmp_path / "m1.jpg", tmp_path / "m2.jpg"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    seen = []

    def fake_sample(video, out_dir, *, start_s, end_s, fps):
        seen.append((start_s, end_s, fps))
        return [a, b]

    monkeypatch.setattr(pc, "sample_window", fake_sample)
    monkeypatch.setattr(pc, "get_duration_seconds", lambda _p: 1000.0)
    return seen


def test_confirm_play_calls_backend_once_with_rule_prompt(fake_frames):
    backend = FakeBackend(json.dumps(_conf().model_dump()))
    conf, start = pc.confirm_play("v.mp4", _play(), vocab=VOCAB, backend=backend, fps=1.0)
    assert conf is not None and conf.approach_s == 22.0
    assert start == FIRST - 30.0
    assert len(backend.calls) == 1
    assert RULE.end["rule"] in backend.calls[0]["system"]
    assert "skirmish" in backend.calls[0]["user"]
    assert fake_frames == [(FIRST - 30.0, LAST + 20.0, 1.0)]


def test_confirm_play_clamps_window_to_recording(fake_frames, monkeypatch):
    monkeypatch.setattr(pc, "get_duration_seconds", lambda _p: 515.0)
    p = _play()
    p.recording_time = PlayTimes(first_kill_s=10.0, last_kill_s=510.0)
    pc.confirm_play("v.mp4", p, vocab=VOCAB, backend=FakeBackend(_conf().model_dump_json()), fps=1)
    assert fake_frames == [(0.0, 515.0, 1)]


def test_confirm_play_returns_none_on_unavailable(fake_frames):
    backend = FakeBackend(VLMUnavailableError("down"))
    conf, start = pc.confirm_play("v.mp4", _play(), vocab=VOCAB, backend=backend, fps=1.0)
    assert conf is None and start == FIRST - 30.0


def test_confirm_play_returns_none_on_garbage(fake_frames):
    conf, _ = pc.confirm_play(
        "v.mp4", _play(), vocab=VOCAB, backend=FakeBackend("not json"), fps=1.0
    )
    assert conf is None


def test_sweep_findings_become_trade_plays_above_confidence(fake_frames):
    reply = json.dumps(
        {
            "findings": [
                {"t_s": 100.0, "type": "trade", "description": "poke war", "confidence": 0.8},
                {"t_s": 200.0, "type": "caught_out", "description": "?", "confidence": 0.4},
            ]
        }
    )
    backend = FakeBackend(reply)
    findings = pc.sweep_game("v.mp4", duration_s=300.0, fps=0.25, backend=backend, vocab=VOCAB)
    assert len(findings) == 2 and len(backend.calls) == 1
    plays = pc.findings_to_plays(findings, start_idx=5, offset_s=-20.0, phases=None)
    assert [p.type for p in plays] == ["trade"]
    p = plays[0]
    assert p.id == "p05" and p.sides.numbers == "?v?" and p.window.source == "vlm"
    assert (p.window.approach_s, p.window.outcome_s) == (90.0, 108.0)
    assert p.recording_time.first_kill_s == 100.0 and p.game_time.first_kill_s == 120.0


def test_late_engage_falls_back_to_approach_and_keeps_window_ordered():
    p = _apply(_conf(approach_s=22.0, engage_s=60.0, outcome_s=65.0))
    w = p.window
    assert w.engage_s == w.approach_s == START + 22
    assert "engage_unconfirmed" in w.flags
    assert w.approach_s <= w.engage_s < w.outcome_s


def test_early_engage_falls_back_to_approach():
    p = _apply(_conf(approach_s=22.0, engage_s=10.0))
    assert p.window.engage_s == START + 22
    assert "engage_unconfirmed" in p.window.flags


def test_sweep_stops_when_should_continue_flips(fake_frames):
    reply = json.dumps({"findings": []})
    backend = FakeBackend(reply)
    ticks = iter([True, False])
    pc.sweep_game(
        "v.mp4",
        duration_s=2000.0,
        fps=0.25,
        backend=backend,
        vocab=VOCAB,
        should_continue=lambda: next(ticks),
    )
    assert len(backend.calls) == 1
