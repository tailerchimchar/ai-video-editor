# Spec: Videos hub, Jhin re-make, editor suggestions

Status: draft from the owner interview 2026-11-25, waiting on approval.
Builds on `spec-narration.md` (AVE-68) and the story requests file (AVE-64).

## Owner asks (verbatim)

- "it's a little hard to navigate between videos. can you make sure that the
  jhin game also works too and i can navigate between?"
- "can you also get like editor to put edits based on how the video should be?"

## Interview answers

| Question | Answer |
|---|---|
| Navigation | A **Videos** tab (list page) plus a video switcher dropdown on the narration page |
| Jhin | **Re-make Jhin first** with the Zeri v6 pipeline, then narrate |
| How editor effects get in | **Suggest, owner approves** (tick boxes, then re-render) |
| Order | **Edits first, then narrate** |
| Trigger | A **"Get editor suggestions" button** per video, on demand only |
| Style | **Light: 3-6 big moments** (story videos are not over-cut, per mentor) |
| Inputs | **Full panel**: gaming-expert → viewer-expert → editing-expert, mentor rules + recorded corrections |

## Part 1: Jhin re-make (one-off operation, no new code expected)

1. `riot-hud-sync` on the Jhin asset (`riot/42672a10-...`); write the offset to
   `meta.json`, keep the old one as `meta.pre-hud.json`.
2. Keep the current `story.mp4` as `story.v1.mp4` (and `story.json` →
   `story.v1.json`), re-plan and render with the v6 rules (outnumbered lead-in,
   `EVENT_POST_S`, cha-ching + death SFX on the HUD-synced times).
3. Check sound sync the same way as Zeri (every bell within 0.25 s of the KDA flip).
4. Drive: upload the new one, delete Jhin story v1/v2 there (local copies exist).

## Part 2: Videos hub (navigation)

**API** — `GET /api/v1/videos` returns every narratable video across all
registered narration target kinds (OCP: each `NarrationTarget` gains
`list_keys()`; the endpoint just loops the registry):

```
VideoSummary { kind, key, title, game, date, duration_s, video_url,
               take_count, stale, exported, suggestion_status }
```

`title` comes from the key (`2026-11-20_2001_jhin_8-2-13` → "Jhin 8/2/13").
`suggestion_status`: `none | pending | ready | applied`.

**Web**
- Top bar gets a **Videos** link → `/videos` (cards: title, game, date, length,
  takes, exported ✓, "editor suggestions ready" badge, **narrate** button).
- Narration page header gets a `VideoSwitcher` dropdown (same query, same
  data) to jump to another video without going back.

## Part 3: Editor suggestions

**Flow**: button → job `editor_suggest` → panel runs → suggestions file →
owner ticks the ones they want → **Apply & re-render** → the ticked ones are
written to `requests.json` as `effect` requests → story render job → done.
The narration page warns that takes go stale if a re-render happens after
recording (already built).

**Panel run: Jev, not Anthropic** (owner 2026-11-25: "wherever we can use
jev we should"). No Anthropic calls. The panel's three voices become typed
questions sent through the existing `decisions.ask_many` (Jev when
`TYPESAFE_API_KEY` is set, free rules otherwise, every answer on the
`jev-<date>.jsonl` audit log, capped by the existing `TYPESAFE_MAX_CALLS`):
1. **Candidates (pure code, no model).** From `story.json` + Riot fight data,
   one candidate per moment: each won fight, multikill, outnumbered win,
   kill/death, and the last fight of the game. Each candidate's state is a
   small dict (minute, kills/deaths, numbers like "4v5", gold swing,
   objective, is_last_fight). Opponents by champion only; **no mic
   transcripts** in any state.
2. **gaming-expert voice**: `ScoreQuestion` 1–5, "how good / game-deciding
   was this play" (the prompt quotes the gaming-expert's rubric).
3. **viewer-expert voice**: `BoolQuestion` "a League viewer would want this
   moment to pop".
4. **editing-expert voice**: `ChoiceQuestion` over `EffectChoice`
   {`none`, `letterbox_focus`, `slowmo`, `zoom`}, with one-line descriptions
   taken from the editing-expert arsenal.
   All three questions per candidate share one state, so Jev answers them in
   **one request per candidate** (≈10–20 per video, all batched by
   `ask_many`).
5. **Mentor (pure code)**: rank by score × viewer probability, keep the top
   3–6, at most one `letterbox_focus` (the last or biggest fight), no
   overlaps, inside chapters. Mentor rules and the recorded league
   corrections are pinned into the question prompts. Each suggestion's
   `reason` is built from the answers ("4v5 won after Urgot died · play 5/5
   (p .82) · pop (p .91)").
6. If Jev is unavailable or gives no opinion (probability 0), the free rules
   path still makes suggestions from the candidate facts (last fight →
   letterbox_focus, outnumbered win → slowmo, multikill → zoom), so the
   button always works.

**Storage**: `story/<round>/suggestions.json`
`{ created_at, status, items: [{id, kind, chapter_id, start_s, end_s, params,
reason, source: "panel", accepted: null|true|false}] }`.

**Swappable pieces (SRP/OCP)**: a `SuggestionEngine` protocol (the Jev
panel engine first; an LLM prose engine could be added later without edits),
candidate builders registered per game (League first, so Valorant/bowling add
a builder, not edits), a
`SuggestionStore` like `NarrationStore`, and the apply step reuses
`story/requests.py` (no second way to write effects).

**API** (prefix `/api/v1/story/{round}/suggestions`): `GET`, `POST` (start the
job), `PUT /{id}` (`{accepted}`), `POST /apply` (writes requests + starts
render job).

**Web**: a `SuggestionsPanel` on the narration page (above the takes) and a
badge on the Videos cards: each row shows kind, time, chapter, reason, a
checkbox and a preview button that seeks the player there.

**MCP tools**: `story_suggest_edits`, `story_list_suggestions`,
`story_accept_suggestion`, `story_apply_suggestions`.

## Not doing

- No auto-apply, no auto-run after renders.
- No Anthropic calls for suggestions (Jev → rules only).
- No new effect kinds (only the four the renderer has; a request for a new
  one is escalated, per the mentor).
- No Drive or Supabase upload of suggestions.
- Story-join of both games: later, after narration.

## Order of work

Jhin re-make (A) and Videos hub (B) are independent → editor suggestions (C,
backend) → suggestions UI (D, depends on B + C) → controller e2e on Jhin.
