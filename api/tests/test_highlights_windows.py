from ai_video_editor.highlights import (
    FIGHT_PRE_ALLY_DEATH_S,
    RANKER_WIDEN_MAX_S,
    _window_anchor,
    fight_window,
)

M = {"fight_start_s": 100.0, "fight_end_s": 110.0}


def test_constants():
    assert (FIGHT_PRE_ALLY_DEATH_S, RANKER_WIDEN_MAX_S) == (8.0, 5.0)


def test_pre_pad_8s_when_fight_opens_with_ally_death():
    meta = {**M, "fight_opens_with_ally_death": True}
    assert fight_window(meta, None, 1000.0, 120.0)[0] == 92.0


def test_pre_pad_4s_otherwise():
    assert fight_window(M, None, 1000.0, 120.0)[0] == 96.0


def test_ranker_widen_capped_at_5s():
    assert fight_window(M, (80.0, 130.0), 1000.0, 120.0) == (91.0, 118.0)


def test_ranker_widen_within_cap_is_honoured():
    assert fight_window(M, (93.0, 115.0), 1000.0, 120.0) == (93.0, 115.0)


def test_end_never_past_game_end_plus_1():
    meta = {**M, "game_end_s": 112.0}
    assert fight_window(meta, (80.0, 130.0), 1000.0, 120.0)[1] == 113.0


def test_ally_death_at_recording_start_clamps_to_zero():
    meta = {"fight_start_s": 3.0, "fight_end_s": 10.0, "fight_opens_with_ally_death": True}
    assert fight_window(meta, None, 1000.0, 120.0)[0] == 0.0


def test_outplayed_only_path_unchanged():
    plain = {"source": "riot_api", "event_type": "kill", "metadata": {"anchor_seconds": 100.0}}
    assert _window_anchor(plain, {}, 1000.0) == (97.0, 108.0)


def test_game_end_before_padded_start_skips_cap():
    meta = {"fight_start_s": 200.0, "fight_end_s": 210.0, "game_end_s": 110.0}
    assert fight_window(meta, None, 1000.0, 120.0) == (196.0, 213.0)
