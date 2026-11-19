<!-- PROMPT_VERSION: 5 -->
# Valorant editing hints

Your primary job when analyzing a Valorant clip is **sequencing
quality**: pick a trim window that gives the right amount of context
before the payoff and cuts cleanly after. Not too little (payoff
feels random), not too much (dead air, walking, reloads kill the
vibe).

**MANY clips have MULTIPLE payoffs** — the user often manually
clipped their own VODs so the clip has a kill near the beginning
AND another one near the end. When you see 2+ kills BY THE USER in
the clip, list them ALL in the `additional_moments` array. The
compile will render each moment as its own sub-clip in the montage
— splitting one long padded clip into several tight hype moments
is much better than trying to compress it into one window.

## The payoff (the "anchor" moment)

The frame in the clip where the highlight moment happens. In
priority order:

1. **Ace** — the 5th enemy dying, killfeed showing your name repeatedly
2. **Clutch shot** — the deciding kill in a 1vX where you were losing
3. **OP one-tap** — a single sniper shot that connects on the head
4. **Multi-kill sequence** — 2+ kills within ~5 seconds
5. **Solo kill on a key duelist** — a Jett dash-kill, a Reyna outplay
6. **Standard kill** — killfeed shows your name on the left, someone
   dies on the right
7. **Plant / defuse under pressure** — bomb interaction while enemies
   are alive nearby

## Deaths — nuanced rule

The user's death by itself is NOT a highlight. But dying AFTER
getting kills IS a highlight — a multi-kill trade, a heroic 1vX
that fell just short, or a clutch attempt that got the majority
of the round.

**Drop rule** (set `payoff_type = "other"` + `confidence <= 0.3`):
- The clip shows ONLY the user dying with NO visible kills BY THEM
- OR the clip is buy phase / agent select / spectator downtime

**Keep rule** — this IS a valid highlight:
- User got 1+ kills, then died — use `outplay` if the trade felt
  earned (1v2, 1v3 attempt that landed some), or `kill` /
  `multi_kill` normally
- User dies AFTER the payoff moment as part of the sequence
  (traded death post-multi-kill is a hype moment)

When user dies MID-CLIP but there are more payoffs (kills)
BEFORE the death, list all pre-death kills in `additional_moments`
and cut the `resolution_end` right around the death (don't linger
on the death cam / spectator view).

Trust the killfeed and the crosshair-on-target — those are your
kill signals, not "the player didn't die."

## Buildup — what MUST be preserved before the payoff

Different plays need different buildup. Judge by what you SEE:

- **Snap kill** (peek → shot within 1s) → **1-2 seconds** pre-anchor.
  Anything more is padding. Err on cutting MORE aggressively when the
  pre-shot frames are repetitive positioning (same corner, same aim).
- **OP hold** (crouched, scoped, still, holding an angle) → preserve
  **4-8 seconds** of the hold. The tension IS the moment. **When in
  doubt on an OP hold, extend the buildup by 1-2 more seconds
  rather than cut early** — a slightly-too-long OP setup reads as
  intentional; a too-short one reads as abrupt.
- **Ability setup** (Cypher trap set → wait → kill; Sova recon dart
  → info → engage) → preserve the setup action + brief wait.
- **Rotation clutch** (running through map to a site with alarm
  music-like tension) → up to 5s of the run-in.
- **Multi-frag streak** → walk backward to the FIRST kill in the
  streak, not the last one.

**Hard ceiling: 12 seconds of pre-anchor context.** Anything before
that is almost always waste even for the biggest OP hold. (Lowered
from 15s in v2 based on feedback that openers still felt padded.)

## Vibe killers — cut these out of the trim window

Things you should chop from the pre-anchor buildup or post-anchor
resolution:

- **Walking through empty callouts** with no enemy in sight — > 2s
  of dead map view = cut. **Even 1.5s feels padded — trim tighter.**
- **Buy phase** at the round start — never include
- **Agent select / loading screen** — never include
- **Post-kill reload** — cut after 1 second unless it's dramatic
- **Camera moving to nothing** — like the player checking their
  crosshair or aiming at floor > 1.5s = cut
- **Repeated scoreboard-check pauses** — cut
- **Post-round buy-menu screen** — cut
- **Idle scoped hold with NO enemy visible / hearable** — count
  every second of this against the 12s buildup ceiling. Trim.

## Post-anchor resolution — how much to keep AFTER the kill

- **Snap kill** → 2-3 seconds (killfeed confirms, brief pause)
- **Multi-kill mid-sequence** → 3-6 seconds (see if follow-up
  kills come)
- **Ace / clutch payoff** → 4-6 seconds (celebration / voice line
  moment)
- Never more than **6 seconds** of post-anchor unless another
  payoff is coming.

## Cut on natural audio drops

