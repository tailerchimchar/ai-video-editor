"""join_map: where each game and chapter sits in a joined video; YouTube chapters."""

from __future__ import annotations

from pathlib import Path

from ai_video_editor.story.broll import BRollInsert
from ai_video_editor.story.join_map import (
    SUBSCRIBE_LEAD_S,
    JoinHook,
    JoinHookEvent,
    JoinHookPick,
    JoinMap,
    build_join_map,
    chapter_id,
    youtube_chapters,
)
from ai_video_editor.story.planner import Chapter, StoryPlan


def _plan(*chapters: Chapter) -> StoryPlan:
    return StoryPlan(
        chapters=list(chapters), total_s=0.0, mic_stream=None, audio="mixed", cards=False
    )


def _ch(title, start, end, kills=(), deaths=(), broll=()) -> Chapter:
    return Chapter(
        title=title,
        start_s=start,
        end_s=end,
        kind="lane",
        kill_times_s=list(kills),
        death_times_s=list(deaths),
        broll=list(broll),
    )


def _map(subscribe=None, hook=None):
    p1 = _plan(_ch("The opening", 60, 160, kills=[100]), _ch("Fight", 300, 330, deaths=[310]))
    item = BRollInsert(item_id=3070, mic_start_s=400, mic_end_s=404, src_start_s=400, src_end_s=404)
    p2 = _plan(_ch("The opening", 20, 120, broll=[item]))
    return build_join_map(
        video=Path("j.mp4"),
        title="How to destroy lane in 5 minutes",
        rounds=["r1", "r2"],
        labels=["Xayah 5/0/2", "Kai'sa 7/1/3"],
        plans=[p1, p2],
        offsets_s=[-10.0, 40.0],
        story_durations_s=[130.0, 100.0],
        card_s=2.5,
        story_card_s=2.5,
        story_jsons=[Path("a/story.json"), Path("b/story.json")],
        digest_jsons=[Path("a/digest.json"), None],
        subscribe=subscribe,
        hook=hook,
    )


def test_games_and_chapters_are_placed_after_each_card():
    jm = _map()
    g1, g2 = jm.games
    assert (g1.video_start_s, g1.story_start_s, g1.video_end_s) == (0.0, 2.5, 132.5)
    assert (g2.video_start_s, g2.story_start_s, g2.video_end_s) == (132.5, 135.0, 235.0)
    assert [c.video_start_s for c in g1.chapters] == [2.5, 102.5]  # 100 s opening, no cards
    assert (g1.chapters[0].game_start_s, g1.chapters[0].game_end_s) == (70.0, 170.0)  # rec - offset
    assert (g1.chapters[0].kills, g1.chapters[1].deaths) == (1, 1)
    assert g2.chapters[0].items == [3070]
    assert g1.digest_json and g2.digest_json is None
    assert jm.duration_s == 235.0 and jm.inserts == []


def test_swipe_overlap_without_cards():
    p1 = _plan(_ch("The opening", 60, 160))
    p2 = _plan(_ch("The opening", 20, 120))
    jm = build_join_map(
        video=Path("j.mp4"),
        title="t",
        rounds=["r1", "r2"],
        labels=["A", "B"],
        plans=[p1, p2],
        offsets_s=[0.0, 0.0],
        story_durations_s=[100.0, 100.0],
        card_s=0.0,
        story_card_s=2.5,
        story_jsons=[Path("a"), Path("b")],
        digest_jsons=[None, None],
        subscribe=(1, 5.0, Path("sub.mov")),
        transition_s=0.5,
    )
    g1, g2 = jm.games
    assert (g1.video_start_s, g1.video_end_s) == (0.0, 100.0)
    assert (g2.video_start_s, g2.story_start_s, g2.video_end_s) == (99.5, 99.5, 199.5)
    assert jm.duration_s == 199.5 and jm.inserts[0].at_s == 99.5 + SUBSCRIBE_LEAD_S


