# Story editor suggestions

"Get editor suggestions" on a rendered story round asks the content panel
which 3–6 moments deserve an effect, then turns the ones the owner ticks into
`effect` requests and re-renders. **No Anthropic call**: the panel's voices are
typed questions answered by Jev (`TYPESAFE_API_KEY`), or by the free rules
backend when Jev is not configured or fails. Spec: `docs/specs/spec-video-hub.md`
Part 3 (AVE-77).

## Flow

```
story.json + cached Riot timeline (+ WORKSPACE/plays/<asset>.json)
   │  candidate builder for the game (pure code)
   ▼
Candidates: one per owner-involved fight inside a chapter
   │  3 questions each, one shared state  ──>  ONE decisions.ask_many call
   ▼                                           (Jev: one request per candidate)
Verdict per candidate (no opinion → facts-based defaults)
   │  rank (pure, the mentor's cut)
   ▼
story/<round>/suggestions.json  ──>  owner ticks  ──>  apply  ──>  requests.json
                                                              └─> story_render job
```

## Pieces (`src/ai_video_editor/story/suggest/`)

| Module | Job |
|---|---|
| `models.py` | `EffectChoice`, `Candidate`, `SuggestionItem`, `SuggestionSet` (pinned contract; times are game-clock seconds) |
| `candidates.py` | `CandidateBuilder` protocol + per-game registry (`register_candidate_builder`, `UnknownGameError`) |
| `league_candidates.py` | League builder: facts `minute, kills, deaths, numbers, outnumbered_win, multikill, objective, is_last_fight, enemy_champions` |
| `questions.py` | The three prompts (rubrics quoted from the panel skills + mentor rules) and `questions_for` |
| `defaults.py` | Rules fallback: last fight → letterbox_focus (score 5), outnumbered win → slowmo (4), multikill ≥3 → 4, multikill 2 → zoom (3), else none (2) |
| `verdict.py` | Reads the 3 answers; a `rules` answer or probability 0 is **no opinion** |
| `params.py` | Default spans/params: letterbox `{bar: 0.12}` + focus `{}`; slowmo `{factor: 0.5}` over ≤2.5 s around the owner's kill; zoom `{factor: 1.3, roi: "center"}` |
| `rank.py` | score × pop probability (0.5 without opinion), drop `none`, one letterbox_focus, no overlaps, inside the chapter, max 6; builds `reason` |
| `engine.py` | `SuggestionEngine` protocol; `JevPanelEngine` |
| `store.py` | `SuggestionStore` protocol; `FileSuggestionStore` (`suggestions.json`, atomic writes) |
| `apply.py` | Accepted items → `story.requests.Effect` (letterbox_focus = letterbox + focus), duplicates skipped, status `applied` |
| `service.py` | Loads `story.json` + timeline, runs the engine, saves the set (used by the job) |

Questions (all about one candidate, state = the candidate's `facts`):

- gaming-expert: `ScoreQuestion(1..5)` "how good / game-deciding was this play";
- viewer-expert: `BoolQuestion` "a League viewer would want this to pop";
- editing-expert: `ChoiceQuestion(EffectChoice)` with one-line descriptions.

States hold champion names and numbers only: never mic transcripts or
summoner names.

## API (`routers/story.py`)

| Call | Does |
|---|---|
| `GET /api/v1/story/{round}/suggestions` | The `SuggestionSet` (404 before the first run) |
| `POST /api/v1/story/{round}/suggestions` | `{job_id}` of an `editor_suggest` job; writes `status: pending` first. 409 without a rendered `story.json`. Failure → `status: failed` + `error` |
| `PUT /api/v1/story/{round}/suggestions/{item_id}` | Body `{accepted: true\|false\|null}` → the set |
| `POST /api/v1/story/{round}/suggestions/apply` | Writes accepted items to `requests.json`, then `{job_id}` of a `story_render` job (asset from `story.json`, plays path). 404 without suggestions, 409 without a story asset |

MCP: `story_suggest_edits`, `story_list_suggestions`, `story_accept_suggestion`,
`story_apply_suggestions`.

## Extending

- **New game** (Valorant, bowling, IRL): add `story/suggest/<game>_candidates.py`
  with a builder (`build`, `chapter_spans`), register it, and add it to
  `candidates._BUILT_IN`. Questions, rank, store and apply stay as they are.
  `service.run_suggestions(game=...)` defaults to `"league"` today.
- **New engine** (e.g. an LLM prose engine): implement `SuggestionEngine.suggest`
  and pass it to `run_suggestions(engine=...)`.
- **New effect kind**: not here. The renderer owns effect kinds; ask the mentor.

## Cost

Jev: one request per candidate (≈10–20 per video), counted against
`TYPESAFE_MAX_CALLS`, every answer on the `jev-<date>.jsonl` audit log.
Rules: $0.
