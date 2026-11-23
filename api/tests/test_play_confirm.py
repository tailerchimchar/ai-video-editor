"""VLM play confirmer: pure window conversion, fake-backend confirm and sweep (Task 3)."""

import json
from pathlib import Path

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


def test_outcome_past_rule_max_after_last_kill_falls_back():
    p = _apply(_conf(outcome_s=65.0))
    assert p.window.outcome_s == LAST + 3
    assert "outcome_unconfirmed" in p.window.flags


def test_outcome_before_last_kill_falls_back():
    # 505 is after engage but before the last kill (510): the fight is not over.
    p = _apply(_conf(outcome_s=35.0))
    assert p.window.outcome_s == LAST + 3
    assert "outcome_unconfirmed" in p.window.flags


def test_approach_before_window_start_falls_back():
    p = _apply(_conf(approach_s=-2.0))
    assert p.window.approach_s == FIRST - 8
    assert "approach_unconfirmed" in p.window.flags


def test_approach_at_or_after_first_kill_falls_back():
    p = _apply(_conf(approach_s=30.0, engage_s=None))
    assert p.window.approach_s == FIRST - 8
    assert "approach_unconfirmed" in p.window.flags


def test_engage_after_first_kill_falls_back_to_approach():
    # engage at 505: before the last kill but after the first one.
    p = _apply(_conf(approach_s=22.0, engage_s=35.0))
    assert p.window.engage_s == START + 22
    assert "engage_unconfirmed" in p.window.flags


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

    def call(self, *, frames, system_prompt, user_prompt, max_tokens=1024):
        self.calls.append(
            {"frames": frames, "system": system_prompt, "user": user_prompt, "max": max_tokens}
        )
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
    monkeypatch.setattr(pc, "sample_panels", fake_sample)
    monkeypatch.setattr(pc, "get_duration_seconds", lambda _p: 1000.0)
    return seen


def _labels(states: str, banner_from: int | None = None, step: int = 2) -> str:
    """One label per char: n=none a=approach f=fighting x=aftermath, every `step` s."""
    names = {"n": "none", "a": "approach", "f": "fighting", "x": "aftermath"}
    frames = [
        {
            "second": i * step,
            "state": names[c],
            "kill_banner": banner_from is not None and i * step >= banner_from and c == "f",
            "allies_visible": 1 + (c == "f") * i % 3,
            "enemies_visible": 2 if c == "f" else 0,
        }
        for i, c in enumerate(states)
    ]
    return json.dumps({"frames": frames, "flank": True, "description": "d"})


def test_confirm_play_labels_frames_and_derives_window(fake_frames):
    # window 470..530: approach 12-20, fighting 22-44, aftermath 46+; kills at 30 and 40.
    backend = FakeBackend(_labels("nnnnnnaaaaffffffffffffxxxxxxxxx", banner_from=28))
    conf, start = pc.confirm_play("v.mp4", _play(), vocab=VOCAB, backend=backend, fps=0.5)
    assert start == FIRST - 30.0
    assert (conf.approach_s, conf.engage_s, conf.outcome_s) == (12.0, 20.0, 44.0)
    assert conf.first_banner_s == 28.0
    assert conf.flank is True and conf.description == "d"
    assert len(backend.calls) == 1
    call = backend.calls[0]
    assert call["max"] == 4096
    assert RULE.end["rule"] in call["system"]
    assert "Label EVERY frame" in call["system"] and "burned in" in call["system"]
    assert "skirmish" in call["user"]
    assert fake_frames == [(FIRST - 30.0, LAST + 20.0, 0.5)]


def test_confirm_play_default_fps_from_settings(fake_frames, monkeypatch):
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "play_confirm_fps", 0.5)
    pc.confirm_play("v.mp4", _play(), vocab=VOCAB, backend=FakeBackend(_labels("ff")))
    assert fake_frames[0][2] == 0.5


def test_confirm_play_lets_spend_cap_through(fake_frames):
    from ai_video_editor.anthropic_surface import SpendCapError

    with pytest.raises(SpendCapError):
        pc.confirm_play(
            "v.mp4", _play(), vocab=VOCAB, backend=FakeBackend(SpendCapError("cap")), fps=1.0
        )


def test_confirm_play_without_skirmish_rule_does_not_raise(fake_frames):
    vocab = VOCAB.model_copy(update={"rules": {}})
    backend = FakeBackend(_labels("ff"))
    conf, start = pc.confirm_play("v.mp4", _play(), vocab=vocab, backend=backend, fps=1.0)
    assert conf is None and start == FIRST - 30.0
    assert backend.calls == []


def test_vocab_rule_for_without_skirmish_raises_clear_error():
    with pytest.raises(ValueError, match="vocabulary has no skirmish rule"):
        VOCAB.model_copy(update={"rules": {}}).rule_for("dive")


def test_sweep_stops_on_spend_cap_and_reports(fake_frames):
    from ai_video_editor.anthropic_surface import SpendCapError

    hit = []
    findings = pc.sweep_game(
        "v.mp4",
        duration_s=2000.0,
        fps=0.25,
        backend=FakeBackend(SpendCapError("cap")),
        vocab=VOCAB,
        on_budget_exhausted=lambda: hit.append(1),
    )
    assert findings == [] and hit == [1]


# --- derive_confirmation (p15 ground truth) ---------------------------------


