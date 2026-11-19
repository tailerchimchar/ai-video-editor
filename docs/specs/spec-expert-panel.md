# Spec: Expert Panel, Typed Decision Layer, and Music-Arc Videos

| | |
|---|---|
| Status | v2 (interview + red-team complete) |
| Date | 2026-11-19 |
| Baseline commit | `88b80a3` (July milestone committed; 700 tests passing) |
| Jira | project **AVE** (to be created in the Jira UI), work split via `jira-split` |
| Interview state | `.spec-expert-panel.interview.json` |
| Preview | https://claude.ai/artifact/6dY4co2bHypPHDskbsvizD (private) |

## 1. Problem

Noodlz returns to content after two months with three content shapes in mind: music-shaped story videos, all-action montages, and (mostly) shorts. The pipeline can detect kills, transcribe, bucket by rule, critique a montage with a VLM, and render shorts. What it cannot do:

- Tell whether a League play is actually good, or what the story of a game was. LoL knowledge is a 52-line HUD lexicon that lists trading as a false positive.
- Think like a viewer. The only viewer signal is three fields in the critique schema (opener, climax, pacing).
- Reason about editing craft. The editing-expert skill is 638 lines of ffmpeg and spec.json internals.
- Shape a cut to a song. Only BPM and a beat grid exist; beat snapping is implemented but never called; no energy or section detection.
- Classify clips consistently or cheaply. Two rule-based classifiers (editorial format vs topical event) disagree, the payoff vocabulary is Valorant-only, and every "is this worth it" judgement today costs a Haiku or VLM call.
- Stay honest about its own arsenal. The tool list went stale twice this year and I claimed built features were missing.

## 2. Goals

1. A four-voice panel (gaming-expert, viewer-expert, editing-expert, mentor) run once per video on the whole candidate list; single experts for follow-ups and engineering questions.
2. Each expert learns continuously from Noodlz without its always-loaded file growing past 400 lines, and every lesson is versioned.
3. A typed layer so panel output is executable, checkable, and cheap: taxonomy, decision adapter (Jev with Haiku fallback), effect registry, edit plan.
4. Music-arc videos: footage leads, music shapes; two formats (one-game story, many-game arc montage), full length or shorts.
5. Engineering workflow: interview or grill before building, jira-split into AVE, TDD via superpowers.
6. Tonight: a v0 panel thin enough to scaffold in about an hour and use on tonight's footage.

## 3. Non-goals

- No runtime hook for the experts: they do not run inside the pipeline or inject rubric text into VLM prompts (D1). Corrections reach the runtime through the existing corrections channel (D21), not through the expert files.
- No knowledge graph (D16). Typed taxonomy plus a decision adapter first; a graph only when a real query needs relationships.
- No database copy of the effect registry (D11).
- No Valorant work in gaming-expert v1 (D5). Valorant stays under mentor's rules.
- No new candidate sources or LoL data parsing (participantFrames, gold, CS) in this spec. Follow-up J, because "judge the play" will want it.
- No agent-to-agent debate. Claude Code subagents cannot message each other; the main session orchestrates (D26).

## 4. The panel

### 4.1 Voices

| Voice | Question it answers | Where it lives (all versioned in this repo, D20) |
|---|---|---|
| gaming-expert | Was that play good by LoL standards? What is the story of this game or lane? | `.claude/agents/gaming-expert.md` + `.claude/skills/gaming-expert/` |
| viewer-expert | Would a LoL player on TikTok keep watching? What should pop, sound, be titled, be the thumbnail? | `.claude/agents/viewer-expert.md` + `.claude/skills/viewer-expert/` |
| editing-expert | How do we cut it, with which of our tools, in what order? | `.claude/agents/editing-expert.md` + `.claude/skills/editing-expert/` (restructured) |
| mentor | Which bucket, which hard rules apply, who wins a tie? | `.claude/skills/mentor/` (moved from `~/.claude/skills/mentor`, global copy removed) |

### 4.2 Invocation (D2, D23, D26)

