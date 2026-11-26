---
tags: [rule, story, early-game]
taught: 2026-11-26
status: active
supersedes: "[[Early fight start - solo vs team]]"
---
# Early fight start
Start the video at the **bot-bush trade, approach or ward fight** when one happens: a fight over a ward, a trade in the bottom-middle bush, or a kill. When **nothing** happens (no fight, no approach, no ward killed), start at **0:50**.

> Owner: "if we don't fight, or we don't approach, or the ward isn't killed, or if NOTHING HAPPENS, then you can start at 50s. otherwise you should start from the ward fight."

**Evidence in the data:** a champion kill before 1:05, or a ward kill before 1:15. Either one starts the opening at 0:20.

**HUD-bar check** (replaces the planned vision check):

> Owner: "instead of a VLM checking 0:20-0:50, check 1 frame every 5 s; if there's fighting or people losing HP, start at 0:20"

The owner's own HP and mana bars are read off the HUD once every 5 s from game 0:20 to 0:50.
- HP drops by 3% or more, or mana by 5% or more, between two samples: start at **0:20** ("HP/mana drop at 0:35").
- At least 5 readable samples and no drop: a quiet opening, start at **0:50**.
- No video, or too few readable samples: **0:20**.

**Unsure means 0:20.** The owner chose "never miss a fight" on 2026-11-26.

**Owner overrides:** per-game `trim` requests: [[Kai'sa 7-1-3 (2026-11-26)]] at 0:32, [[Yunara 6-0-5 (2026-11-25)]] at 1:00.

**Code:** `league/early_game.py::_opening` and `_bars_verdict`; bars from `league/hud_bars.py`.
**Taught by:** [[Jhin 8-2-13 (2026-11-20)]] (a real fight at about 0:30) · Kai'sa · Yunara (nothing until 0:50)