def test_hook_lead_and_outro_shift_the_map_and_chapters():
    p1 = _plan(_ch("The opening", 60, 160))
    p2 = _plan(_ch("The opening", 20, 120))
    jm = build_join_map(
        video=Path("j.mp4"),
        title="t",
        rounds=["r1", "r2"],
        labels=["A", "B"],
        plans=[p1, p2],
        offsets_s=[0.0, 0.0],
        story_durations_s=[100.0, 100.0],
        card_s=0.0,
        story_card_s=2.5,
        story_jsons=[Path("a"), Path("b")],
        digest_jsons=[None, None],
        transition_s=0.5,
        lead_s=19.0,
        outro=(12.0, Path("outro.mp4")),
    )
    g1, g2 = jm.games
    assert (g1.video_start_s, g2.video_start_s, g2.video_end_s) == (19.0, 118.5, 218.5)
    assert (jm.intro_s, jm.outro_s, jm.duration_s) == (19.0, 12.0, 230.0)
    assert [i.kind for i in jm.inserts] == ["hook", "outro"] and jm.inserts[1].at_s == 218.0
    lines = youtube_chapters(jm).splitlines()
    assert lines[:2] == ["0:00 Intro", "0:19 A: The opening"]
    assert lines[-2:] == ["1:58 B: The opening", "3:38 Outro"]


def test_subscribe_insert_lands_in_the_game_after_n():
    jm = _map(subscribe=(1, 4.0, Path("sub.mov")))
    [ins] = jm.inserts
    assert ins.kind == "subscribe" and ins.at_s == 135.0 + SUBSCRIBE_LEAD_S and ins.dur_s == 4.0


def test_subscribe_after_the_last_game_is_ignored():
    assert _map(subscribe=(2, 4.0, Path("sub.mov"))).inserts == []


def test_youtube_chapters_start_at_zero_and_skip_short_entries():
    text = youtube_chapters(_map())
    lines = text.strip().splitlines()
    assert lines[0] == "0:00 Xayah 5/0/2: The opening"
    assert "1:42 Fight" in lines  # 102.5 s
    assert "2:12 Kai'sa 7/1/3: The opening" in lines  # game card at 132.5 s
    assert all(len(line.split(" ", 1)) == 2 for line in lines)


def test_chapter_name_cleans_planner_play_titles():
    from ai_video_editor.story.join_map import chapter_name

    assert (
        chapter_name(
            "4v3 objective_fight in top_lane, owner follow_up, won 2-for-1, then DRAGON/AIR_DRAGON"
        )
        == "4v3 objective fight · won 2-for-1 · air dragon"
    )
    assert (
        chapter_name("2v2 skirmish in bot_lane, owner initiator_kill, won 2-for-0")
        == "2v2 skirmish · won 2-for-0"
    )
    assert chapter_name("First back") == "First back"


def test_chapter_id_uses_play_id_or_kind_and_start():
    play = Chapter(title="x", start_s=240.6, end_s=260.7, kind="lane", play_id="p01")
    assert chapter_id("r1", play) == "r1:p01"
    early = Chapter(title="The opening", start_s=50.0, end_s=204.1, kind="early")
    assert chapter_id("r1", early) == "r1:early@50.0"


def test_map_v2_carries_hook_block_and_event_times():
    pick = JoinHookPick(
        id="a1b2c3d4",
        chapter_id="r1:p01",
        round="r1",
        chapter_index=1,
        rec_start_s=300.0,
        rec_end_s=312.0,
        offset_s=-10.0,
        at_s=0.0,
        video_start_s=0.0,
        video_end_s=12.0,
        events=[JoinHookEvent(rec_s=310.0, kind="death")],
        chapter_rec_start_s=300.0,
        chapter_rec_end_s=330.0,
    )
    jm = _map(hook=JoinHook(mode="play", title="t", picks=[pick]))
    assert jm.schema_version == 2 and jm.hook.picks[0].chapter_id == "r1:p01"
    assert len(jm.hook.picks[0].id) == 8
    assert jm.games[0].chapters[0].kill_times_s == [110.0]  # rec 100 - offset -10
    assert jm.games[0].chapters[1].death_times_s == [320.0]
    assert JoinMap.model_validate_json(jm.model_dump_json()).hook == jm.hook


def test_map_without_hook_is_v1():
    assert _map().schema_version == 1 and _map().hook is None
    assert _map().options.subscribe_after == 1


def test_hook_block_at_s_follows_rendered_lengths_with_the_freeze():
    from ai_video_editor.story.bookends import HookClip, HookPick
    from ai_video_editor.story.join_map import hook_block

    plan = _plan(_ch("c", 100, 140, kills=[120]))
    clips = [HookClip(Path("s.mp4"), 0.0, 10.0, ""), HookClip(Path("s.mp4"), 0.0, 10.0, "")]
    hook = hook_block(
        ["r1"],
        [plan],
        [HookPick(1, 1), HookPick(1, 1)],
        clips,
        [0.0, 10.9],
        [11.4, 9.0],
        [0.0],
        mode="tease",
        title=None,
    )
    assert [p.at_s for p in hook.picks] == [0.0, 11.4]