- **Per video, one batched panel round.** The main session builds one brief (candidate list with events, transcript excerpts, VLM sidecars where present, the format under consideration) and dispatches the three expert agents in parallel. Each returns its typed JSON (sections 5-7), never an essay. The main session then loads mentor, applies policy as chair, and reports agreements, disagreements, and a recommendation. Noodlz decides.
- **Follow-ups** (one clip, one title, one effect) go to a single expert.
- **Engineering questions** go to a single expert.
- **LoL ties** between mentor and gaming-expert are not auto-resolved for the first few videos: the session reports both and Noodlz breaks the tie (D28). Each tie broken is logged as a lesson for whichever voice was wrong.
- Agent bodies begin by reading their own `SKILL.md` and `dictionary.md`, then only the appendix entries the brief needs. No reliance on frontmatter preloading (D26).

### 4.3 Expert folder layout (D4, D10)

```
.claude/skills/<expert>/
  SKILL.md          persona, how it judges, output schema, index of appendix   (< 400 lines, enforced by test)
  dictionary.md     term -> plain definition -> what it looks like on screen / in the data
  appendix/         one deep-dive file per hard concept
  log.md            dated entries: what Noodlz taught or corrected, with the example that triggered it
```

editing-expert additionally has:

```
  arsenal.md        GENERATED from the effect registry (never hand-edited)
  engineering.md    the current 638 lines of ffmpeg / spec.json / journal internals, moved verbatim
  craft/            the appendix name for editing: pacing, transitions, sound design, slow-mo rules
```

Rules:
- `api/tests/test_skill_budgets.py` counts lines of every expert SKILL.md and fails above 400.
- When SKILL.md needs more detail, the detail moves to an appendix file and SKILL.md keeps a one-line pointer.
- `log.md` is append-only, newest last. Format: `- YYYY-MM-DD: <rule or fact>. Context: <clip, session, or paraphrase>. Promoted to: <dictionary | appendix/<file> | SKILL.md | none>`.
- Logs and dictionaries never quote another player's speech and never contain summoner names (D22).
- Web-research findings (viewer-expert, editing-expert) go into log.md with the source URL and date before they go anywhere else.

### 4.4 Teaching protocol (D21)

Noodlz teaches in conversation. The listening session (or the expert agent itself when corrected mid-panel) must, in the same turn:
1. Append a `log.md` entry.
2. Add or update the `dictionary.md` term if a new concept was named.
3. Update `SKILL.md` only if the lesson changes how the expert judges, not just what it knows.
4. **Dual write:** if the lesson is about what counts as a payoff or a false positive in footage, also call `mentor_record_correction` so the VLM critic's `<game>_examples.jsonl` gets it. The JSONL is the runtime truth; `log.md` is the teaching narrative. `league_examples.jsonl` is created on first use.

The same protocol applies when Noodlz overrules a panel recommendation.

## 5. gaming-expert (D3, D5)

- **Role**: a professional League of Legends analyst, ADC specialist. Judges plays by what a coach would say, not by kill count. Extracts the arc of a game or lane: lane state, the decision, the execution, the payoff, the cost.
- **Inputs**: clip refs, Overwolf/Riot events, transcript snippets, kill anchors, VLM analysis sidecars when present. Later: participantFrames (gold/CS per minute) once parsed (J).
- **Output schema** (per clip or game): `quality` (1-5 with a one-line coach reason), `concepts` (dictionary terms exhibited), `story_beats` (ordered, each with a timestamp range and a plain sentence), `talking_points` (facts for a VO, not scripted lines).
- **Scope**: LoL only, ADC first. SKILL.md is sectioned by role (`## ADC`, `## Support (empty)`, ...) so another role is a new section plus appendix files, not a refactor.
- **v1 knowledge**: Noodlz teaches; the dictionary is seeded with the ADC vocabulary (trading, wave management, crash, freeze, slow push, kiting, positioning, tempo) as entries with empty bodies that he fills by talking.

## 6. viewer-expert (D6, D7, D8)

