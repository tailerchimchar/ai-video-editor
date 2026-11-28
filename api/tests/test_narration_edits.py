import pytest

from ai_video_editor.narration.edits import (
    OverlapError,
    TakeTooShortError,
    apply_edit,
    check_no_overlap,
    media_refcount,
    punch_in,
    shift_session,
    split_take,
)
from ai_video_editor.narration.models import (
    CameraBox,
    CameraRegion,
    GameAudioRegion,
    NarrationSession,
    Take,
    TakeEdit,
)


def _take(id, start, dur, media="m.webm", **kw) -> Take:
    return Take(
        id=id,
        start_s=start,
        duration_s=dur,
        media=media,
        created_at="2026-11-27T00:00:00+00:00",
        **kw,
    )


def _session(takes=(), game=(), cam=()) -> NarrationSession:
    return NarrationSession(
        target={"kind": "story", "key": "r1"},
        takes=list(takes),
        game_regions=list(game),
        camera_regions=list(cam),
    )


def test_old_session_json_loads_with_defaults():
    old = (
        '{"target": {"kind": "story", "key": "r1"}, "takes": [{"id": "a",'
        ' "start_s": 1, "duration_s": 2, "media": "a.webm",'
        ' "created_at": "2026-01-01T00:00:00+00:00"}]}'
    )
    s = NarrationSession.model_validate_json(old)
    t = s.takes[0]
    assert (t.media_in_s, t.offset_s, t.gain, t.muted) == (0, 0, 1, False)
    assert s.game_regions == [] and s.camera_regions == []
    assert s.pinned_intro_s is None and s.mix.game_gain == 1.0


def test_start_effective():
    assert _take("a", 10, 5, offset_s=-0.5).start_effective_s == 9.5


def test_apply_edit_tail_trim_and_bounds():
    t = apply_edit(_take("a", 10, 20), TakeEdit(duration_s=5))
    assert (t.start_s, t.duration_s, t.media_in_s) == (10, 5, 0)
    with pytest.raises(TakeTooShortError):
        apply_edit(_take("a", 10, 20), TakeEdit(duration_s=0.2))


def test_apply_edit_returns_copy_and_sets_flags():
    orig = _take("a", 10, 20)
    t = apply_edit(orig, TakeEdit(muted=True, gain=2.0, offset_s=0.5))
    assert (t.muted, t.gain, t.offset_s) == (True, 2.0, 0.5)
    assert orig.muted is False


def test_split_shares_media_and_offsets_media_in():
    a, b = split_take(_take("a", 10, 20, media_in_s=1.5), at_s=18, new_id="b")
    assert (a.duration_s, b.start_s, b.media_in_s, b.duration_s) == (8, 18, 9.5, 12)
    assert a.media == b.media == "m.webm"
    with pytest.raises(ValueError):
        split_take(_take("a", 10, 20), at_s=10.1, new_id="b")


def test_split_keeps_offset_in_effective_time():
    a, b = split_take(_take("a", 10, 20, offset_s=1.0), at_s=18, new_id="b")
    assert b.offset_s == 1.0 and b.start_effective_s == 18
    assert a.duration_s == 7


def test_punch_in_trims_deletes_and_splits():
    old = [
        _take("A", 0, 25),
        _take("B", 30, 10),
        _take("C", 43, 30),
        _take("D", 50, 5, media="d.webm"),
    ]
    out = punch_in(old, _take("N", 18, 37), new_id=iter(["x"]).__next__)
    by = {t.id: t for t in out}
    assert by["A"].duration_s == 18 and "B" not in by and "D" not in by
    assert by["C"].start_s == 55 and by["C"].media_in_s == 12
    assert by["C"].duration_s == 18
    assert by["N"].start_s == 18


def test_punch_in_spanning_take_is_split():
    out = punch_in([_take("A", 0, 60)], _take("N", 20, 10), new_id=iter(["x"]).__next__)
    assert sorted(t.id for t in out) == ["A", "N", "x"]
    tail = next(t for t in out if t.id == "x")
    assert (tail.start_s, tail.media_in_s, tail.duration_s) == (30, 30, 30)


def test_punch_in_at_exact_boundary_touches_nothing():
    out = punch_in([_take("A", 0, 10)], _take("N", 10, 5), new_id=iter(["x"]).__next__)
    assert {t.id: (t.start_s, t.duration_s) for t in out} == {"A": (0, 10), "N": (10, 5)}


def test_punch_in_drops_remainder_under_min():
    out = punch_in([_take("A", 0, 5)], _take("N", 0.2, 5), new_id=iter(["x"]).__next__)
    assert sorted(t.id for t in out) == ["N"]


def test_shift_session_clamps_at_zero_and_moves_regions():
    s = _session(
        takes=[_take("a", 1.0, 4)],
        game=[
            GameAudioRegion(id="g1", start_s=2, end_s=4, level=0.5),
            GameAudioRegion(id="g2", start_s=0, end_s=1, level=0.5),
        ],
        cam=[CameraRegion(id="c1", start_s=5, end_s=6, box=CameraBox())],
    )
    out = shift_session(s, -3)
    assert (out.takes[0].start_s, out.takes[0].duration_s) == (0, 4)
    assert [(r.id, r.start_s, r.end_s) for r in out.game_regions] == [("g1", 0, 1)]
    assert (out.camera_regions[0].start_s, out.camera_regions[0].end_s) == (2, 3)
    up = shift_session(s, 13.8)
    assert up.takes[0].start_s == pytest.approx(14.8)
    assert up.game_regions[1].start_s == pytest.approx(13.8)
    assert s.takes[0].start_s == 1.0


def test_region_requires_end_after_start():
    with pytest.raises(ValueError):
        GameAudioRegion(id="g", start_s=2, end_s=2, level=1)


def test_check_no_overlap_allows_touching_rejects_overlap():
    r = lambda i, a, b: GameAudioRegion(id=i, start_s=a, end_s=b, level=1)  # noqa: E731
    check_no_overlap([r("b", 5, 8), r("a", 0, 5)])
    with pytest.raises(OverlapError):
        check_no_overlap([r("a", 0, 5.1), r("b", 5, 8)])


def test_media_refcount():
    ts = [_take("a", 0, 1, media="x"), _take("b", 2, 1, media="x"), _take("c", 4, 1, media="y")]
    assert media_refcount(ts, "x") == 2 and media_refcount(ts, "z") == 0
