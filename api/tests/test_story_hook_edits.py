"""hook_edits: the edits file and the pure track maths."""

from __future__ import annotations

import math

import pytest

from ai_video_editor.story import hook_edits as he
from ai_video_editor.story.hook_edits import (
    EditHook,
    EditPick,
    HookEdits,
    base_digest,
    base_len,
    chapter_bases,
    clamp_offsets,
    fresh_edits,
    hook_view,
    limits,
    settle,
    split_pick,
    validate_picks,
)
from ai_video_editor.story.join_map import (
    JoinChapter,
    JoinGame,
    JoinHook,
    JoinHookEvent,
    JoinHookPick,
    JoinMap,
)


def _chapter(title, kind, start, end, vs):
    return JoinChapter(
        title=title,
        kind=kind,
        video_start_s=vs,
        game_start_s=start,
        game_end_s=end,
        kills=0,
        deaths=0,
    )


def _pick(pid, cid, idx, rs, re_, at, c0, c1, events=()):
    return JoinHookPick(
        id=pid,
        chapter_id=cid,
        round="r1",
        chapter_index=idx,
        rec_start_s=rs,
        rec_end_s=re_,
        offset_s=0.0,
        at_s=at,
        video_start_s=at,
        video_end_s=at + (re_ - rs),
        events=list(events),
        chapter_rec_start_s=c0,
        chapter_rec_end_s=c1,
    )


def _jm() -> JoinMap:
    game = JoinGame(
        round="r1",
        label="Xayah 2/0/0",
        video_start_s=0,
        story_start_s=0,
        video_end_s=100,
        story_json="s.json",
        digest_json=None,
        chapters=[
            _chapter("2v2 fight, owner won", "play", 100, 140, 0),
            _chapter("early", "early", 160, 180, 40),
            _chapter("Another play, x", "play", 200, 230, 60),
        ],
    )
    hook = JoinHook(
        mode="play",
        title="t",
        picks=[
            _pick(
                "aaaaaaaa",
                "r1:p00",
                0,
                116,
                130,
                0.0,
                100,
                140,
                [JoinHookEvent(rec_s=120, kind="kill"), JoinHookEvent(rec_s=128, kind="kill")],
            ),
            _pick("bbbbbbbb", "r1:p02", 2, 206, 214, 14.0, 200, 230),
        ],
    )
    return JoinMap(
        schema_version=2,
        video="j.mp4",
        title="T",
        duration_s=100,
        intro_s=20.0,
        games=[game],
        hook=hook,
    )


def _edits(jm, picks):
    return HookEdits(video="joined_x", base_digest=base_digest(jm.hook), hook=EditHook(picks=picks))


def test_base_digest_depends_on_ids_and_windows_only():
    jm = _jm()
    d = base_digest(jm.hook)
    assert d.startswith("sha256:") and len(d) == len("sha256:") + 12
    assert base_digest(_jm().hook) == d
    jm3 = _jm()
    jm3.hook.picks[0].rec_end_s = 131
    assert base_digest(jm3.hook) != d
    jm4 = _jm()
    jm4.hook.picks[0].at_s = 5.0  # not part of the digest
    assert base_digest(jm4.hook) == d


def test_chapter_bases_one_per_chapter_with_label():
    b = chapter_bases(_jm())
    assert set(b) == {"r1:p00", "r1:p02"}
    assert b["r1:p00"].label == "2v2 fight"
    assert len(b["r1:p00"].events) == 2


def test_base_len_adds_the_freeze_in_tease_mode_only():
    p = _jm().hook.picks[0]
    assert base_len(p, "play") == 14.0
    assert base_len(p, "tease") == pytest.approx(14.0 + he.FREEZE_S)


def test_settle_pushes_overlaps():
    a, b, c = (EditPick(id=i, chapter_id=i, at_s=t) for i, t in (("a", 0), ("b", 5), ("c", 30)))
    out = settle([a, b, c], {"a": 14, "b": 14, "c": 14})
    assert [p.at_s for p in out] == [0.0, 14.0, 30.0]


def test_settle_sorts_by_at_and_clamps_negative():
    a = EditPick(id="a", chapter_id="x", at_s=10.04)
    b = EditPick(id="b", chapter_id="x", at_s=-3)
    out = settle([a, b], {"a": 1, "b": 1})
    assert [(p.id, p.at_s) for p in out] == [("b", 0.0), ("a", 10.0)]


def test_limits_at_the_ends_and_between_neighbours():
    jm = _jm()
    b = chapter_bases(jm)
    assert limits(jm, b["r1:p00"]) == (0.0, 159.5)
    assert limits(jm, b["r1:p02"]) == (180.5, math.inf)


def test_clamp_offsets_stops_at_neighbours():
    jm = _jm()
    b = chapter_bases(jm)
    assert clamp_offsets(jm, b["r1:p00"], 0.0, 40.0, "start") == (0.0, 29.5)  # 130 + 29.5
    assert clamp_offsets(jm, b["r1:p02"], 40.0, 0.0, "end") == (25.5, 0.0)  # 206 - 180.5
    assert clamp_offsets(jm, b["r1:p00"], 30.0, 0.0, "end") == (30.0, 0.0)  # first chapter: from 0