- **Audience model**: a LoL player scrolling TikTok or Shorts. Knows the game, swipes in two seconds, rewards clarity of payoff and personality.
- **Judges**: hook by t=3s, swipe-risk points with timestamps, which beats deserve slow-mo, zoom, flash, SFX and which are overdone, title and caption and hashtags, thumbnail frame choice.
- **Sources**: short-form craft written into SKILL.md at creation (Noodlz red-pens), web research on demand (logged with URL and date), Noodlz's posted analytics pasted into log.md as `analytics:` entries that the expert mines for its own rules.
- **Output schema**: `hook_verdict` (pass/weak/fail + reason), `swipe_risks[]` (t, reason), `effects_wanted[]` (clip_ref, t, effect kind from the registry), `copy` (title, caption, hashtags), `thumbnail_t`.

## 7. editing-expert (D10, D11, D12, D13)

- **Role**: the editor. Knows editing craft (learned online and from Noodlz) and exactly what this pipeline can render.
- **Output is a typed edit plan**, not prose:

```json
{
  "format": "story_short | arc_montage | funny_short | vo_short | hype_montage",
  "source_clips": [{"clip_ref": "...", "start": 0.0, "end": 12.4, "intensity": 0.7}],
  "order": ["..."],
  "music": {"song_id": "...", "trim_to_peak": true},
  "effects": [{"clip_ref": "...", "kind": "slow_mo", "at": 8.1, "params": {"factor": 0.5}}],
  "cards": [{"after": null, "text": "first time in voice comms", "seconds": 2.5}],
  "sfx": [{"clip_ref": "...", "at": 8.3, "sound": "hit_marker"}]
}
```

- Every `kind`, `sound`, and `format` must exist in the registry or the plan is rejected before any render.
- **Execution (D30)**: the main session snapshots the compilation first (the journal and `revert_compilation` already exist), then executes the plan one entry at a time through existing MCP tools, recording the completed step index in the compilation folder (`plan_progress.json`). A failed run resumes from the last completed step or reverts to the snapshot.

### 7.1 Effect registry (D11, D30)

- Module: `api/src/ai_video_editor/registry.py`. Pydantic models: `EffectKind` (literal set), `EffectSpec` (kind, params schema, time-scoped or whole-clip, executing MCP tool name, one-line description).
- Consumers: `arsenal.md` generator (a test diffs generated vs committed), edit-plan validator, MCP coverage test.
- Coverage test covers **effect tools only** (an explicit allowlist excludes list/get/scan/delete tools) and checks in both directions: every effect tool has a registry entry, and every entry's params match the tool's signature.
- Not in a database. If SaaS needs it, export; the module stays the truth.

## 8. Typed decision layer

### 8.1 Taxonomy (D16, D27)

- Module: `api/src/ai_video_editor/taxonomy.py`. Three separate enums, because the two existing classifiers are different axes:
  - `Format`: the editorial bucket (hype, story, voice_over, funny, arc_montage).
  - `Topic`: the topical event (first_blood, outplay, multikill, teamfight, objective_steal, plus new LoL entries: kite_escape, tower_dive, teamfight_turn).
  - `Phase`: laning, mid, late (replaces the time-threshold slugs laning_phase/mid_game/late_game).
  - Also `Game`, `Role`, `PayoffType` (imported by the VLM schemas instead of their own literals), `Intensity` (0-1 float with named bands).
- `bucketing.py` produces `Format`; `shorts.py:categorize_clips` produces `Topic` + `Phase`. Both import the taxonomy. The MCP tools `list_session_buckets`, `list_shorts_topics`, `compile_shorts` keep their current output shapes; old slugs are aliases, and a test asserts every legacy slug maps to exactly one new value.
- Corrections (`<game>_examples.jsonl`) gain typed fields: `concept` (dictionary term), `verdict`, `example`. Existing rows migrate with `concept: null`.
- **Cache safety (D29)**: the VLM prompt version hashes only the top-K corrections actually rendered into the prompt, plus an explicit `RUBRIC_VERSION` constant. The typed migration does not bump the constant, so it does not re-bill the analyzed library.

### 8.2 Decision adapter (D31, D32, D33)

