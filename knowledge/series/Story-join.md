---
tags: [series, format]
---
# Story-join
Several games of one series in one video (2-3 per video).

**Tool:** `story-join`.

## First video: "How to destroy lane in 5 minutes" (owner 2026-11-26)
A [[Solo queue]] story-join focused on the laning phase: how the owner wins lane in the first ~5 minutes, played through several games' scenarios. Order (owner): Xayah 5/0/2 → [[Kai'sa 7-1-3 (2026-11-26)]] → [[Yunara 6-0-5 (2026-11-25)]]. First cut: 12:46 (v1). Cut v2 (2026-11-26): 12:57, swipes instead of cards, Kai'sa's real first back (2:24 Cull), Yunara's Cho'Gath kill kept, Xayah trimmed before Outplayed's "Alt Tabbed" screen; `story/joined_2026-11-26_xayah-kaisa-yunara.mp4` (+ `.join.json` map, `.chapters.txt` YouTube chapters), on Drive under `renders/story-join/`.
- **Subscribe overlay:** once, 2 s into the game after game 1 (owner: midpoint for 2 games, 1/3 for 3), bottom centre just above the ability bar (owner 2026-11-26; was top-left). The cursor clicks the greyed thumb (it lights up and bounces), then Subscribe.
- **Between games:** a right-to-left swipe (0.5 s) with an original whoosh; no "Game n" title cards (owner 2026-11-26).
- **Hook (owner 2026-11-27):** the video opens cold on its best plays, cut just before the outcome: the frame freezes, blurs, and a teaser pops ("2v3?"). The opening line ("Want to know the trick to laning?") pops over the first clip. First pick: Yunara's 2v3, then Kai'sa's 4v4. `story-join --hook GAME:CHAPTER[:TEXT] --hook-title "..."`. Owner 2026-11-27 later decided the opening should instead be a montage of the best plays played through their outcomes (`--hook-mode play`: no freeze, opening line only, no op.gg card); `tease` stays the default. The hook is the intro; there is no separate intro.
  - **Picks for this video** (game:chapter, checked against the join map): Yunara 2v3 = `3:2`, Kai'sa 4v4 top = `2:2`, Xayah 2v2 = `1:2`, Yunara 3v2 objective fight at Horde (the dive setup, 8:01) = `3:5`. Command: `story-join <xayah round> <kaisa round> <yunara round> --hook 3:2 --hook 2:2 --hook 1:2 --hook 3:5 --hook-mode play --hook-title "Want to know the trick to laning?" --outro`.
- **Outro (owner 2026-11-27):** 8 s end card: the last frame blurred, "thanks for watching", the like & subscribe animation large and centred, over the owner's music (`WORKSPACE/music/outro.*`, the owner supplies it; TheFatRat's "Monody" is the wish, licence to be checked by the owner; never to Drive). `--outro`.
- **Narrating it:** `/narrate/joined/<joined file stem>` (camera + voice, same page as single stories). The final renders happen on the laptop; the owner records narration on the PC from Drive.
- **Outplayed "Alt Tabbed" endings:** the owner tabs out at VICTORY, so the last 1-2 s of a story can show Outplayed's "video capture paused" screen. Trim it with a `trim` request (end) on the last chapter (Xayah, Yunara).
[[Zeri 8-7-12 (2026-11-23)]] is retired: the owner won't use it.

