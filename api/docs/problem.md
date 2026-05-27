# The Problem

## Context

Outplayed (an Overwolf app) auto-records gameplay. A regular player ends
up with two kinds of files in `Videos/Overwolf/Outplayed/<game>/...`:

- **Full session recordings** — 15–100+ minute VODs of whole play sessions.
- **Short event clips** — Outplayed already auto-detects notable moments
  (kills, multikills) and saves them as separate ~10–60 s files.

Probing a real library showed a clean bimodal duration split — short
clips clustered ≤ ~40 s, recordings ≥ ~900 s, with **nothing in
between**. That gap is what makes a duration cutoff a reliable
classifier.

The user wants a highlight reel. Doing it by hand across hundreds of
files is infeasible.

## Why not "let an AI watch the video"?

Vision over hours of footage is:

- **Expensive** — image tokens scale with video length; a 30-minute VOD
  sampled at 1 fps is thousands of images.
- **Slow** — minutes-to-hours of model time per video.
- **Hard to debug** — one opaque "find the good parts" call.

## The candidate-first answer

Instead of asking *"find interesting parts in this video"*, ask
*"generate 200 cheap candidate moments, then reduce to the best 20"*.

1. **Generate candidates deterministically and for free** from signals
   we already have: Outplayed's own clips, Riot's kill timeline, audio
   energy peaks. No LLM. Hundreds of candidates, each a tiny structured
   row.
2. **Rank with one small LLM call** that reads only the structured
   metadata (source, timestamps, event type, confidence) — never the
   video. Returns keep/reject + funny/hype/story scores + a reason.

This is ~100× cheaper than vision, fast, fully traceable in Langfuse,
and each source is independently testable.

## Scope discipline

Phase 2 is **analyzer-only**: it surfaces ranked suggestions. It does
*not* auto-cut clips yet — you review the ranking first. Auto-assembly
is a deliberate later step, gated on trusting the ranker's judgment.