- Module: `api/src/ai_video_editor/decisions.py`. One function shape: `ask(question: TypedQuestion, state: dict) -> Answer` where `TypedQuestion` is a choice over a taxonomy enum, a yes/no, or a score, and `Answer` carries the value plus a probability.
- Backends: **Jev** (TypeSafe AI, `typesafe-sdk`) when `TYPESAFE_API_KEY` is set; **Claude Haiku structured output** with the identical schema otherwise. Backend selection is per call and logged to Langfuse like every other paid call. Noodlz is on the Jev waitlist, so the Haiku path is the one exercised first (D33).
- **First uses (D32)**:
  1. `Format` and `Topic` classification over event state, replacing the hand-written frozensets in both classifiers. The rule sets stay as the offline fallback when no backend is configured.
  2. Include/exclude pre-filter: "is this candidate worth the panel's time?" over events + transcript excerpt, run before any agent or VLM call. Anything below a configurable probability is dropped from the panel brief but kept in the index.
- Not in v1: intensity scoring and viewer pre-screen through Jev. They are candidates once the first two prove out.
- Jev sees text and structured state only, never frames. Visual judgements stay with the VLM.

## 9. Music-arc videos (D14, D15, D25)

### 9.1 Principle
Footage leads, music shapes. The cut's intensity curve is computed first. The song is chosen second (Noodlz picks; the library suggests when he does not care). The song is trimmed so its climax lands on the cut's peak, then cuts snap to beats.

### 9.2 Components
1. **Intensity score per clip** (`intensity.py`): from kill count, multi-kill tier, VLM payoff strength when a sidecar exists, voice reaction (transcript arousal), with weights in config. Pure function, tested.
2. **Song structure** (`music/structure.py`): librosa RMS/onset envelope and section boundaries produce `rising`, `climax`, `falling` spans plus the drop timestamp. Noodlz may correct spans in the sidecar; corrections survive re-detection because the sidecar records `source: manual`.
3. **Cache keying (D25)**: one sidecar per song, named by content hash and analyzer version, holding both beats and sections. The existing path-keyed `beats.json` is migrated. Trimmed songs are never re-analyzed: timings derive from the untrimmed analysis by offset.
4. **Arc planner** (`music/arc.py`): given clips with intensity and a song structure, produce an ordered placement: low-to-mid clips in rising, the best clip on the drop, aftermath in falling. Output is an edit plan (section 7) so the panel can override any placement.
5. **Wire `snap_clips_to_beats`** into `render_short` behind a flag. Cheapest step; unused since July.
6. **Formats**: `story` = one game, chronological, song trimmed to the game's shape. `arc_montage` = many games, music-first placement. Both at 45-90s (short) or 2-5 min.

### 9.3 Song library
`WORKSPACE/media_library/music/` already caches downloads by URL hash. Add `library.json` with per-song: title, source URL, duration, structure sidecar path, tags Noodlz gives it ("sad", "hype", "build"). Suggestion = tag match plus duration fit.

## 10. Privacy: other players' voices (D22)

Tonight's footage is solo-queue voice comms, so it contains strangers' voices.
- Teammate audio may stay in published cuts; it is the content.
- Summoner names are blurred or cropped in 9:16 layouts; captions never attribute a line to a named player.
- `log.md`, `dictionary.md`, and specs never quote teammate speech; paraphrase only.
- Transcripts stay in the local workspace and are never committed. `.gitignore` already excludes the workspace; a test asserts no `transcripts` artefact lands under `docs/` or `.claude/`.

## 11. Engineering workflow (D17)

1. Design: `grill-me` for a decision, `interview-me` for a feature. Output lands in `docs/specs/`.
2. Split: `jira-split` into project AVE. Epic per spec section group; Tasks follow the brief template; shared names are contracts.
3. Build: superpowers `test-driven-development`; `subagent-driven-development` when Tasks are independent.
4. Verify: controller checks the diff and runs tests before any Task closes.
5. Ship: commit on master (solo project); push is Noodlz's call.

## 12. Tonight: v0 panel (D18, D20)