def test_clamp_offsets_keeps_min_length():
    jm = _jm()
    p = jm.hook.picks[0]  # 116..130
    assert clamp_offsets(jm, p, 0.0, -20.0, "start") == (0.0, -12.0)  # 116..118
    assert clamp_offsets(jm, p, -20.0, 0.0, "end") == (-12.0, 0.0)  # 128..130


def test_validate_picks_rejects_unknown_chapter_and_duplicate_pick_ids():
    jm = _jm()
    with pytest.raises(ValueError, match="unknown chapter_id: r1:nope"):
        validate_picks(jm, [EditPick(chapter_id="r1:nope")])
    with pytest.raises(ValueError, match="duplicate pick id: zz"):
        validate_picks(
            jm, [EditPick(id="zz", chapter_id="r1:p00"), EditPick(id="zz", chapter_id="r1:p02")]
        )
    out = validate_picks(
        jm,
        [
            EditPick(id="a1", chapter_id="r1:p00", at_s=0),
            EditPick(id="a2", chapter_id="r1:p00", at_s=3, end_s=99),
        ],
    )
    assert [p.at_s for p in out] == [0.0, 14.0]  # pushed past the first piece
    assert out[1].end_s == 29.5  # clamped to the neighbour


def test_split_pick_makes_two_pieces_of_the_same_chapter():
    jm = _jm()
    p = jm.hook.picks[0]  # base window 116..130 at at_s 0
    out = split_pick(jm, fresh_edits(jm, "joined_x").hook.picks, p.id, 6.0)
    left, right = out[0], out[1]
    assert (left.id, left.at_s, left.start_s, left.end_s) == (p.id, 0.0, 0.0, -8.0)  # 116..122
    assert (right.chapter_id, right.at_s, right.start_s, right.end_s) == (
        p.chapter_id,
        6.0,
        -6.0,
        0.0,
    )
    assert right.id != left.id and len(right.id) == 8
    assert out[2].id == "bbbbbbbb"


def test_split_pick_too_close_to_an_edge_raises():
    jm = _jm()
    picks = fresh_edits(jm, "j").hook.picks
    with pytest.raises(ValueError, match="too close"):
        split_pick(jm, picks, "aaaaaaaa", 1.0)
    with pytest.raises(ValueError, match="too close"):
        split_pick(jm, picks, "aaaaaaaa", 13.0)


def test_hook_view_without_edits_is_the_rendered_track_and_clean():
    v = hook_view(_jm(), "joined_x", None)
    assert not v.dirty and not v.stale_edits and v.edits_digest is None
    assert [p.pending for p in v.picks] == [False, False]
    assert v.picks[0].video_end_s == 14.0 and v.picks[0].label == "2v2 fight"
    assert v.laptop_command == "story-join --apply-edits joined_x"
    assert v.rendered_intro_s == 20.0


def test_hook_view_marks_pending_and_sums_the_track():
    jm = _jm()
    v = hook_view(
        jm,
        "joined_x",
        _edits(
            jm,
            [
                EditPick(id="aaaaaaaa", chapter_id="r1:p00", at_s=0, end_s=2.0),
                EditPick(id="bbbbbbbb", chapter_id="r1:p02", at_s=30),
            ],
        ),
    )
    assert v.picks[0].pending and v.picks[0].len_s == 16.0 and v.picks[1].at_s == 30.0
    assert v.picks[1].pending
    assert v.track_end_s == 30.0 + base_len(jm.hook.picks[1], "play")
    assert v.empty_space_s == 14.0 and v.dirty
    assert v.intro_after_s == pytest.approx(16 + 8 - 2 * 0.5)
    assert v.picks[0].beyond_chapter_s == 0.0 and v.picks[0].limit_hi_rec_s == 159.5
    assert v.picks[1].limit_hi_rec_s is None


def test_hook_view_beyond_chapter_and_new_piece():
    jm = _jm()
    v = hook_view(
        jm, "j", _edits(jm, [EditPick(id="new00000", chapter_id="r1:p00", at_s=0, end_s=12.0)])
    )
    p = v.picks[0]
    assert p.beyond_chapter_s == 2.0 and p.video_start_s is None and p.pending and v.dirty


def test_hook_view_stale_when_digest_differs():
    jm = _jm()
    e = _edits(jm, fresh_edits(jm, "j").hook.picks).model_copy(update={"base_digest": "sha256:000"})
    v = hook_view(jm, "j", e)
    assert v.stale_edits and v.edits_digest == "sha256:000" and not v.dirty


def test_edits_round_trip_on_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(he.settings, "workspace_dir", tmp_path)
    jm = _jm()
    assert he.load_edits("j") is None
    e = fresh_edits(jm, "j")
    path = he.save_edits(e)
    assert path == tmp_path / "story" / "j.edits.json" == he.edits_path("j")
    assert he.load_edits("j") == e
    path.write_text("{nope", encoding="utf-8")
    with pytest.raises(ValueError, match=r"j\.edits\.json"):
        he.load_edits("j")
    he.delete_edits("j")
    he.delete_edits("j")
    assert he.load_edits("j") is None
