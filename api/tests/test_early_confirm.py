"""Early-game vision confirm: schedule, labelling call, runs, merge, failure (AVE-56)."""

import json

import pytest

from ai_video_editor.league.early_game import EarlySegment
from ai_video_editor.vlm import early_confirm as ec
from ai_video_editor.vlm.backends.base import VLMUnavailableError


class FakeBackend:
    name = "fake"

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def call(self, *, frames, system_prompt, user_prompt, max_tokens=1024):
        self.calls.append({"frames": frames, "system": system_prompt, "max": max_tokens})
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _reply(states: list[str], step: int = 2) -> str:
    return json.dumps({"frames": [{"second": i * step, "state": s} for i, s in enumerate(states)]})


@pytest.fixture
def sampled(tmp_path, monkeypatch):
    seen = []

    def fake_sample(video, out_dir, *, start_s, end_s, fps):
        seen.append((start_s, end_s, fps))
        f = tmp_path / "f.jpg"
        f.write_bytes(b"x")
        return [f]

    monkeypatch.setattr(ec, "sample_window", fake_sample)
    monkeypatch.setattr(ec, "get_duration_seconds", lambda _p: 3000.0)
    return seen


def _seg(kind, a, b, confirmed=False):
    return EarlySegment(kind=kind, start_s=a, end_s=b, reason="r", confirmed=confirmed)


def test_states_pinned():
    assert ec.EARLY_STATES == (
        "laning", "trade", "bush_fight", "low_hp", "recall", "jungle", "dead"
    )  # fmt: skip


def test_schedule_45_to_first_back_plus_8(sampled):
    backend = FakeBackend(_reply(["laning"]))
    ec.confirm_early("v.mp4", [], first_back_s=200.0, backend=backend)
    assert sampled == [(45.0, 208.0, 0.5)]
    assert len(backend.calls) == 1
    assert "bush_fight" in backend.calls[0]["system"]


def test_schedule_capped_at_120_frames(sampled):
    ec.confirm_early("v.mp4", [], first_back_s=2000.0, backend=FakeBackend(_reply(["laning"])))
    assert sampled[0][1] - sampled[0][0] == pytest.approx(240.0)


def test_uncovered_runs_become_confirmed_segments(sampled):
    # frames every 2s from 45: bush_fight at idx 2-3 (49-51), trade at idx 6 (57)
    states = ["laning"] * 2 + ["bush_fight"] * 2 + ["laning"] * 2 + ["trade"] + ["laning"]
    out = ec.confirm_early(
        "v.mp4",
        [_seg("first_back", 190, 210)],
        first_back_s=200.0,
        backend=FakeBackend(_reply(states)),
    )
    new = [s for s in out if s.kind != "first_back"]
    assert [(s.kind, s.start_s, s.end_s, s.confirmed) for s in new] == [
        ("opening", 46.0, 54.0, True),
        ("level3_trade", 54.0, 60.0, True),
    ]


def test_low_hp_run_kind(sampled):
    out = ec.confirm_early(
        "v.mp4", [], first_back_s=200.0, backend=FakeBackend(_reply(["laning", "low_hp"]))
    )
    assert [(s.kind, s.confirmed) for s in out] == [("low_hp", True)]


def test_late_bush_fight_is_not_opening(sampled):
    states = ["laning"] * 40 + ["bush_fight"]  # rec 125, opening start 33
    out = ec.confirm_early(
        "v.mp4",
        [_seg("opening", 33, 40)],
        first_back_s=200.0,
        backend=FakeBackend(_reply(states)),
    )
    assert [s.kind for s in out if s.start_s > 100] == ["level3_trade"]


def test_existing_overlap_confirmed_and_trimmed(sampled):
    states = ["laning"] * 10 + ["trade"] * 2 + ["laning"] * 3  # rec 65-67
    out = ec.confirm_early(
        "v.mp4",
        [_seg("level3_trade", 50, 100)],
        first_back_s=200.0,
        backend=FakeBackend(_reply(states)),
    )
    assert [(s.kind, s.start_s, s.end_s, s.confirmed) for s in out] == [
        ("level3_trade", 62.0, 70.0, True)
    ]


def test_first_back_confirmed_not_trimmed(sampled):
    states = ["laning"] * 70 + ["low_hp"]  # rec 185
    seg = _seg("first_back", 183, 208)
    out = ec.confirm_early("v.mp4", [seg], first_back_s=200.0, backend=FakeBackend(_reply(states)))
    assert (out[0].start_s, out[0].end_s, out[0].confirmed) == (183, 208, True)


def test_unavailable_returns_input_unchanged(sampled, caplog):
    segs = [_seg("opening", 33, 103)]
    out = ec.confirm_early(
        "v.mp4", segs, first_back_s=200.0, backend=FakeBackend(VLMUnavailableError("down"))
    )
    assert out == segs and not out[0].confirmed
    assert "early confirm" in caplog.text


def test_garbage_reply_returns_input(sampled):
    segs = [_seg("opening", 33, 103)]
    assert ec.confirm_early("v.mp4", segs, first_back_s=200.0, backend=FakeBackend("nope")) == segs


def test_no_frames_returns_input(monkeypatch):
    monkeypatch.setattr(ec, "sample_window", lambda *a, **k: [])
    monkeypatch.setattr(ec, "get_duration_seconds", lambda _p: 3000.0)
    segs = [_seg("opening", 33, 103)]
    assert ec.confirm_early("v.mp4", segs, first_back_s=200.0, backend=FakeBackend("{}")) == segs
