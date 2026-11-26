---
tags: [rule, story, early-game]
taught: 2026-11-26
status: active
---
# Opening runs to the first back
When the first back comes soon, the early game is **one continuous chapter** from the opening to the end of the first back. Never cut to a later fight before the recall.

> Owner: "keep the game going up until the first back. it's important that you show that i crash the wave and recall"

**When the first back is late**, skip the quiet lane phase instead: see [[Skip quiet laning, keep heavy trades]].

**The first-back window** runs from 30 s before the recall channel (8 s) to 8 s after the first purchase after 2:30, so it shows the [[Wave crash]] and the recall.

**Continuous only when** the first-back window starts at most **60 s** after the opening ends (`CONTINUOUS_MAX_GAP_S`). This replaced the old 300 s limit, which kept the whole lane phase.

**Why:** [[Wave crash]] and [[Recall timing]] are the payoff of the laning phase.
**Code:** `league/early_game.py::_through_first_back` and `_first_back`.
**Taught by:** [[Kai'sa 7-1-3 (2026-11-26)]] (the cut from 2:00 to 4:00)
