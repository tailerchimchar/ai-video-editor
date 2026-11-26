"""Item B-roll: when the owner names an item on the mic, show its purchase.

`plan_broll` matches item mentions in the owner's mic lines to the owner's
purchase of that item nearest in time (the one before the line preferred,
within BROLL_MATCH_S) and returns one `BRollInsert` per (line, item),
dropping inserts whose cutaway window overlaps an earlier one.
`place_broll` attaches each insert to the chapter that contains its window,
or adds a short `broll` chapter when the window lies outside every chapter.

Privacy: transcript text is read only to match item names; it is never
stored or logged. Inserts carry item ids and times only.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel

from ..league.items import ItemInfo, Purchase, match_item_mentions

if TYPE_CHECKING:
    from .planner import StoryPlan

_log = logging.getLogger(__name__)

BROLL_MATCH_S = 6 * 60.0  # a purchase further than this from the line is not it
BROLL_LEAD_S = 2.0  # footage starts this long before the purchase
BROLL_MIN_S = 3.0
BROLL_MAX_S = 8.0
FIRST_BACK_INSERT_S = 4.5  # the first-back item overlay (owner 2026-11-26)


class BRollInsert(BaseModel):
    item_id: int
    mic_start_s: float  # recording seconds of the narration line
    mic_end_s: float
    src_start_s: float  # recording seconds of footage to show (purchase - 2 s)
    src_end_s: float  # src_start_s + (mic_end_s - mic_start_s), min 3 s, max 8 s
    build_path: bool = True

    @property
    def dur_s(self) -> float:
        return round(self.src_end_s - self.src_start_s, 3)

    @property
    def window_end_s(self) -> float:
        """End of the cutaway on the recording's timeline (starts at mic_start_s)."""
        return round(self.mic_start_s + self.dur_s, 3)


def _nearest_purchase(t: float, times: list[float]) -> float | None:
    before = [p for p in times if p <= t and t - p <= BROLL_MATCH_S]
    if before:
        return max(before)
    after = [p for p in times if p > t and p - t <= BROLL_MATCH_S]
    return min(after) if after else None


def plan_broll(
    mic_rows: list[dict], purchases: list[Purchase], items: dict[int, ItemInfo]
) -> list[BRollInsert]:
    """Inserts in mic order, non-overlapping (the earlier one wins)."""
    by_item: dict[int, list[float]] = {}
    for p in purchases:
        by_item.setdefault(p.item_id, []).append(p.t_rec_s)
    candidates: list[BRollInsert] = []
    for row in sorted(mic_rows, key=lambda r: float(r.get("start_seconds") or 0.0)):
        start = float(row.get("start_seconds") or 0.0)
        end = float(row.get("end_seconds") or start)
        for iid in match_item_mentions(str(row.get("text") or ""), items):
            bought = _nearest_purchase(start, by_item.get(iid, []))
            if bought is None:
                continue
            dur = min(BROLL_MAX_S, max(BROLL_MIN_S, end - start))
            src = round(max(0.0, bought - BROLL_LEAD_S), 3)
            candidates.append(
                BRollInsert(
                    item_id=iid,
                    mic_start_s=start,
                    mic_end_s=end,
                    src_start_s=src,
                    src_end_s=round(src + dur, 3),
                    build_path=bool(items[iid].from_ids),
                )
            )
    kept: list[BRollInsert] = []
    for ins in candidates:
        if kept and ins.mic_start_s < kept[-1].window_end_s:
            _log.info(
                "story broll: item %d at %.1fs overlaps; dropped", ins.item_id, ins.mic_start_s
            )
            continue
        kept.append(ins)
    return kept


def first_back_insert(purchase: Purchase, item: ItemInfo) -> BRollInsert:
    """The first back's main item over its own purchase footage (owner
    2026-11-26: "once we recall get the item i bought"): the cutaway starts
    BROLL_LEAD_S before the purchase at the same moment, so the viewer sees
    the shop footage itself with the icon and build path on top. Needs no
    mic line."""
    start = round(max(0.0, purchase.t_rec_s - BROLL_LEAD_S), 3)
    end = round(start + FIRST_BACK_INSERT_S, 3)
    return BRollInsert(
        item_id=item.id,
        mic_start_s=start,
        mic_end_s=end,
        src_start_s=start,
        src_end_s=end,
        build_path=bool(item.from_ids),
    )


def with_first_back(
    plan: StoryPlan, first_back: list[BRollInsert], inserts: list[BRollInsert]
) -> list[BRollInsert]:
    """`inserts` plus each first-back insert that sits inside a chapter (it
    never adds footage of its own) and overlaps no other insert, by time."""
    kept = [
        fb
        for fb in first_back
        if any(c.start_s <= fb.mic_start_s and fb.window_end_s <= c.end_s for c in plan.chapters)
        and not any(fb.mic_start_s < i.window_end_s and i.mic_start_s < fb.window_end_s
                    for i in inserts)
    ]  # fmt: skip
    return sorted(inserts + kept, key=lambda i: i.mic_start_s)


def place_broll(
    plan: StoryPlan, inserts: list[BRollInsert], items: dict[int, ItemInfo]
) -> StoryPlan:
    """Attach inserts to the chapter holding their window; a window outside
    every chapter becomes its own `broll` chapter; one straddling a chapter
    edge is dropped (it would cut the chapter's footage mid-insert)."""
    from .planner import Chapter

    chapters = [c.model_copy(update={"broll": list(c.broll)}) for c in plan.chapters]
    added: list[Chapter] = []
    for ins in inserts:
        lo, hi = ins.mic_start_s, ins.window_end_s
        home = next((c for c in chapters if c.start_s <= lo and hi <= c.end_s), None)
        if home is not None:
            home.broll.append(ins)
            continue
        if any(lo < c.end_s and c.start_s < hi for c in chapters + added):
            _log.info("story broll: item %d at %.1fs straddles a chapter; dropped", ins.item_id, lo)
            continue
        name = items[ins.item_id].name if ins.item_id in items else "Item"
        added.append(Chapter(title=name, start_s=lo, end_s=hi, kind="broll", broll=[ins]))
    merged = sorted(chapters + added, key=lambda c: c.start_s)
    total = round(sum(c.end_s - c.start_s for c in merged), 3)
    return plan.model_copy(update={"chapters": merged, "total_s": total})
