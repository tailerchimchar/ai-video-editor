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
    Mute,
    StoryRequests,
    Trim,
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
                    {"kind": "trim", "at": 5, "start": "0:02"},
                    {"kind": "mute", "at": 5},
                ]
            }
        ),
        encoding="utf-8",
    )
    kinds = [type(r) for r in load_requests(tmp_path).requests]
    assert kinds == [AddChapter, Extend, Drop, Effect, Trim, Mute]


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


# --- trim / mute ---
# Clocks are game seconds = recording seconds - OFFSET (-21.6).


def test_trim_shrinks_to_the_window_and_drops_outside_data():
    from ai_video_editor.story.broll import BRollInsert
    from ai_video_editor.story.planner import ChapterEffect

    ch = _ch(
        100.0, 200.0,
        kill_times_s=[110.0, 150.0, 190.0], death_times_s=[105.0, 160.0],
        effects=[
            ChapterEffect(kind="slowmo", start_s=105.0, end_s=120.0),
            ChapterEffect(kind="zoom", start_s=140.0, end_s=170.0),
        ],
        mic_mutes=[(100.0, 130.0), (160.0, 180.0)],
        broll=[
            BRollInsert(item_id=1, mic_start_s=102, mic_end_s=106, src_start_s=1, src_end_s=5),
            BRollInsert(item_id=2, mic_start_s=150, mic_end_s=154, src_start_s=1, src_end_s=5),
        ],
    )  # fmt: skip
    req = Trim(at=150.0 - OFFSET, start=125.0 - OFFSET, end=175.0 - OFFSET)
    plan, warns = _apply(_plan([ch]), [req])
    c = plan.chapters[0]
    assert not warns
    assert (c.start_s, c.end_s) == (125.0, 175.0)
    assert c.kill_times_s == [150.0] and c.death_times_s == [160.0]
    assert [(e.kind, e.start_s, e.end_s) for e in c.effects] == [("zoom", 140.0, 170.0)]
    assert c.mic_mutes == [(125.0, 130.0), (160.0, 175.0)]
    assert [b.item_id for b in c.broll] == [2]


def test_trim_start_only_and_never_grows():
    ch = _ch(100.0, 200.0)
    at = 150.0 - OFFSET
    plan, _ = _apply(_plan([ch]), [Trim(at=at, start=120.0 - OFFSET)])
    assert (plan.chapters[0].start_s, plan.chapters[0].end_s) == (120.0, 200.0)
    plan, _ = _apply(_plan([ch]), [Trim(at=at, start=50.0 - OFFSET, end=500.0 - OFFSET)])
    assert (plan.chapters[0].start_s, plan.chapters[0].end_s) == (100.0, 200.0)


def test_trim_total_follows():
    plan, _ = _apply(_plan([_ch(100.0, 200.0)]), [Trim(at=150.0 - OFFSET, end=130.0 - OFFSET)])
    assert plan.total_s == 30.0


def test_trim_warns_without_chapter_or_for_empty_window():
    _, warns = _apply(_plan([_ch(100.0, 200.0)]), [Trim(at=900.0)])
    assert len(warns) == 1 and "no chapter" in warns[0]
    plan, warns = _apply(
        _plan([_ch(100.0, 200.0)]),
        [Trim(at=150.0 - OFFSET, start=300.0 - OFFSET, end=400.0 - OFFSET)],
    )
    assert len(warns) == 1 and "empty" in warns[0]
    assert (plan.chapters[0].start_s, plan.chapters[0].end_s) == (100.0, 200.0)


def test_mute_defaults_to_the_whole_chapter():
    plan, warns = _apply(_plan([_ch(100.0, 200.0)]), [Mute(at=150.0 - OFFSET)])
    assert not warns and plan.chapters[0].mic_mutes == [(100.0, 200.0)]


def test_mute_range_is_clamped_and_appends():
    ch = _ch(100.0, 200.0, mic_mutes=[(100.0, 110.0)])
    plan, _ = _apply(
        _plan([ch]), [Mute(at=150.0 - OFFSET, start=180.0 - OFFSET, end=260.0 - OFFSET)]
    )
    assert plan.chapters[0].mic_mutes == [(100.0, 110.0), (180.0, 200.0)]


def test_mute_warns_without_chapter_or_outside():
    _, warns = _apply(_plan([_ch(100.0, 200.0)]), [Mute(at=900.0)])
    assert "no chapter" in warns[0]
    _, warns = _apply(
        _plan([_ch(100.0, 200.0)]),
        [Mute(at=150.0 - OFFSET, start=300.0 - OFFSET, end=310.0 - OFFSET)],
    )
    assert "outside" in warns[0]


def test_order_extend_then_trim_then_mute():
    reqs = [
        Mute(at=150.0 - OFFSET),
        Trim(at=150.0 - OFFSET, start=120.0 - OFFSET, end=180.0 - OFFSET),
        Extend(at=150.0 - OFFSET, after_s=50.0),
    ]
    c = _apply(_plan([_ch(100.0, 200.0)]), reqs)[0].chapters[0]
    assert (c.start_s, c.end_s) == (120.0, 180.0)  # trimmed after the extend
    assert c.mic_mutes == [(120.0, 180.0)]  # muted after the trim