When choosing the exact cut point, prefer these visual proxies for
audio quiet (since you can't hear):

- Player stops moving / reloads finish
- Round-end white flash
- Next round buy phase starts
- Enemy visibility drops (nobody on screen, no killfeed)

## Common mistakes to avoid

- **Starting exactly at the kill** — feels abrupt, viewer can't tell
  what happened
- **Ending during the reload animation** — feels incomplete
- **Preserving 10s of walking because Outplayed cut it that way** —
  Outplayed pads clips; trim aggressively
- **Confusing teammate kills for user kills** — the killfeed shows
  ALL kills. Only trim around kills where the LEFT side (killer) is
  the user's name (see HUD reference)
- **Marking a downtime OR DEATH clip as a real payoff** — if no kill
  BY THE USER is visible, use `other` and low confidence. The
  compile drops these clips entirely.
- **Trusting the fact that Outplayed cut this clip** — Outplayed
  detects ANY event including deaths. Not every clip is a highlight.

## HUD reference (1920x1080)

- **Killfeed** top-right — user's name on LEFT of the row = they got
  the kill. Read this to identify the anchor moment.
- **Round score** top-center — includes round timer
- **Team icons** top-center (5 each side) — grey out on death
- **Minimap** top-left
- **Loadout / abilities** bottom
- **Crosshair** center — flashes red briefly on hit
- **Player death** — full-screen colored overlay + spectator UI
  (you're now watching a teammate). If you see this, the user died.

## Multi-payoff clips (VERY COMMON — read this carefully)

The user often pre-clips their own VOD to keep the "hype" bits.
That means a single clip you're looking at often has 2 or 3 kills
by the user, spread across the clip's duration.

**When you see multiple kills by the user in the sampled frames:**

1. Pick the SINGLE most impactful moment as the primary payoff
   (fill the top-level `payoff_anchor_seconds`, `buildup_start_seconds`,
   `resolution_end_seconds`, `payoff_type`).
2. List EVERY OTHER kill moment in `additional_moments`, each as a
   full `{buildup_start, payoff_anchor, resolution_end,
   payoff_type, description, confidence}` object.

Each additional moment's trim window follows the same rules as the
primary — 2s pre for snap kills, 4-8s pre for OP holds, 2-3s post.
Windows may NOT overlap; if two kills are within 4 seconds of each
other, MERGE them into one wider window with a single anchor at
the more impactful kill.

Concrete example: Outplayed clip is 40 seconds. User gets:
- t=3s: snap kill on entry
- t=17s: OP hold → one-tap
- t=34s: multi-kill on retake

You'd return:
- Primary (highest impact — the multi-kill): buildup=30, anchor=34, end=38
- additional_moments = [
    {buildup=1, anchor=3, end=6, type="kill"},
    {buildup=12, anchor=17, end=21, type="kill"},
  ]

The compile then renders 3 tight sub-clips from that single source
file. Total: ~13 seconds of hype vs 40s of padding.

## Priority hierarchy (v5 — the load-bearing rule)

When trim windows compete, resolve in this exact order:

1. **Kills win over quotes.** If a funny voice-line lands at t=X but a
   kill lands at t=X+2, anchor the window on the kill. The quote
   still appears in-clip; the killfeed pop is the visual payoff.
2. **Kills win over buildup.** A 2s peek is not worth cutting a kill.
   If tightening buildup would push the kill to the clip's last 500ms,
   keep the buildup instead.
3. **Kills need ≥1s post-buffer.** The killfeed row animates in ~0.5-1s
   AFTER the frag. Cutting on the kill sfx alone drops the pop off
   screen. Always leave 1s minimum after the kill instant.
4. **Sub-clip must contain a verifiable killfeed event.** If the window
   you propose doesn't include the killfeed pop, you picked the wrong
   window — extend or shift until it does.
5. **Quote-only clips (setup, callout, reaction) are the exception, not
   the default.** Only mark a clip "quote_only" when there is no kill
   within ±10s of the quote. Otherwise anchor to the kill.

## Rubric changelog (for reference — do not use as rules)

- v2 → v3: hard-drop death clips (`payoff_type="other"` + low
  confidence); snap-kill buildup 3s → 2s; ceiling 15s → 12s;
  err longer for OP holds when unsure.
- v3 → v4: soften death rule (kills-before-death is valid); add
  `additional_moments` array for multi-payoff clips (user pre-clips
  their own VOD so most clips have 2+ kills); tighter guidance on
  merging close-together kills.
- v4 → v5: explicit priority hierarchy (kills > quotes > buildup);
  ≥1s post-buffer requirement (killfeed pop must be in-frame);
  quote-only clips demoted to exception status. Motivation: mechanical
  fix layer (`anchoring.py`) now handles snap-to-kill for clips with
  Overwolf ground truth — the rubric enforces the same rule for VLM-
  driven windows on clips without ground truth.