def _fl(states: str, *, banner: set[int] = frozenset(), step: int = 2, counts=None):
    names = {"n": "none", "a": "approach", "f": "fighting", "x": "aftermath"}
    return pc.FrameLabels(
        frames=[
            pc.FrameLabel(
                second=i * step,
                state=names[c],
                kill_banner=i * step in banner,
                allies_visible=(counts or {}).get(i * step, (1, 0))[0],
                enemies_visible=(counts or {}).get(i * step, (1, 0))[1],
            )
            for i, c in enumerate(states)
        ],
        flank=False,
        description="teamfight",
    )


# 0..50 s every 2 s: none 0-10, approach 12-20, fighting 22-44, aftermath 46-50.
P15 = "nnnnnnaaaaaffffffffffffxxx"


def test_derive_matches_p15_ground_truth():
    labels = _fl(P15, banner={28, 30, 40}, counts={24: (4, 3), 30: (5, 4), 46: (5, 0)})
    conf = pc.derive_confirmation(labels, first_kill_rel_s=30.0, last_kill_rel_s=44.5, window_s=50)
    assert conf.approach_s == 12.0
    assert conf.engage_s == 22.0
    assert conf.outcome_s == 46.0
    assert conf.first_banner_s == 28.0
    assert (conf.allies_seen, conf.enemies_seen) == (5, 4)
    assert conf.flank is False and conf.description == "teamfight"


def test_derive_engage_run_contains_kill_one_step_after_its_end():
    # fighting 10-14, kill at 15.5 (<= 14 + step 2) still belongs to that run.
    conf = pc.derive_confirmation(
        _fl("aaaaafffnnn"), first_kill_rel_s=15.5, last_kill_rel_s=15.5, window_s=20
    )
    assert conf.engage_s == 10.0 and conf.approach_s == 0.0 and conf.outcome_s == 16.0


def test_derive_engage_falls_back_to_last_run_before_kill():
    # runs at 2-4 and 10-12; kill at 20 is in neither: the later one wins.
    conf = pc.derive_confirmation(
        _fl("affnnfffnnnnn"), first_kill_rel_s=20.0, last_kill_rel_s=20.0, window_s=24
    )
    assert conf.engage_s == 10.0


def test_derive_engage_none_without_fighting_before_kill():
    conf = pc.derive_confirmation(
        _fl("aaaaaff"), first_kill_rel_s=4.0, last_kill_rel_s=4.0, window_s=12
    )
    assert conf.engage_s is None and conf.approach_s is None
    assert conf.allies_seen is None


def test_derive_approach_allows_one_none_frame_gap():
    one = pc.derive_confirmation(
        _fl("aaanfff"), first_kill_rel_s=9.0, last_kill_rel_s=9.0, window_s=12
    )
    assert one.approach_s == 0.0
    two = pc.derive_confirmation(
        _fl("aaannfff"), first_kill_rel_s=11.0, last_kill_rel_s=11.0, window_s=14
    )
    assert two.approach_s is None


def test_derive_outcome_none_when_fight_never_ends():
    conf = pc.derive_confirmation(
        _fl("aaffff"), first_kill_rel_s=5.0, last_kill_rel_s=8.0, window_s=10
    )
    assert conf.outcome_s is None


def test_derive_banner_before_engage_ignored():
    conf = pc.derive_confirmation(
        _fl("ffnnaaffff", banner={0, 14}), first_kill_rel_s=13.0, last_kill_rel_s=13.0,
        window_s=18,
    )  # fmt: skip
    assert conf.engage_s == 12.0 and conf.first_banner_s == 14.0


# --- ffmpeg argv --------------------------------------------------------------


def _capture_ffmpeg(monkeypatch, tmp_path):
    seen = []

    def fake_run(cmd, **_kw):
        seen.append(cmd)
        out = Path(cmd[-1]).parent
        for i in (1, 2, 3):
            (out / f"f_{i:04d}.jpg").write_bytes(b"x")

    monkeypatch.setattr(pc.subprocess, "run", fake_run)
    return seen


def test_sample_window_single_960px_frames_with_burned_second(monkeypatch, tmp_path):
    font = tmp_path / "arial.ttf"
    font.write_bytes(b"x")
    monkeypatch.setattr(pc, "FONT_PATH", font)
    seen = _capture_ffmpeg(monkeypatch, tmp_path)
    frames = pc.sample_window("v.mp4", tmp_path / "o", start_s=10.0, end_s=40.0, fps=0.5)
    assert [f.name for f in frames] == ["f_0001.jpg", "f_0002.jpg", "f_0003.jpg"]
    cmd = seen[0]
    vf = cmd[cmd.index("-vf") + 1]
    assert vf == (
        "fps=0.5,scale=960:-2,drawtext=fontfile='C\\:/Windows/Fonts/arial.ttf':"
        "text='%{eif\\:t\\:d}s':x=10:y=10:fontsize=44:fontcolor=yellow:box=1:"
        "boxcolor=black@0.7"
    )
    assert cmd[cmd.index("-ss") + 1] == "10.000" and cmd[cmd.index("-t") + 1] == "30.000"


def test_sample_window_without_font_file_omits_fontfile(monkeypatch, tmp_path):
    monkeypatch.setattr(pc, "FONT_PATH", tmp_path / "missing.ttf")
    seen = _capture_ffmpeg(monkeypatch, tmp_path)
    pc.sample_window("v.mp4", tmp_path / "o", start_s=0.0, end_s=4.0, fps=0.5)
    vf = seen[0][seen[0].index("-vf") + 1]
    assert "fontfile" not in vf and "drawtext=text=" in vf


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
