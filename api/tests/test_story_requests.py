"""Owner requests file: clock parsing, load/save, and applying them to a plan."""

from __future__ import annotations

import json

import pytest

from ai_video_editor.league.riot_timeline import RiotTimeline, TimelineEvent
from ai_video_editor.story.planner import Chapter, StoryPlan
from ai_video_editor.story.requests import (
    REQUESTS_FILE,
    AddChapter,
    Drop,
    Effect,
    Extend,
    StoryRequests,
    apply_requests,
    game_clock_s,
    load_requests,
    save_requests,
)


@pytest.mark.parametrize(
    ("raw", "want"),
    [("20:05", 1205.0), ("1:02:05", 3725.0), (75, 75.0), (12.5, 12.5), ("75", 75.0)],
)
def test_game_clock_s(raw, want):
    assert game_clock_s(raw) == want


@pytest.mark.parametrize("raw", ["junk", "", "1:2:3:4", "20:75", "-5", -3, True, None])
def test_game_clock_s_rejects_junk(raw):
    with pytest.raises(ValueError):
        game_clock_s(raw)


def test_load_missing_file_is_empty(tmp_path):
    assert load_requests(tmp_path).requests == []


def test_save_then_load_roundtrip(tmp_path):
    reqs = StoryRequests(requests=[AddChapter(at="20:05", title="IE"), Drop(at=30)])
    save_requests(tmp_path, reqs)
    assert (tmp_path / REQUESTS_FILE).read_text(encoding="utf-8").count("\n") > 3  # pretty
    assert load_requests(tmp_path) == reqs


def test_load_parses_each_kind(tmp_path):
    (tmp_path / REQUESTS_FILE).write_text(
        json.dumps(
            {
                "requests": [
                    {"kind": "add_chapter", "at": "20:05"},
                    {"kind": "extend", "at": 100, "after_s": 3},
                    {"kind": "drop", "at": "1:00"},
                    {"kind": "effect", "effect": "slowmo", "at": 5, "params": {"factor": 0.4}},
                ]
            }
        ),
        encoding="utf-8",
    )
    kinds = [type(r) for r in load_requests(tmp_path).requests]
    assert kinds == [AddChapter, Extend, Drop, Effect]


