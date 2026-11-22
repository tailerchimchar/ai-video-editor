from ai_video_editor.highlights import _window_anchor, cut_window




def _cands():
    plain = {"source": "riot_api", "event_type": "kill", "metadata": {"anchor_seconds": 100.0}}
    cluster = {
        "source": "riot_api",
        "start_seconds": 200.0,
        "end_seconds": 230.0,
        "metadata": {"anchor_seconds": 215.0},
    }
    sugg = {"source": "riot_api", "metadata": {"anchor_seconds": 300.0}}
    return plain, cluster, sugg


def test_no_bounds_path_unchanged():
    plain, cluster, sugg = _cands()
    assert _window_anchor(plain, {}, 1000.0) == (97.0, 108.0)
    assert cut_window(cluster, {}, 1000.0) == (207.5, 222.5)
    r = {"suggested_start_seconds": 280.0, "suggested_end_seconds": 340.0}
    assert cut_window(sugg, r, 1000.0) == (280.0, 340.0)


from ai_video_editor.highlights import (  # noqa: E402
    FIGHT_POST_S,
    FIGHT_PRE_S,
    GAME_END_REACH_S,
    fight_window,
)

M = {"fight_start_s": 100.0, "fight_end_s": 120.0}


def test_constants():
    assert (FIGHT_PRE_S, FIGHT_POST_S, GAME_END_REACH_S) == (4.0, 3.0, 25.0)


def test_pads():
    assert fight_window(M, None, 1000.0, 120.0) == (96.0, 123.0)


def test_pre_pad_override():
    assert fight_window({**M, "pre_pad_s": 8.0}, None, 1000.0, 120.0) == (92.0, 123.0)


def test_game_end_reach_inside():
    m = {**M, "game_end_s": 145.0}
    assert fight_window(m, None, 1000.0, 120.0) == (96.0, 146.0)


def test_game_end_reach_boundary_and_outside():
    assert fight_window({**M, "game_end_s": 145.0}, None, 1000.0, 120.0)[1] == 146.0
    assert fight_window({**M, "game_end_s": 145.5}, None, 1000.0, 120.0)[1] == 123.0
    assert fight_window({**M, "game_end_s": 110.0}, None, 1000.0, 120.0)[1] == 123.0
    assert fight_window({**M, "game_end_s": None}, None, 1000.0, 120.0)[1] == 123.0


def test_ranker_widens_only():
    assert fight_window(M, (90.0, 130.0), 1000.0, 120.0) == (90.0, 130.0)
    assert fight_window(M, (99.0, 121.0), 1000.0, 120.0) == (96.0, 123.0)


def test_clamp():
    assert fight_window({"fight_start_s": 2.0, "fight_end_s": 995.0 - 880}, None, 118.0, 120.0) == (
        0.0,
        118.0,
    )


def test_front_trim_keeps_end():
    m = {"fight_start_s": 100.0, "fight_end_s": 300.0}
    assert fight_window(m, None, 1000.0, 120.0) == (183.0, 303.0)


def test_window_anchor_delegates_and_bypasses_cluster_midpoint():
    cand = {
        "source": "riot_api",
        "start_seconds": 200.0,
        "end_seconds": 230.0,
        "metadata": {"anchor_seconds": 215.0, **M},
    }
    assert _window_anchor(cand, {}, 1000.0) == (96.0, 123.0)
    assert cut_window(cand, {}, 1000.0) == (96.0, 123.0)
    r = {"suggested_start_seconds": 90.0, "suggested_end_seconds": 130.0}
    assert _window_anchor(cand, r, 1000.0) == (90.0, 130.0)
