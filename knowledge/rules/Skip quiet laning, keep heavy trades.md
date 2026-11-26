---
tags: [rule, story, early-game]
taught: 2026-11-26
status: active
---
# Skip quiet laning, keep heavy trades
When the first back is late, cut the boring lane phase. Keep only the heavy trades and the 30 s before the recall, then show the item bought.

> Owner: "usually, if the enemy is good or it's an even lane, no one really recalls until like maybe 4 minutes or 5 minutes. some times 6 minutes. When this happens, we need to skip like a lot of the boring laning phase EXCEPT IF THERE ARE HEAVY TRADES. otherwise we need to skip to like the first recall or like the 30 seconds leading up to the recall and then once we recall get the item i bought pls"

**What stays** when the first-back window starts more than 60 s after the opening ends:
- the opening;
- every **heavy trade**: the owner's HP bar drops by 20% or more between two HUD samples (one every 5 s), shown from 10 s before to 8 s after ("Heavy trade" chapter);
- the level-3 trade, close-call and jungle-move windows;
- plays with kills (they come in as play chapters);
- the first back: 30 s lead-up + 8 s recall channel, to 8 s after the purchase.

Everything else in the lane phase is skipped.

**The item:** at the first back, the most expensive item bought in that shop visit (within 10 s) is shown with its icon and build path over the purchase footage.

**Related:** [[Opening runs to the first back]] · [[Early fight start]] · [[Recall timing]]
**Code:** `league/early_game.py::early_segments` (`_heavy_trades`, `lane_phase_s`), `league/items.py::first_back_main_item`, `story/broll.py::first_back_insert`, `story/cli.py::load_first_back_broll`.
