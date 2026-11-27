"""Where a suggestion lands in the rendered story.mp4 (for "jump there")."""

from ai_video_editor.story.planner import Chapter, ChapterEffect, StoryPlan
from ai_video_editor.story.suggest.placement import chapter_footage_starts, video_second


def _plan(*chapters: Chapter, cards: bool = True) -> StoryPlan:
    return StoryPlan(
        chapters=list(chapters),
        total_s=0.0,
        mic_stream=None,
        audio="mixed",
        cards=cards,
    )


def _ch(start: float, end: float, effects: list[ChapterEffect] | None = None) -> Chapter:
    return Chapter(title="c", start_s=start, end_s=end, kind="fight", effects=effects or [])


def test_footage_starts_add_a_card_before_every_chapter():
    plan = _plan(_ch(100, 130), _ch(500, 520))
    assert chapter_footage_starts(plan, 2.5) == [2.5, 35.0]


def test_footage_starts_without_cards():
    plan = _plan(_ch(100, 130), _ch(500, 520), cards=False)
    assert chapter_footage_starts(plan, 2.5) == [0.0, 30.0]


def test_slowmo_in_an_earlier_chapter_pushes_later_chapters():
    slow = ChapterEffect(kind="slowmo", start_s=110, end_s=112, params={"factor": 0.5})
    plan = _plan(_ch(100, 130, [slow]), _ch(500, 520))
    # 30 s of footage + 2 s stretched to 4 s = 32 s.
    assert chapter_footage_starts(plan, 2.5) == [2.5, 37.0]


def test_video_second_inside_a_chapter():
    plan = _plan(_ch(100, 130), _ch(500, 520))
    assert video_second(plan, 1, 510.0, 2.5) == 45.0


def test_video_second_after_a_slowmo_inside_the_same_chapter():
    slow = ChapterEffect(kind="slowmo", start_s=110, end_s=112, params={"factor": 0.5})
    plan = _plan(_ch(100, 130, [slow]))
    assert video_second(plan, 0, 120.0, 2.5) == 2.5 + 22.0


def test_video_second_clamps_to_the_chapter_and_rejects_bad_index():
    plan = _plan(_ch(100, 130))
    assert video_second(plan, 0, 90.0, 2.5) == 2.5
    assert video_second(plan, 3, 110.0, 2.5) is None