Scaffold only, in this order, before the stream:
1. Remove `.claude/agents/` from `.gitignore`. Move `~/.claude/skills/mentor` into `.claude/skills/mentor` and delete the global copy.
2. Three agent files in `.claude/agents/` (name, description, model, body that reads its skill folder and returns the schema from sections 5-7).
3. Three skill folders with SKILL.md under 120 lines each, dictionary with seeded terms, empty appendix, log.md with one entry.
4. editing-expert: move the current SKILL.md to `engineering.md` untouched; new SKILL.md is the persona and points at it. `arsenal.md` is hand-written tonight from the MCP tool list, marked `GENERATED-PENDING` until the registry exists.
5. Mentor SKILL.md gains a `## Panel chair` section: how to break ties, LoL ties go to Noodlz, when to escalate.
6. Smoke test: one real decision on tonight's footage, panel output recorded in each expert's log.md as the first lesson.

After the stream: harvest Outplayed clips to the workspace immediately (retention purges), transcribe, bucket, run the panel on the funny and VO candidates, cut 2-4 shorts.

## 13. Risks

| Risk | Mitigation |
|---|---|
| Panel cost | One batched round per video; typed JSON outputs; SKILL.md cap enforced by test; measure tokens per round in week one. |
| Gaming-expert judges plays it cannot see (wave state, trades) from kill events alone | v1 relies on Noodlz's teaching plus VLM frames; follow-up J parses MATCH-V5 participantFrames. |
| Jev access is waitlisted | Haiku fallback with the identical schema is the first path exercised; Jev is a drop-in. |
| librosa section detection is noisy on some genres | Sidecar with manual override; the drop timestamp is the only hard requirement. |
| Taxonomy change breaks MCP tool outputs | Separate Format/Topic/Phase enums; old slugs as aliases; response-shape tests. |
| Mentor's rules are Valorant-learned | LoL ties go to Noodlz for the first few videos; each becomes a logged lesson. |
| Jira project AVE cannot be created through the Atlassian MCP | Noodlz creates it in the Jira UI (team-managed software project, key AVE). |

## Decisions Log

