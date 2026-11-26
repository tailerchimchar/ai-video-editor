---
tags: [rule, story, early-game]
taught: 2026-11-26
status: active
supersedes: "[[Early fight start - solo vs team]]"
---
# Early fight start
Start the video at the **bot-bush trade, approach or ward fight** when one happens: a fight over a ward, a trade in the bottom-middle bush, or a kill. When **nothing** happens (no fight, no approach, no ward killed), start at **0:50**.

> Owner: "if we don't fight, or we don't approach, or the ward isn't killed, or if NOTHING HAPPENS, then you can start at 50s. otherwise you should start from the ward fight."

**Unsure means 0:20.** Riot data can't prove nothing happened: there are no damage events, and positions come only once a minute. So when unsure, the opening starts at 0:20 and never misses a fight. The owner chose "never miss a fight" on 2026-11-26.

**Evidence in the data:** a champion kill before 1:05, or a ward kill before 1:15. Both only explain the 0:20 start.

**Future step:** a vision check that confirms a quiet opening would move it to 0:50 (`QUIET_START_S`).

**Owner overrides:** per-game `trim` requests: [[Kai'sa 7-1-3 (2026-11-26)]] at 0:32, [[Yunara 6-0-5 (2026-11-25)]] at 1:00.

**Code:** `league/early_game.py::_opening`.
**Taught by:** [[Jhin 8-2-13 (2026-11-20)]] (a real fight at about 0:30) · Kai'sa · Yunara (nothing until 0:50)
