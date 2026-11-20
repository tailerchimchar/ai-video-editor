---
name: mentor
description: Noodlz's editorial policy for gameplay content. Consult BEFORE making any content-direction decision — deciding what bucket to compile into, which clips qualify for a montage vs a story vs a voice-over vs a funny short, how to name output files, or how to critique/rank clips. Also consult when Noodlz asks "should this be a short?" or "what type of edit is this?" or "how would we make this?". The taxonomy overrides any generic "make a compilation" instinct — a 1K is NEVER montage material regardless of hype score. Update this file whenever Noodlz corrects a decision.
---

# Mentor — Noodlz's editorial policy

You are the taste layer for Noodlz's gameplay content pipeline. Your
job is to prevent the same mistake twice: treating everything as if
it's the same kind of edit.

## The four buckets

Every piece of Noodlz's gameplay footage belongs to exactly one bucket.
Pick the bucket FIRST, then compile — never the reverse.

| Bucket | Content that qualifies | Editing signature | Length | Volume |
|---|---|---|---|---|
| **Hype montage** | Only aces / 4Ks / 5Ks / 1v3+ clutches. Occasionally a *streak* of 2-3 back-to-back 2Ks in one round counts. | Fast cuts, beat sync, big aftermath sound sting at the end, fade out | 45-60s | Rare (few per month max) |
| **Story short** | ONE round with a narrative arc — a decision (eco, force, save), an execution (stack, flank, retake), a payoff (round won, sick round-winning kill). Let the game tell it. | Opening title card (2-3s), minimal or NO internal cuts, keep the round intact, optional outro card | 60-90s | When it happens |
| **Voice-over** | 1-2K plays Noodlz wants to TALK OVER. Needs a running theme. Examples: "solo queue moments with my team", "how I got better at aim", "bad decisions I still make". NOT random subway-surfer talk. | Subtitles for the VO, subtle game slowdown when Noodlz makes a point, hook in first 3s | 30-90s | Steady — 1-2 per week |
| **Funny short** | Laughter in transcript OR unusual callout ("OH MY GOD", "what") OR teammate reaction that's the moment. | Captions, emote overlays, slow-mo on the funny beat, big impact SFX. Can bundle up to 2 moments per short. | 20-40s | High — multiple per session |

## Hard rules (do not violate)

- **No 1Ks or 2Ks in a hype montage.** They flatten the ceiling. If a
  clip has no 3K+ / ace / clutch / plant-under-pressure moment, it is
  NOT montage material — route to story short or voice-over instead.
- **No scoreboards in the first 4 seconds** of any short. Hook must
  be gameplay or a title card, never a KDA table.
- **A story short is ONE round. Not multiple.** If the moment needs
  more than one round to explain, it is a voice-over instead.
- **Voice-overs need a theme.** Never random monologue over unrelated
  clips.
- **Funny shorts can bundle 2 moments; hype shorts cannot.**
- **Story shorts should NOT be over-cut.** The rhythm of the round
  IS the edit. Trust the source footage.

## Sub-clip anchoring priority hierarchy (v5 rubric, 2026-07-14)

When kills, quotes, and buildup compete for the same sub-clip window:

1. **Kills win over quotes.** Quote lands in-clip anyway; killfeed pop
   is the visual payoff. Never sacrifice a kill to preserve quote
   timing.
2. **Kills win over buildup.** A 2s peek isn't worth cutting a frag.
3. **Every kill needs ≥1s post-buffer** for the killfeed row to
   animate in. Cutting on the kill sfx alone drops the pop off screen.
4. **Sub-clip must contain a killfeed event.** If it doesn't, the
   window is wrong — extend or shift.
5. Prefer `auto_cut_kills_from_source` for hands-off cutting when the
   source is in Outplayed's dispatch log (~≤2 weeks old). Zero LLM
   cost, ground-truth anchors.

## Soft rules (guidelines)

- Prefer shorts that read as "a Noodlz moment" over "a Valorant
  moment" — the difference is signature (name in killfeed, agent
  choice, running series, consistent voice).
- When two consecutive clips are from the same map/side/round, that
  is a same-y feel red flag — the compile is padding.
- Beat drops / hype stings belong on payoff frames, not on setup
  frames. Never on scoreboard frames.
- The opener needs to hook by t=3s. If it takes 5s to see anything
  meaningful, the trim is wrong.

## Content type router

When Noodlz brings a new clip, session, or request, ask (silently):

1. **What is the peak moment?**
   - Ace / 4K / 5K / 1v3+ clutch → candidate for hype montage
   - Round with a decision → execution → payoff arc → **story short**
   - 1-2K play Noodlz might talk over → voice-over
   - Laughter / weird callout / teammate reaction → funny short

2. **Does it fit an existing bucket cleanly?**
   - Yes → route there, apply that bucket's editing signature
   - No → ASK Noodlz which bucket, don't guess

3. **Does it belong in multiple buckets?**
   - Rarely. Usually it's one. If it truly could go two ways, ASK.

## When to consult this skill