| ID | Topic | Decision | Rationale | Source | Date |
|---|---|---|---|---|---|
| D1 | Expert runtime | Engineering-time agents only, no pipeline hook | User wants advisors in sessions; rubric slice deferred | Interview | 2026-11-18 |
| D2 | Invocation | Full panel for content, solo expert for engineering | No judgment call about who to ask on content | Interview | 2026-11-18 |
| D3 | Gaming-expert job | Judge the play + extract the story; talking points, not scripts | Voice is the user's; phrasing is viewer-expert's | Interview | 2026-11-19 |
| D4 | Expert folder | SKILL.md < 400 lines + dictionary + appendix + log | Constant learning without bloating the always-loaded file | Interview | 2026-11-19 |
| D5 | Gaming-expert scope | LoL only, ADC first, role-extensible | Depth over breadth | Interview | 2026-11-19 |
| D6 | Viewer audience | LoL players on TikTok/Shorts | Jargon OK, slow setups not | Interview | 2026-11-19 |
| D7 | Viewer judges | Hook/retention, effects + sound, title/caption/hashtags, thumbnail | Owns what the viewer sees before and during | Interview | 2026-11-19 |
| D8 | Viewer sources | Built-in craft + web research + posted analytics | Analytics is the strongest signal | Interview | 2026-11-19 |
| D9 | Mentor | Stays as fourth voice and chair | Editorial policy keeps its seat | Interview | 2026-11-19 |
| D10 | Editing-expert shape | Same folder layout; internals move to engineering.md | 638-line file is engineering, not craft | Interview | 2026-11-19 |
| D11 | Effect registry | In-code pydantic module; arsenal.md generated; no DB | Code is the truth; DB copy drifts | Interview | 2026-11-19 |
| D12 | Edit handoff | Typed JSON edit plan, validated against registry | Executable and checkable panel output | Interview | 2026-11-19 |
| D13 | Editing research | Web research on demand, logged | Same as viewer-expert | Interview | 2026-11-19 |
| D14 | Music arcs | Footage leads, music shapes; auto-detect sections with manual sidecar override; user picks songs | Reconciles "music defines" with "video first" | Interview | 2026-11-19 |
| D15 | Clip placement | Intensity score + panel override | Automatic default, taste on top | Interview | 2026-11-19 |
| D16 | Classification | Typed taxonomy module now; graph deferred | Two classifiers disagree; corpus too small for a graph | Interview | 2026-11-19 |
| D17 | Jira | New project AVE | Keep MOSHI board clean | Interview | 2026-11-19 |
| D18 | Tonight | Scaffold v0 panel, use on tonight's footage | First real test with real stakes | Interview | 2026-11-19 |
| D19 | July work | One milestone commit `88b80a3` | Clean tree before new branches | Interview | 2026-11-19 |
| D20 | Versioning | Un-ignore `.claude/agents/`; move mentor into the repo; global copy removed | Panel files had no history or backup | Red Team | 2026-11-19 |
| D21 | Corrections | Dual write: log.md + `mentor_record_correction`; JSONL is runtime truth | Keeps D1 while the VLM critic still learns | Red Team | 2026-11-19 |
| D22 | Privacy | Teammate audio allowed; no names, no quotes in logs; transcripts never committed | Voice comms contain strangers' speech | Red Team | 2026-11-19 |
| D23 | Panel cost | One batched round per video; typed JSON outputs; 400-line test | "Every decision" would multiply cost | Red Team | 2026-11-19 |
| D24 | Taxonomy axes | Separate Format, Topic, Phase enums; legacy slugs as aliases; tool outputs unchanged | The classifiers are different axes, both behind live tools | Red Team | 2026-11-19 |
| D25 | Audio caches | One sidecar per song keyed by content hash + analyzer version; trimmed timings by offset | Path-keyed beat cache served stale grids | Red Team | 2026-11-19 |
| D26 | Agent wiring | Agent bodies read their own SKILL.md first; main session orchestrates and chairs | No evidence of frontmatter skill preloading; subagents cannot talk to each other | Red Team | 2026-11-19 |
| D27 | Taxonomy migration | Response-shape tests; alias map test | Avoid breaking `list_session_buckets` / `list_shorts_topics` | Red Team | 2026-11-19 |
| D28 | LoL ties | Noodlz breaks mentor vs gaming-expert ties for the first few videos; each is logged | Mentor's rules are Valorant-learned | Red Team | 2026-11-19 |
| D29 | Cache invalidation | Prompt version hashes only rendered top-K corrections + explicit RUBRIC_VERSION | Typed migration must not re-bill the library | Red Team | 2026-11-19 |
| D30 | Registry + plan safety | Effect-tools-only coverage test with params match; plan runs snapshot first and record step index | Half-applied cuts had no undo | Red Team | 2026-11-19 |
| D31 | Decision backend | Jev (TypeSafe AI) via a decision adapter with a Haiku structured-output fallback | Cheap typed decisions; no hard vendor dependency | Interview | 2026-11-19 |
| D32 | Jev first uses | Format + Topic classification; include/exclude pre-filter | Replaces hand rules; gates panel cost | Interview | 2026-11-19 |
| D33 | Jev access | On the waitlist; build against the SDK shape, Haiku path first | Access not confirmed | Interview | 2026-11-19 |

## Dependency Graph & Implementation Order

```
A  v0 panel scaffold: .gitignore fix, mentor move, agents, skill folders, chair section   tonight, no code
B  taxonomy.py (Format/Topic/Phase/PayoffType/...) + alias map + migrate both classifiers   independent of A
C  registry.py + arsenal.md generator + effect-tool coverage test                           independent of A, B
D  decisions.py adapter (Haiku backend first, Jev when key arrives) + Format/Topic/pre-filter questions   needs B
E  edit plan schema + validator + snapshot/resume executor                                  needs C (and B for Format names)
F  intensity.py                                                                             needs B
G  music/structure.py + hash-keyed sidecar + library.json + beats.json migration            independent
H  music/arc.py + wire snap_clips_to_beats                                                  needs E, F, G
I  corrections typed fields + top-K/RUBRIC_VERSION cache key + league_examples.jsonl        needs B
J  gaming-expert depth (dictionary fill, ADC appendix) + viewer-expert depth                needs A; continuous teaching
K  Riot participantFrames parsing (follow-up, not in this spec)                             after J shows the need
```

Order: A tonight. Then B, C, G in parallel (fan out). Then D, E, F, I. Then H. J runs as teaching sessions alongside everything.