def test_load_invalid_names_the_bad_entry(tmp_path):
    (tmp_path / REQUESTS_FILE).write_text(
        json.dumps({"requests": [{"kind": "drop", "at": "1:00"}, {"kind": "bogus", "at": 1}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"request #2"):
        load_requests(tmp_path)


def test_load_bad_clock_names_the_bad_entry(tmp_path):
    (tmp_path / REQUESTS_FILE).write_text(
        json.dumps({"requests": [{"kind": "drop", "at": "soon"}]}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match=r"request #1"):
        load_requests(tmp_path)


def test_load_not_json_raises(tmp_path):
    (tmp_path / REQUESTS_FILE).write_text("{nope", encoding="utf-8")
    with pytest.raises(ValueError, match=REQUESTS_FILE):
        load_requests(tmp_path)


# ---- apply ----------------------------------------------------------------

OFFSET = -21.6


def _tl(events=(), offset=OFFSET, game_end=None) -> RiotTimeline:
    return RiotTimeline("NA1_1", "high", offset, game_end, list(events), [])


def _ev(kind, rec) -> TimelineEvent:
    return TimelineEvent(kind, rec - OFFSET, rec, [], {})


def _plan(chapters, *, cards=False, card_s=0.0) -> StoryPlan:
    total = sum(c.end_s - c.start_s + card_s for c in chapters)
    return StoryPlan(
        asset_id="a1", chapters=chapters, total_s=total, mic_stream=1, audio="mic_over_game",
        cards=cards,
    )  # fmt: skip


def _ch(start, end, title="T", kind="fight", **kw) -> Chapter:
    return Chapter(title=title, start_s=start, end_s=end, kind=kind, **kw)


def _apply(plan, reqs, tl=None):
    return apply_requests(plan, StoryRequests(requests=reqs), tl or _tl())


def test_no_requests_returns_plan_unchanged():
    plan = _plan([_ch(100, 130)])
    out, warns = _apply(plan, [])
    assert out == plan
    assert warns == []


def test_owner_example_add_chapter():
    plan = _plan([_ch(100, 130)])
    out, warns = _apply(plan, [AddChapter(at="20:05", title="Infinity Edge")])
    assert warns == []
    added = out.chapters[1]
    assert (added.start_s, added.end_s) == (1178.4, 1203.4)
    assert (added.title, added.kind) == ("Infinity Edge", "request")
    assert out.total_s == 30 + 25.0


def test_add_chapter_default_title_and_note():
    out, _ = _apply(_plan([]), [AddChapter(at="20:05", note="walking out of base")])
    c = out.chapters[0]
    assert c.title == "At 20:05"
    assert "walking out of base" in c.beats


def test_add_chapter_goes_in_chronological_order():
    plan = _plan([_ch(100, 130), _ch(2000, 2030)])
    out, _ = _apply(plan, [AddChapter(at="20:05")])
    assert [c.start_s for c in out.chapters] == [100, 1178.4, 2000]


def test_add_chapter_merges_into_overlapping_existing():
    plan = _plan([_ch(1170, 1190, title="Dive", kind="fight")])
    out, _ = _apply(plan, [AddChapter(at="20:05", title="Mine")])
    assert len(out.chapters) == 1
    c = out.chapters[0]
    assert (c.start_s, c.end_s) == (1170, 1203.4)
    assert (c.title, c.kind) == ("Dive", "fight")


def test_add_chapter_after_drop_of_overlapped_makes_new_request_chapter():
    plan = _plan([_ch(1170, 1190, title="Dive")])
    out, _ = _apply(plan, [Drop(at=1190 - OFFSET), AddChapter(at="20:05", title="Mine")])
    assert [(c.title, c.kind) for c in out.chapters] == [("Mine", "request")]


def test_add_chapter_clamped_at_recording_start():
    out, _ = _apply(_plan([]), [AddChapter(at=10, before_s=5, after_s=5)], _tl(offset=-3.0))
    assert out.chapters[0].start_s == 2.0
    out, _ = _apply(_plan([]), [AddChapter(at=1, before_s=5, after_s=5)], _tl(offset=-3.0))
    assert out.chapters[0].start_s == 0.0


def test_add_chapter_fills_kill_and_death_times():
    tl = _tl(
        [_ev("kill", 1185.0), _ev("death", 1190.0), _ev("kill", 5000.0), _ev("assist", 1186.0)]
    )
    out, _ = _apply(_plan([]), [AddChapter(at="20:05")], tl)
    c = out.chapters[0]
    assert c.kill_times_s == [1185.0]
    assert c.death_times_s == [1190.0]


def test_drop_removes_chapter_containing_time():
    plan = _plan([_ch(100, 130), _ch(200, 230, title="B")])
    out, warns = _apply(plan, [Drop(at=215 - OFFSET)])
    assert [c.title for c in out.chapters] == ["T"]
    assert warns == []
    assert out.total_s == 30


def test_drop_unmatched_warns():
    plan = _plan([_ch(100, 130)])
    out, warns = _apply(plan, [Drop(at=500)])
    assert out.chapters == plan.chapters
    assert len(warns) == 1
    assert "drop" in warns[0] and "500" in warns[0]


def test_drops_run_before_adds_regardless_of_file_order():
    plan = _plan([_ch(1170, 1190, title="Dive")])
    out, _ = _apply(plan, [AddChapter(at="20:05", title="Mine"), Drop(at=1180 - OFFSET)])
    assert [c.title for c in out.chapters] == ["Mine"]


def test_extend_grows_chapter_and_fills_new_times():
    tl = _tl([_ev("kill", 132.0), _ev("death", 98.0), _ev("kill", 110.0)])
    plan = _plan([_ch(100, 130, kill_times_s=[110.0])])
    out, warns = _apply(plan, [Extend(at=110 - OFFSET, before_s=5, after_s=4)], tl)
    c = out.chapters[0]
    assert (c.start_s, c.end_s) == (95, 134)
    assert c.kill_times_s == [110.0, 132.0]
    assert c.death_times_s == [98.0]
    assert out.total_s == 39
    assert warns == []


def test_extend_into_neighbour_merges_without_overlap():
    plan = _plan([_ch(100, 130, title="A"), _ch(135, 160, title="B")])
    out, _ = _apply(plan, [Extend(at=110 - OFFSET, after_s=10)])
    assert [(c.title, c.start_s, c.end_s) for c in out.chapters] == [("A", 100, 160)]


def test_extend_unmatched_warns():
    _, warns = _apply(_plan([_ch(100, 130)]), [Extend(at=900, after_s=5)])
    assert len(warns) == 1 and "extend" in warns[0]


def test_effect_defaults_to_whole_chapter():
    plan = _plan([_ch(100, 130)])
    out, _ = _apply(plan, [Effect(effect="letterbox", at=115 - OFFSET)])
    (fx,) = out.chapters[0].effects
    assert (fx.kind, fx.start_s, fx.end_s) == ("letterbox", 100, 130)


def test_effect_range_is_clamped_to_chapter():
    plan = _plan([_ch(100, 130)])
    req = Effect(effect="slowmo", at=115 - OFFSET, start=90 - OFFSET, end=125 - OFFSET,
                 params={"factor": 0.4})  # fmt: skip
    out, _ = _apply(plan, [req])
    (fx,) = out.chapters[0].effects
    assert (fx.start_s, fx.end_s, fx.params) == (100, 125, {"factor": 0.4})
    out, _ = _apply(plan, [Effect(effect="zoom", at=115 - OFFSET, start=120 - OFFSET, end=500)])
    assert (out.chapters[0].effects[0].start_s, out.chapters[0].effects[0].end_s) == (120, 130)


def test_effect_unmatched_warns():
    _, warns = _apply(_plan([_ch(100, 130)]), [Effect(effect="focus", at=900)])
    assert len(warns) == 1 and "effect" in warns[0]


def test_owner_example_letterbox_lands_on_close_chapter():
    close = _ch(2040.0, 2080.0, title="The close", kind="close")  # 34:30 -> 2048.4 recording
    plan = _plan([_ch(100, 130), close])
    out, warns = _apply(plan, [Effect(effect="letterbox", at="34:30")])
    assert warns == []
    assert out.chapters[0].effects == []
    (fx,) = out.chapters[1].effects
    assert (fx.kind, fx.start_s, fx.end_s) == ("letterbox", 2040.0, 2080.0)


def test_effect_targets_chapter_added_in_same_file():
    out, _ = _apply(_plan([]), [AddChapter(at="20:05"), Effect(effect="slowmo", at="20:05")])
    c = out.chapters[0]
    assert (c.effects[0].start_s, c.effects[0].end_s) == (c.start_s, c.end_s)


def test_total_includes_card_seconds_when_plan_has_cards():
    plan = _plan([_ch(100, 130)], cards=True, card_s=2.0)
    out, _ = _apply(plan, [AddChapter(at="20:05")])
    assert out.total_s == 32 + 27


def test_apply_is_pure():
    plan = _plan([_ch(100, 130)])
    before = plan.model_copy(deep=True)
    _apply(plan, [Drop(at=115 - OFFSET), AddChapter(at="20:05")])
    assert plan == before