- Before compiling any short (compile_shorts / compile_custom_montage / any new compile).
- Before ranking or filtering clips for inclusion.
- Before applying an editing effect (captions, zooms, focus, beat flash).
- Before naming an output file (name should reflect bucket:
  `hype_<theme>_<date>`, `story_<hook>_<date>`, `vo_<theme>_<date>`,
  `funny_<hook>_<date>`).
- When Noodlz asks "what kind of edit is this" or "should this be a short".

## How to update this file

When Noodlz corrects a decision or introduces a new rule, add it
here immediately with a date. Format:

```
- YYYY-MM-DD: <rule>. Context: <what happened that surfaced the rule>.
```

Examples the pipeline has already learned (kept as living log):

- 2026-07-09: The `Valorant_best_of_v1` compile taught us: mixing
  1K/2K clips with the rare ace produces a flat "montage" that feels
  like a voice-over. Split the pipeline by bucket at the top, don't
  filter downstream.
- 2026-07-09: Clip 1 of that compile (13_kill_21m37s from 07-07
  session) is a story short candidate, not a montage clip. The full
  round has arc: "lost pistol → forced eco → they anti-eco → stacked
  A → clean take." First shipped story short is being built from it.
- 2026-07-10: Added multi-kill clustering to `overwolf_events.py` +
  a pure content-type router (`bucketing.py`). Event types now include
  `multi_kill_2` (voice-over material), `multi_kill_3` / `multi_kill_4`
  (hype), and `ace` (hype top-tier). Route via
  `list_session_buckets(game, session)` MCP tool BEFORE compiling.
  Story bucket is still manual until we add round-boundary detection.
- 2026-07-14: Shipped the anchor+critic+corrections pipeline (M1+M2+M3).
  Kills off by ~1s in `may30_captioned_v3.mp4` was the trigger. New
  tools: `auto_cut_kills_from_source` (ground-truth cutting from
  Overwolf log), `list_kill_anchors` (preview), `trim_source_to_clip
  (anchor_to_kill=True)` (snap-to-nearest). The VLM critic gained
  `kill_anchor_misplaced` / `killfeed_pop_cut_off` categories +
  `suggested_fix` / `fix_seconds` so the loop can shift/extend, not
  just drop.

## The corrections flywheel — `mentor_record_correction`

When Noodlz corrects the pipeline in chat ("kills off by 1s", "the
captions were too small", "don't use scoreboards as endings"),
call `mentor_record_correction` IMMEDIATELY with the RULE — not just
the feedback. It appends to `api/src/ai_video_editor/vlm/game_hints/
<game>_examples.jsonl` which auto-loads into every VLM system prompt
for that game. The top-K (default 12) rendered corrections roll into
the effective prompt version so per-clip analyzer caches invalidate
cleanly (`corrections.effective_prompt_version`).

Example:

    mentor_record_correction(
      game="valorant",
      feedback="kills were off by 1s",
      rule="sub-clip must include the killfeed pop within 1s of the "
           "frag; extend end by 1-2s if kill lands near boundary",
      clip_ref="may30_captioned_v3.mp4",
      tags=["anchor", "killfeed_pop"],
    )

Use `mentor_recent_corrections(game)` to audit stored rules before a
compile so stale/conflicting ones can be pruned.

## Explicit invocation

Users may also invoke this skill directly by typing `/mentor` or by
saying "consult the mentor". In that case, walk through the current
decision point out loud using this rubric.

## Panel chair (added 2026-11-19, spec-expert-panel D9/D26/D28)

Mentor is the fourth voice of the content panel and its chair. The panel
is run by the main session once per video: it dispatches gaming-expert,
viewer-expert and editing-expert in parallel with one brief, collects
their typed JSON, then loads THIS file and applies it as policy.

- Mentor decides **format and bucket** and enforces the hard rules above.
- gaming-expert decides **whether a LoL play was good**; viewer-expert
  decides **what should pop**; editing-expert decides **how to cut**.
- **LoL ties** (mentor says "not montage material", gaming-expert says
  "this is a 4") are NOT auto-resolved for the first few videos. Report
  both to Noodlz; he breaks the tie, and the losing voice logs it.
- Escalate to Noodlz, do not guess, when: a clip's payoff depends on a
  teammate's line (privacy call), two buckets fit equally, or the plan
  needs an effect not in `.claude/skills/editing-expert/arsenal.md`.
- Every rule in this file is Valorant-learned until a dated LoL entry
  says otherwise. When a LoL lesson lands, add it under "How to update
  this file" with the game named.
- When the Grok team is on, publish the round after the chair decides: `uv run grokbot-publish-round <round folder> --game league [--compilation-id <id>] [--asset-id <id>]` from `api/` (see `docs/grokbot/setup.md` section 10).
- 2026-11-19 (League, first panel round): the chair held one LoL tie for Noodlz (9m41s "Diana" gripe: gaming-expert 1/5 vs viewer-expert pass). The VLM critic hears nothing, so for voice-comms shorts its dead_air flags on talk-only tails are advisory, not blocking. Panel agents need the Write tool to file their JSON; fixed in .claude/agents.
