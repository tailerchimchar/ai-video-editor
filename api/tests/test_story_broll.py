"""Item B-roll: mention -> purchase matching, overlap dropping, placement on a plan."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_video_editor.league.items import ItemInfo, Purchase
from ai_video_editor.story.broll import BRollInsert, place_broll, plan_broll
from ai_video_editor.story.planner import Chapter, StoryPlan

YUN_TAL, KRAKEN, CULL, BF = 3032, 6672, 1083, 1038


def _items(tmp: Path = Path("icons")) -> dict[int, ItemInfo]:
    def it(i, name, frm=()):
        return ItemInfo(id=i, name=name, icon_path=tmp / f"{i}.png", from_ids=list(frm))

    return {
        YUN_TAL: it(YUN_TAL, "Yun Tal Wildarrows", (BF, 1042)),
        KRAKEN: it(KRAKEN, "Kraken Slayer", (BF,)),
        CULL: it(CULL, "Cull"),
        BF: it(BF, "B. F. Sword"),
    }


def _row(text: str, start: float, end: float) -> dict:
    return {"text": text, "start_seconds": start, "end_seconds": end, "sentiment_score": 0.1}


def test_mention_matches_nearest_purchase_before_line():
    purchases = [
        Purchase(item_id=YUN_TAL, t_rec_s=300.0),
        Purchase(item_id=YUN_TAL, t_rec_s=540.0),
        Purchase(item_id=YUN_TAL, t_rec_s=610.0),  # after the line: not preferred
    ]
    out = plan_broll([_row("went yun tal this game", 600.0, 604.0)], purchases, _items())
    assert len(out) == 1
    ins = out[0]
    assert ins.item_id == YUN_TAL
    assert (ins.mic_start_s, ins.mic_end_s) == (600.0, 604.0)
    assert ins.src_start_s == 538.0  # purchase - 2 s
    assert ins.src_end_s == 542.0  # + mic length (4 s)
    assert ins.build_path is True


def test_after_purchase_used_when_none_before_within_limit():
    purchases = [Purchase(item_id=KRAKEN, t_rec_s=100.0), Purchase(item_id=KRAKEN, t_rec_s=650.0)]
    out = plan_broll([_row("kraken next", 600.0, 603.0)], purchases, _items())
    assert [i.src_start_s for i in out] == [648.0]


def test_purchase_beyond_six_minutes_is_ignored():
    purchases = [Purchase(item_id=KRAKEN, t_rec_s=200.0)]  # 400 s before the line
    assert plan_broll([_row("kraken", 600.0, 603.0)], purchases, _items()) == []


def test_unknown_or_unbought_item_is_ignored():
    purchases = [Purchase(item_id=CULL, t_rec_s=100.0)]
    rows = [_row("infinity edge is good", 120.0, 124.0), _row("kraken slayer", 130.0, 134.0)]
    assert plan_broll(rows, purchases, _items()) == []


def test_duration_clamped_to_three_and_eight_seconds():
    purchases = [Purchase(item_id=CULL, t_rec_s=50.0), Purchase(item_id=KRAKEN, t_rec_s=400.0)]
    rows = [_row("cull", 60.0, 61.0), _row("kraken slayer now", 410.0, 430.0)]
    out = plan_broll(rows, purchases, _items())
    assert [round(i.src_end_s - i.src_start_s, 3) for i in out] == [3.0, 8.0]
    assert out[0].build_path is False  # Cull has no components


def test_src_start_never_negative():
    out = plan_broll([_row("cull", 10.0, 14.0)], [Purchase(item_id=CULL, t_rec_s=1.0)], _items())
    assert out[0].src_start_s == 0.0


def test_overlapping_inserts_dropped_first_kept():
    purchases = [Purchase(item_id=YUN_TAL, t_rec_s=500.0), Purchase(item_id=KRAKEN, t_rec_s=505.0)]
    rows = [
        _row("yun tal over kraken slayer", 600.0, 605.0),  # two items, same window
        _row("kraken", 603.0, 606.0),  # overlaps the first line's window
        _row("kraken slayer", 700.0, 704.0),
    ]
    out = plan_broll(rows, purchases, _items())
    assert [(i.item_id, i.mic_start_s) for i in out] == [(YUN_TAL, 600.0), (KRAKEN, 700.0)]


def _plan(chapters: list[Chapter]) -> StoryPlan:
    return StoryPlan(
        chapters=chapters,
        total_s=sum(c.end_s - c.start_s for c in chapters),
        mic_stream=2,
        audio="mic_over_game",
        cards=False,
    )


def _ins(mic_start: float, dur: float = 4.0, item: int = YUN_TAL) -> BRollInsert:
    return BRollInsert(
        item_id=item,
        mic_start_s=mic_start,
        mic_end_s=mic_start + dur,
        src_start_s=100.0,
        src_end_s=100.0 + dur,
    )


def test_place_broll_attaches_inside_chapter_and_adds_broll_chapter_outside():
    plan = _plan(
        [
            Chapter(title="a", start_s=10.0, end_s=40.0, kind="early"),
            Chapter(title="b", start_s=200.0, end_s=230.0, kind="fight"),
        ]
    )
    out = place_broll(plan, [_ins(20.0), _ins(100.0, dur=5.0, item=KRAKEN)], _items())
    assert [c.kind for c in out.chapters] == ["early", "broll", "fight"]
    assert out.chapters[0].broll == [_ins(20.0)]
    broll = out.chapters[1]
    assert (broll.start_s, broll.end_s) == (100.0, 105.0)
    assert broll.title == "Kraken Slayer"
    assert broll.broll == [_ins(100.0, dur=5.0, item=KRAKEN)]
    assert out.total_s == pytest.approx(65.0)


def test_place_broll_drops_insert_straddling_a_chapter_edge():
    plan = _plan([Chapter(title="a", start_s=10.0, end_s=40.0, kind="fight")])
    out = place_broll(plan, [_ins(38.0)], _items())
    assert len(out.chapters) == 1 and out.chapters[0].broll == []


def test_story_json_carries_only_ids_and_times():
    plan = _plan([Chapter(title="a", start_s=10.0, end_s=40.0, kind="fight")])
    out = place_broll(plan, [_ins(20.0)], _items())
    dumped = out.model_dump()["chapters"][0]["broll"][0]
    assert set(dumped) == {
        "item_id",
        "mic_start_s",
        "mic_end_s",
        "src_start_s",
        "src_end_s",
        "build_path",
    }


# --- first-back item insert (owner 2026-11-26) ---


def test_first_back_insert_shows_the_purchase_footage_itself():
    from ai_video_editor.league.items import ItemInfo, Purchase
    from ai_video_editor.story.broll import first_back_insert

    info = ItemInfo(id=1038, name="B. F. Sword", icon_path=Path("1038.png"), gold_total=1300)
    ins = first_back_insert(Purchase(item_id=1038, t_rec_s=500.0), info)
    assert (ins.item_id, ins.mic_start_s, ins.src_start_s) == (1038, 498.0, 498.0)
    assert 4.0 <= ins.dur_s <= 5.0
    assert ins.mic_end_s == ins.src_end_s
    assert ins.build_path is False
    built = ItemInfo(id=3031, name="IE", icon_path=Path("3031.png"), from_ids=[1038])
    assert first_back_insert(Purchase(item_id=3031, t_rec_s=1.0), built).src_start_s == 0.0
    assert first_back_insert(Purchase(item_id=3031, t_rec_s=1.0), built).build_path is True
