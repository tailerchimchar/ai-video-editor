<!-- PROMPT_VERSION: 2 -->
# Valorant editing hints

Your primary job when analyzing a Valorant clip is **sequencing
quality**: pick a trim window that gives the right amount of context
before the payoff and cuts cleanly after. Not too little (payoff
feels random), not too much (dead air, walking, reloads kill the
vibe).

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

If NO payoff moment is visible in the sampled frames, the clip is
low-value — return low confidence and mark payoff_type as `other`.

## Buildup — what MUST be preserved before the payoff

Different plays need different buildup. Judge by what you SEE:

- **Snap kill** (peek → shot within 1s) → 1-3 seconds pre-anchor.
  Anything more is padding.
- **OP hold** (crouched, scoped, still, holding an angle) →
  preserve 3-8 seconds of the hold. The tension IS the moment.
- **Ability setup** (Cypher trap set → wait → kill; Sova recon dart
  → info → engage) → preserve the setup action + brief wait.
- **Rotation clutch** (running through map to a site with alarm
  music-like tension) → up to 5s of the run-in.
- **Multi-frag streak** → walk backward to the FIRST kill in the
  streak, not the last one.

**Hard ceiling: 15 seconds of pre-anchor context.** Anything before
that is almost always waste even for the biggest OP hold.

## Vibe killers — cut these out of the trim window

Things you should chop from the pre-anchor buildup or post-anchor
resolution:

- **Walking through empty callouts** with no enemy in sight — > 2s
  of dead map view = cut
- **Buy phase** at the round start — never include
- **Agent select / loading screen** — never include
- **Post-kill reload** — cut after 1 second unless it's dramatic
- **Camera moving to nothing** — like the player checking their
  crosshair or aiming at floor > 2s = cut
- **Repeated scoreboard-check pauses** — cut
- **Post-round buy-menu screen** — cut

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
  the user's name
- **Marking a downtime clip as payoff_type=kill** — if no kill or
  key moment is visible, use `other` and low confidence

## HUD reference (1920x1080)

- **Killfeed** top-right — user's name on LEFT of the row = they got
  the kill. Read this to identify the anchor moment.
- **Round score** top-center — includes round timer
- **Team icons** top-center (5 each side) — grey out on death
- **Minimap** top-left
- **Loadout / abilities** bottom
- **Crosshair** center — flashes red briefly on hit
