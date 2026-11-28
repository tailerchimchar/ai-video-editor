---
tags: [format]
---
# Narration
The owner records voice, and optionally camera, over a finished story in the
web page (`/narrate/story/<round>`). Edits come first, then narration
([[Story format]]).

## Voice and style (owner 2026-11-26)
- **Narration:** explain what is going on and why (Mehalic Pops / Isab style coaching: waves, trades, recall timing, cooldowns), but keep it funny and self-aware (Pianta style). Original lines, never their bits.
- **Editing (target):** Jawgemo / Zekken style: fast cuts, punch-in zooms, meme SFX, text pops, freeze-frames. Most of these are not in the arsenal yet. Built: the comic zoom-out (owner 2026-11-27, Isab style, for something funny or strange): the picture shrinks inside black borders, a `zoom_out` story effect.
- **Script:** bullet points per beat, not a teleprompter. First one: [[How to destroy lane in 5 minutes - script v1]].

## How the owner records (2026-11-27)
- **In pieces, not one take.** Seek to a spot, record, stop; many short takes, each pinned to the second it started at.
- **Record over a mistake.** A new take replaces what it covers; older takes under it are trimmed, split or dropped, nothing else moves.
- **Fix on the timeline:** trim the edges, split, nudge (±3 s) against the picture, mute, gain per take.
- **Game audio:** draw a region on the game lane to make that stretch softer or louder (100 / 50 / 25 / 0 %). One duck knob sets how far the game dips under the voice.
- **What you hear in the page is what exports**, but the export is the reference: check `narrated.mp4` before trusting a mix.
- **Camera box:** shown only when switched on; drag to move, corner to resize.
- **Headphones always**, or the game bleeds into the mic.
- **Hook (owner 2026-11-27):** a montage of the best plays played through; the voice carries the 10/0 stat and one credential sentence over it; opening line only, no op.gg card. Built as `--hook-mode play` in [[Story-join]].
