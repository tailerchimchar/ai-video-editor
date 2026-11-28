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
    return HookEdits(
        video="joined_x",
        base_digest=base_digest(jm.hook),
        hook=EditHook(mode=jm.hook.mode, title=jm.hook.title, picks=picks),
    )


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


def _jm_with_extra_chapters():
    """The `_jm` map, with the chapters carrying their own base windows."""
    jm = _jm()
    ch = jm.games[0].chapters
    ch[0].play_id = "p00"
    ch[0].hook_rec_start_s, ch[0].hook_rec_end_s = 110.0, 120.0  # the pick's 116..130 wins
    ch[2].play_id = "p02"
    ch[2].hook_rec_start_s, ch[2].hook_rec_end_s = 205.0, 215.0
    ch[2].kill_times_s = [220.0, 212.0]
    ch[2].death_times_s = [225.0]
    jm.hook.picks = jm.hook.picks[:1]
    return jm


def test_chapter_bases_includes_chapters_outside_the_hook():
    jm = _jm_with_extra_chapters()
    b = chapter_bases(jm)
    assert set(b) == {"r1:p00", "r1:p02"}
    assert (b["r1:p00"].rec_start_s, b["r1:p00"].rec_end_s) == (116, 130)  # from the pick
    p2 = b["r1:p02"]
    assert (p2.rec_start_s, p2.rec_end_s) == (205.0, 215.0)
    assert (p2.round, p2.chapter_index, p2.offset_s) == ("r1", 2, 0.0)
    assert (p2.chapter_rec_start_s, p2.chapter_rec_end_s) == (200.0, 230.0)
    assert p2.label == "Another play"
    assert [(e.rec_s, e.kind) for e in p2.events] == [
        (212.0, "kill"),
        (220.0, "kill"),
        (225.0, "death"),
    ]


def test_chapter_bases_non_play_chapter_id_uses_kind_and_recording_start():
    jm = _jm_with_extra_chapters()
    jm.games[0].offset_s = 10.0
    c = jm.games[0].chapters[1]
    c.hook_rec_start_s, c.hook_rec_end_s = 175.0, 185.0
    assert "r1:early@170.0" in chapter_bases(jm)  # game_start 160 + offset 10


def _edits_with(jm, **hook_kw):
    e = fresh_edits(jm, "j")
    return e.model_copy(update={"hook": e.hook.model_copy(update=hook_kw)})


def test_dirty_considers_mode_and_title():
    jm = _jm()
    assert not hook_view(jm, "j", fresh_edits(jm, "j")).dirty
    assert hook_view(jm, "j", _edits_with(jm, mode="tease")).dirty
    assert hook_view(jm, "j", _edits_with(jm, title="new")).dirty


def test_mode_comes_from_the_edits_when_there_are_edits():
    jm = _jm()  # map mode: play
    v = hook_view(jm, "j", _edits_with(jm, mode="tease"))
    assert v.picks[0].len_s == pytest.approx(14.0 + he.FREEZE_S)
    out = validate_picks(jm, [EditPick(id="a", chapter_id="r1:p00"), EditPick(chapter_id="r1:p00")])
    assert out[1].at_s == 14.0  # no edits arg: the map's mode (play)
    out = validate_picks(
        jm,
        [EditPick(id="a", chapter_id="r1:p00"), EditPick(chapter_id="r1:p00")],
        _edits_with(jm, mode="tease"),
    )
    assert out[1].at_s == pytest.approx(15.4, abs=0.05)


def test_hook_view_in_tease_mode_includes_the_freeze():
    jm = _jm()
    jm.hook.mode = "tease"
    v = hook_view(jm, "j", None)
    assert v.picks[0].len_s == pytest.approx(14.0 + 1.4)


def test_split_pick_errors():
    jm = _jm()
    picks = fresh_edits(jm, "j").hook.picks
    with pytest.raises(ValueError, match="unknown pick id"):
        split_pick(jm, picks, "nope", 5.0)
    with pytest.raises(ValueError, match="outside the pick"):
        split_pick(jm, picks, "aaaaaaaa", 50.0)
    bad = [picks[0].model_copy(update={"chapter_id": "r1:gone"})]
    with pytest.raises(ValueError, match="unknown chapter_id: r1:gone"):
        split_pick(jm, bad, "aaaaaaaa", 5.0)


def test_clamp_never_leaves_a_window_shorter_than_the_minimum_after_rounding():
    jm = _jm()
    p = jm.hook.picks[0]
    for keep in ("start", "end"):
        s, e = clamp_offsets(jm, p, 0.04, -14.04, keep)
        assert (p.rec_end_s - p.rec_start_s) + s + e >= he.HOOK_MIN_S - 1e-9


def test_settle_rounds_up_so_rounding_never_overlaps():
    a = EditPick(id="a", chapter_id="x", at_s=0)
    b = EditPick(id="b", chapter_id="x", at_s=0.5)
    out = settle([a, b], {"a": 2.01, "b": 1})
    assert out[1].at_s == 2.1


def test_settle_unknown_id_raises():
    with pytest.raises(ValueError, match="missing"):
        settle([EditPick(id="a", chapter_id="x")], {})


def test_limits_next_neighbour_index_is_bounded():
    jm = _jm()
    b = chapter_bases(jm)["r1:p02"]
    assert limits(jm, b.model_copy(update={"chapter_index": 3}))[1] == math.inf


def test_edits_path_rejects_path_tricks_and_save_is_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr(he.settings, "workspace_dir", tmp_path)
    for bad in ("../x", "a/b", "a\b", ".."):
        with pytest.raises(ValueError):
            he.edits_path(bad)
    he.save_edits(fresh_edits(_jm(), "j"))
    assert [p.name for p in (tmp_path / "story").iterdir()] == ["j.edits.json"]


def test_hook_view_carries_the_rendered_window_with_the_folded_offsets():
    jm = _jm()
    r0 = jm.hook.picks[0]
    jm.hook.picks[0] = r0.model_copy(update={"start_s": 1.0, "end_s": -2.0})
    v = hook_view(
        jm,
        "j",
        _edits(
            jm,
            [
                EditPick(id=r0.id, chapter_id=r0.chapter_id, at_s=0, start_s=3.0, end_s=-2.0),
                EditPick(id="new00000", chapter_id=r0.chapter_id, at_s=40),
            ],
        ),
    )
    p, new = v.picks
    # as rendered: the map's base window with the folded offsets, not the base
    assert p.rendered_rec_start_s == r0.rec_start_s - 1.0
    assert p.rendered_rec_end_s == r0.rec_end_s - 2.0
    assert p.rec_start_s == r0.rec_start_s - 3.0  # what the edits want
    assert new.rendered_rec_start_s is None and new.rendered_rec_end_s is None
