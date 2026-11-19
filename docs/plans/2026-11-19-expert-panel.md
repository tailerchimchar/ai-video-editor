# Plan: Expert panel build-out (Jira AVE-1..AVE-12)

Spec: `docs/specs/spec-expert-panel.md` (authority). Jira: project AVE. Each task below mirrors one AVE Task verbatim; the Jira key is in the heading.

## Global Constraints

Binding for every task. Copy into every brief and reviewer prompt.

**Repo and commands**
- Repo `C:\Users\taile\source\repos\ai-video-editor`. Backend `api/src/ai_video_editor/`, tests `api/tests/`. Windows.
- Test: `cd api && uv run python -m pytest -q` (never `uv run pytest`, it fails on Windows). Lint: `cd api && uv run ruff check src/ && uv run ruff format --check src/`.
- Read `api/CLAUDE.md` conventions first: SRP per module, open/closed registries for sources and strategies, pure cores separated from I/O shells, fail-safe optional enrichment, source files immutable, no `shell=True`, UUID4 ids, ISO timestamps. Never import from `mcp` in the backend.
- Commit on your branch with clear messages; end commit messages with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

**Code principles (Noodlz, 2026-11-19)**
- Readable: no mumbo jumbo. Single Responsibility, Open/Closed, Dependency Injection (pass collaborators in; no hidden globals beyond `settings`). Clean, no smells, no dead code, no commented-out code.
- Extendable: adding a capability (a source, a backend, an effect, a strategy) must mean adding one or two new files and one registry entry, never editing five existing files. Prefer registries/dicts of callables over if/elif chains.
- Scalable: structure must house future features without rework; keep modules small and focused.
- Cost-happy: cheap deterministic paths first; LLM/API calls last, bounded, cached, and traced. Never add an LLM call where a rule or cached signal suffices.
- Maintainable: same bar as readable. Docstrings say what and why, not how.
- Process: superpowers `test-driven-development`: write the failing test first, make it pass, refactor. Report RED and GREEN evidence.
- Barebones for parts the user must test later (Jev API, posting, anything needing his key): a clean seam and a fake, not a polished integration.

**Contracts (pinned names, must match verbatim across tasks)**
- `taxonomy.py`: `Format` {hype, story, voice_over, funny, arc_montage}; `Topic` {first_blood, outplay, multikill, teamfight, objective_steal, kite_escape, tower_dive, teamfight_turn}; `Phase` {laning, mid, late}; `Game`; `Role`; `PayoffType`; `Intensity`; `LEGACY_ALIASES: dict[str, Format | Topic | Phase]`.
- `decisions.py`: `TypedQuestion` (`ChoiceQuestion`, `BoolQuestion`, `ScoreQuestion`), `Answer(value, probability, backend)`, `ask(question, state) -> Answer`, `select_backend()`.
- `registry.py`: `EffectKind`, `EffectSpec(kind, params_model, scope, mcp_tool, description)`, `REGISTRY`, `EFFECT_TOOLS`, `NON_EFFECT_TOOLS`, `NOT_RENDERABLE`, `render_arsenal_md()`.
- `edit_plan.py`: `EditPlan` (fields exactly as in `.claude/skills/editing-expert/SKILL.md`), `validate_plan(plan) -> list[str]`, `PlanProgress`, `execute_plan(plan, compilation_id, executor)`, `PlanExecutor` Protocol.
- `intensity.py`: `IntensityWeights`, `score_clip(events, transcript_arousal, vlm_payoff, weights=None) -> float`, `ClipWithIntensity(clip_ref, start, end, intensity, topic)`, `score_session(index_json_path)`.
- `music/structure.py`: `SongStructure(rising, climax, falling, drop_s, source, analyzer_version)`, `detect_structure(path)`, `STRUCTURE_ANALYZER_VERSION`. `music/analysis.py`: `SongAnalysis(beats, structure, file_sha256)`, `load_or_analyze(path)`, `analysis_path(path)`, `offset_analysis(analysis, trim_start_s, trim_end_s)`. `music/library.py`: `Library` with `add`, `suggest`.
- `music/arc.py`: `plan_arc(clips, analysis, format, target_seconds) -> EditPlan`, `trim_to_peak(analysis, peak_at_s, target_seconds)`; `render_short(..., snap_to_beats: bool = False)`.
- `vlm/corrections.py`: `RUBRIC_VERSION`, `effective_prompt_version(game)`; settings `CORRECTIONS_TOP_K = 12`.
- MCP tool output shapes for `list_session_buckets`, `list_shorts_topics`, `compile_shorts` stay byte-compatible.

---

### Task 1: AVE-5 · B · taxonomy.py with Format/Topic/Phase enums, legacy alias map, both classifiers migrated with byte-compatible MCP outputs

**Story:** one taxonomy module defines every classification vocabulary; `bucketing.py` produces `Format`, `shorts.py:categorize_clips` produces `Topic` + `Phase`, the VLM schemas import `PayoffType` from it, and no MCP tool output changes.
**Environment / Contracts:** branch from `master`. Contracts (verbatim): module `api/src/ai_video_editor/taxonomy.py` exporting `Format` {hype, story, voice_over, funny, arc_montage}, `Topic` {first_blood, outplay, multikill, teamfight, objective_steal, kite_escape, tower_dive, teamfight_turn}, `Phase` {laning, mid, late}, `Game`, `Role`, `PayoffType` (today's values from `vlm/prompts.py:102` plus `first_blood`, `multi_kill_2`..`multi_kill_5`, `objective_steal`, `outplay`, `kite_escape`, `tower_dive`, `teamfight_turn`), `Intensity` (float 0-1 with named bands low/mid/high/peak), `LEGACY_ALIASES: dict[str, Format | Topic | Phase]` mapping every slug in `bucketing.BUCKETS` and `shorts.BUCKET_ORDER` (laning_phase→Phase.laning, mid_game→Phase.mid, late_game→Phase.late, the rest→Topic) to exactly one value.
**Context pack:** Load skill `ai-expert` (prompt-version rules) · Read: `api/src/ai_video_editor/bucketing.py` (BUCKETS line 69, _HYPE_EVENT_TYPES 38-66), `shorts.py:107-116` (BUCKET_ORDER), `shorts.py:277-283` (_phase_bucket), `shorts.py:313` (categorize_clips), `shorts.py:513,1629` (consumers), `routers/shorts.py:171,730-768`, `vlm/prompts.py:102` (PayoffType), `vlm/watch_montage.py:60-84` (FlagCategory) · Verify: `api/tests/test_bucketing.py`, `mcp/tests/test_server.py:107` (list_session_buckets shape), any existing shorts topic tests.
**Success criteria:**
- `taxonomy.py` exists with the exported names above; `from ai_video_editor.taxonomy import Format, Topic, Phase, PayoffType, LEGACY_ALIASES` works.
- Test: every slug in `bucketing.BUCKETS` and `shorts.BUCKET_ORDER` is a key in `LEGACY_ALIASES` and maps to exactly one enum member.
- `bucketing.py` imports `Format` and `_HYPE_EVENT_TYPES`-style sets use `PayoffType` members, with identical routing results on the existing test fixtures.
- `categorize_clips` returns the same slug strings as today (consumers unchanged) but is implemented via `Topic` + `Phase` + `LEGACY_ALIASES` reverse map; a response-shape test pins the JSON of `/highlights/{game}/{session}/buckets` and `/assets/{id}/shorts/topics` against a recorded fixture.
- `vlm/prompts.py` and `vlm/watch_montage.py` import their literals from `taxonomy` (no duplicated value lists); the VLM prompt text is unchanged (test compares rendered system prompt before/after for the Valorant game hint).
- Full suite green; ruff clean.
**Not doing:** no decision adapter (Task 5); no corrections changes (Task 6); no renaming of output folders or MCP tool parameters; no removal of `BUCKET_ORDER` (keep it as the public list, derived from the enums).
**Steps:** TDD per step: alias-map test → taxonomy module → bucketing.py import + routing test → categorize_clips rewrite behind response-shape test → VLM literal imports + prompt-text test → ruff.

### Task 2: AVE-8 · C · registry.py as the single source of truth for effects, arsenal.md generator, two-way effect-tool coverage test

**Story:** every renderable effect, sound op, card op and structural op is a typed registry entry naming the MCP tool that executes it; `.claude/skills/editing-expert/arsenal.md` is generated from the registry and a test fails when the two differ or when an effect tool has no entry.
**Environment / Contracts:** branch from `master` (independent of Task 1; if `taxonomy.Format` is not merged, define `FORMATS` locally with the five values hype_montage/story_short/vo_short/funny_short/arc_montage and mark `# TODO(taxonomy)`). Contracts (verbatim): module `api/src/ai_video_editor/registry.py` exporting `EffectKind` (Literal of today's kinds: `zoom`, `focus`, `caption`, `caption_video`, `beat_flash`, `sfx`, `music`, `card_text`, `card_logo`, plus `NOT_RENDERABLE = ("slow_mo","speed_ramp","transition","pulse_zoom","beat_snap")`), `EffectSpec(kind, params_model: type[BaseModel], scope: Literal["clip","compilation","short"], mcp_tool: str, description: str)`, `REGISTRY: dict[EffectKind, EffectSpec]`, `EFFECT_TOOLS: frozenset[str]` (the MCP tool names that mutate media), `NON_EFFECT_TOOLS: frozenset[str]` (allowlist), `render_arsenal_md() -> str`, and a CLI entry `python -m ai_video_editor.registry --write` that rewrites `arsenal.md`.
**Context pack:** Load skills `editing-expert` (read `engineering.md` sections "The three primitives", "effects[] array", "SFX"), `mcp-expert` · Read: `api/src/ai_video_editor/edits.py:548,576,603,641` (apply_zoom/caption/focus/sfx signatures and param dicts), `edits.py:23` (_ROI_PRESETS), `compile.py:434` (_mix_music), `intros/render.py`, `music/effects.py:88`, `mcp/src/ai_video_editor_mcp/server.py` (tool names + parameters; read only, never import), `.claude/skills/editing-expert/arsenal.md` (current hand-written version, the target content) · Verify: `api/tests/test_sfx_overlay.py`, `test_caption_video.py`, `mcp/tests/test_server.py` for tool names.
**Success criteria:**
- `render_arsenal_md()` output equals the committed `arsenal.md` byte-for-byte (test), and the generated file carries a `GENERATED by registry.py` header replacing `GENERATED-PENDING`.
- Coverage test both ways: every name in `EFFECT_TOOLS` has a registry entry whose `mcp_tool` matches; every registry entry's `params_model` field names are a subset of that MCP tool's parameter names (parsed from the sibling `server.py` by AST, no import); every `@mcp.tool` in server.py is in `EFFECT_TOOLS` or `NON_EFFECT_TOOLS` (a new tool with neither fails).
- `NOT_RENDERABLE` kinds are listed in arsenal.md under "not renderable yet" and `REGISTRY` does not contain them.
- Full suite green; ruff clean.
**Not doing:** no edit-plan model or validator (Task 7); no new effects; no changes to edits.py behaviour; no DB table.
**Steps:** TDD per step: arsenal diff test (red against the hand-written file) → EffectSpec models + REGISTRY for the existing kinds → renderer until the diff test is green (adjust arsenal.md formatting to the generator, content preserved) → AST-based coverage test + allowlists → CLI `--write` → ruff.

### Task 3: AVE-10 · G · music/structure.py (rising/climax/falling + drop), content-hash sidecar holding beats + structure, beats.json migration, library.json

**Story:** a song's structure is detected once, cached by the bytes of the file and the analyzer version together with its beats, manual corrections survive re-detection, trimmed copies reuse the untrimmed analysis by offset, and the song library knows every cached song.
**Environment / Contracts:** branch from `master` (independent). librosa is already a dependency (`music/beats.py`). Contracts (verbatim): module `api/src/ai_video_editor/music/structure.py` exporting `SongStructure(rising: tuple[float,float], climax: tuple[float,float], falling: tuple[float,float], drop_s: float, source: Literal["auto","manual"], analyzer_version: str)`, `detect_structure(path: Path) -> SongStructure`, `STRUCTURE_ANALYZER_VERSION: str`; module `music/analysis.py` exporting `SongAnalysis(beats: BeatTrack, structure: SongStructure, file_sha256: str)`, `load_or_analyze(path: Path) -> SongAnalysis`, `analysis_path(path) -> Path` = `<music cache dir>/<sha256-of-file-bytes>.<STRUCTURE_ANALYZER_VERSION>.analysis.json`, `offset_analysis(analysis, trim_start_s: float, trim_end_s: float) -> SongAnalysis`; `music/library.py` exporting `Library` over `WORKSPACE/media_library/music/library.json` rows `{song_id, title, source_url, duration_s, analysis_path, tags[]}` with `add(path, title, source_url, tags)`, `suggest(tags, duration_s) -> list[row]`.
**Context pack:** Load skill `editing-expert` (`engineering.md` "music + beats + flash") · Read: `api/src/ai_video_editor/music/beats.py` (BeatTrack line 28, detect_beats 52, sidecar 77-154), `music/download.py:109` (cache dir + URL hash), `music/sync.py:177-191`, `shorts.py:773-805` (the only production caller of detect_beats), `candidates/audio.py:69` (energy_curve, reuse the decode approach) · Verify: existing `tests/test_beats*.py` if present; add a 20s synthetic WAV fixture generated in-test (numpy: quiet → loud → quiet) so no binary fixture is committed.
**Success criteria:**
- `detect_structure` on the synthetic WAV returns rising/climax/falling spans that tile [0, duration] in order and `drop_s` inside `climax`.
- `load_or_analyze` writes one sidecar per content hash; rewriting the file with different bytes produces a new sidecar; the old `<name>.<detector>.beats.json` is migrated into the new sidecar once and then ignored (test).
- A sidecar with `structure.source == "manual"` is not overwritten by `load_or_analyze` even when `STRUCTURE_ANALYZER_VERSION` changes (test).
- `offset_analysis` shifts every beat and span by `-trim_start_s` and drops those outside the trim (test); no librosa call for trimmed files.
- `shorts.py` beat flash reads beats through `load_or_analyze` and its output is byte-identical to before on the existing test (regression test).
- `Library.suggest` ranks by tag overlap then duration closeness (test).
- Full suite green; ruff clean.
**Not doing:** no intensity (Task 4); no arc planner or beat-snap wiring (Task 8); no download changes; no UI.
**Steps:** TDD per step: synthetic WAV fixture + structure test (red) → detect_structure (RMS envelope + onset strength, smoothed; climax = longest high-energy plateau around the global peak) → analysis sidecar + migration test → manual-override test → offset test → shorts.py regression → library tests → ruff.

### Task 4: AVE-11 · F · intensity.py pure scorer (events, multi-kill tier, VLM payoff, voice arousal) with configurable weights

**Story:** every clip gets a 0-1 intensity from signals the pipeline already has, as a pure tested function, so the arc planner and the panel brief can rank clips without an LLM call.
**Environment / Contracts:** branch from `master` (independent; if Task 1 is merged use `taxonomy.PayoffType` names, else raw `event_type` strings with a `# TODO(taxonomy)`). Contracts (verbatim): module `api/src/ai_video_editor/intensity.py` exporting `IntensityWeights(kills: float, multi_kill_bonus: dict[int,float], vlm_payoff: float, voice: float, death_penalty: float)` (pydantic, defaults from `config.py` as `INTENSITY_*` settings), `score_clip(events: list[dict], transcript_arousal: float | None, vlm_payoff: float | None, weights: IntensityWeights | None = None) -> float`, `ClipWithIntensity(clip_ref: str, start: float, end: float, intensity: float, topic: str | None)`, and `score_session(index_json_path: Path) -> list[ClipWithIntensity]`.
**Context pack:** Load skill `ai-expert` · Read: `api/src/ai_video_editor/candidates/overwolf_events.py` (event dict shape, multi-kill clustering), `bucketing.py:111-160` (how index.json clips carry event_type / hype_score / funny_score), `candidates/transcript.py` (VADER arousal per segment), `vlm/clip_analyzer.py:114` (ClipAnalysis payoff fields in the sidecar) · Verify: `api/tests/test_bucketing.py` fixtures (reuse the index.json shape), `test_overwolf_multikill.py`.
**Success criteria:**
- `score_clip` is monotonic in kill count and multi-kill tier, clamps to [0,1], returns 0 for no signals, and a death without a kill scores below a lone kill (property tests).
- `score_session` reads an index.json fixture and returns one `ClipWithIntensity` per clip with the highest-intensity clip being the ace/pentakill fixture.
- Missing VLM sidecar or transcript yields a score from the remaining signals, never an exception.
- Weights are overridable from settings and from the function argument (test both).
- Full suite green; ruff clean.
**Not doing:** no Jev/LLM calls; no arc placement (Task 8); no changes to ranker scores.
**Steps:** TDD per step: monotonic/clamp property tests → score_clip → session fixture test → score_session → settings test → ruff.

### Task 5: AVE-6 · D · decisions.py adapter (Haiku structured output now, Jev when TYPESAFE_API_KEY) answering Format/Topic classification and the include/exclude pre-filter

**Story:** `ask(question, state)` returns a typed answer with a probability from whichever backend is configured, and the pipeline uses it for Format/Topic classification and an include/exclude pre-filter before any agent or VLM call.
**Environment / Contracts:** branch from `master` AFTER Task 1 is merged (imports `taxonomy`). Contracts (verbatim): module `api/src/ai_video_editor/decisions.py` exporting `TypedQuestion` (pydantic; one of `ChoiceQuestion(prompt, options: type[Enum])`, `BoolQuestion(prompt)`, `ScoreQuestion(prompt, lo, hi)`), `Answer(value, probability: float, backend: Literal["jev","haiku","rules"])`, `ask(question: TypedQuestion, state: dict) -> Answer`, and `select_backend() -> str`. Settings in `config.py`: `TYPESAFE_API_KEY: str | None`, `DECISIONS_BACKEND: Literal["auto","jev","haiku","rules"] = "auto"`, `PREFILTER_MIN_PROBABILITY: float = 0.35`. Enum values come from `taxonomy.Format` / `taxonomy.Topic`. Backends are a registry (`decisions/backends/<name>.py`, one file per backend, discovered by a dict), so adding a backend is one new file.
**Context pack:** Load skills `ai-expert` (single Anthropic surface rules, spend guard, Langfuse `@observe`) and `langfuse` · Read: `api/src/ai_video_editor/ranker.py` (how the existing `messages.parse` call, spend guard and tracing are done; reuse the client factory), `config.py`, `bucketing.py` (after Task 1), `candidates/service.py:83` (where the pre-filter hooks in), `tracing.py` · Verify: `api/tests/test_ranker.py` style mocks for Anthropic; no real network in tests.
**Success criteria:**
- `ask()` with `DECISIONS_BACKEND=rules` reproduces today's `bucketing.py` routing exactly (rules backend = the existing frozensets; this is the offline fallback).
- `ask()` with the Haiku backend builds a `messages.parse` call whose response schema is derived from the question (choice → Literal of enum values; bool; score → bounded int) and returns `Answer(backend="haiku")`; tested with a mocked client, including malformed-response retry once then rules fallback.
- Jev backend: thin client against the `typesafe-sdk` shape (`choice` / `score` / `bool` with probabilities); selected only when `TYPESAFE_API_KEY` is set and `DECISIONS_BACKEND` is `auto` or `jev`; tested with a fake transport, no network. Barebones: a clean seam and a fake, since the user has no key yet.
- Both LLM backends are counted by the existing spend guard and traced in Langfuse with `backend` as metadata.
- Pre-filter: a new pure function `prefilter_candidates(candidates, ask=ask) -> (kept, dropped)` in `bucketing.py`; dropped candidates stay in `index.json` with `prefilter_probability`; `list_session_buckets` output shape unchanged (adds an optional `dropped` count only).
- Full suite green; ruff clean.
**Not doing:** no intensity scoring or viewer pre-screen via Jev; no changes to the VLM critic; no frontend.
**Steps:** TDD per step: question/answer models + rules backend test → Haiku backend with mocked client → Jev backend with fake transport → backend selection + settings → prefilter function + service hook → tracing/spend-guard test → ruff.

### Task 6: AVE-7 · I · typed correction rows (concept/verdict/example), top-K + RUBRIC_VERSION cache key, league_examples.jsonl created on first use

**Story:** corrections carry typed fields, the VLM cache key no longer changes on every appended correction or on the typed migration, and LoL corrections have a file to land in.
**Environment / Contracts:** branch from `master` AFTER Task 1 (uses `taxonomy.PayoffType` for `verdict` where applicable). Contracts (verbatim): JSONL row gains optional `concept: str | None` (a gaming-expert dictionary term), `verdict: Literal["payoff","false_positive","style"] | None`, `example: str | None`; constant `RUBRIC_VERSION: str` in `vlm/corrections.py`; `CORRECTIONS_TOP_K: int = 12` in `config.py`; function `effective_prompt_version(game) -> str` = hash of (`RUBRIC_VERSION`, game hint file bytes, the top-K rendered correction rows only).
**Context pack:** Load skill `ai-expert` · Read: `api/src/ai_video_editor/vlm/corrections.py` (lines 1-20, 55-143), `vlm/clip_analyzer.py:56-90,140,187` (sidecar cache keying), `vlm/prompts.py:206-227` (game hint loading), `routers/mentor.py`, `mcp/src/ai_video_editor_mcp/server.py:1877` (mentor_record_correction docstring, read only) · Verify: `api/tests/test_corrections.py`, `api/tests/test_watch_montage.py` (prompt version assertions).
**Success criteria:**
- Appending a correction beyond the top-K window does NOT change `effective_prompt_version(game)`; appending one that enters the rendered top-K DOES (two tests).
- Migrating `valorant_examples.jsonl` rows to the typed shape (`concept: null`) leaves `effective_prompt_version("valorant")` unchanged (test records the version before migration in a fixture).
- `mentor_record_correction(game="league", ...)` creates `vlm/game_hints/league_examples.jsonl` if missing and the next LoL VLM prompt renders it.
- `mentor_recent_corrections` returns the typed fields.
- Full suite green; ruff clean.
**Not doing:** no new MCP tool parameters beyond the optional typed fields (the MCP server change, if any, is a separate sibling-repo PR: note it in the closing report); no changes to the critic rubric text.
**Steps:** TDD per step: top-K version test (red) → `effective_prompt_version` → migration test → typed row model + writer → league file creation test → ruff.

### Task 7: AVE-9 · E · edit_plan.py schema + validate_plan against the registry + snapshot/resume executor with plan_progress.json

**Story:** an edit plan from the editing-expert is parsed into a pydantic model, rejected before any render if it names anything the registry does not know, and executed step by step with a snapshot first and a progress file so a failed run resumes or reverts.
**Environment / Contracts:** branch from `master` AFTER Task 2 (imports `registry`). Contracts (verbatim): module `api/src/ai_video_editor/edit_plan.py` exporting `EditPlan` with fields exactly as in `.claude/skills/editing-expert/SKILL.md` (`format`, `source_clips[]{clip_ref,start,end,intensity}`, `order[]`, `music{song_id,trim_to_peak}`, `effects[]{clip_ref,kind,at,params}`, `cards[]{after,text,seconds}`, `sfx[]{clip_ref,at,sound}`, `rejected[]{wanted,why}`), `validate_plan(plan: EditPlan) -> list[str]`, `PlanProgress(plan_hash, completed_steps: int, snapshot_version: str)` persisted as `plan_progress.json` in the compilation folder, and `execute_plan(plan, compilation_id, executor: PlanExecutor) -> PlanProgress` where `PlanExecutor` is a Protocol with one method per registry scope that the HTTP layer implements by calling the existing mutator functions in `routers/edits.py` (`_do_edit`).
**Context pack:** Load skills `editing-expert` (`engineering.md` "Journal + revert", "Iterative editing"), `compilation-expert` · Read: `api/src/ai_video_editor/registry.py` (Task 2), `routers/edits.py:579-604` (_do_edit), `compile_journal.py` (revert_steps / revert_to_version), `routers/shorts.py` (compile_shorts entry) · Verify: `api/tests/test_compile*.py`, journal/revert tests.
**Success criteria:**
- `validate_plan` returns messages for: unknown `kind`, kind in `NOT_RENDERABLE`, unknown `sound` (not in the game's sfx folder), unknown `format`, `params` failing the kind's `params_model`, `order` referencing a clip_ref not in `source_clips`; returns `[]` for a plan built only from registry kinds (tests for each).
- `execute_plan` first records the compilation's current journal version in `PlanProgress.snapshot_version`, then applies steps in order (source_clips → order → cards → effects → sfx → music), writing `plan_progress.json` after each step; a test with a fake executor that raises on step 3 leaves `completed_steps == 2`, and a second call resumes at step 3; `revert_to_version(snapshot_version)` restores the pre-plan spec (test).
- A new endpoint `POST /compilations/{id}/plan` accepts an `EditPlan`, validates, executes, returns `PlanProgress` + the validation messages; registered in `main.py`. Note in the report that the matching MCP tool (`apply_edit_plan`) is a sibling-repo PR.
- Full suite green; ruff clean.
**Not doing:** no new effects; no arc planner (Task 8); no frontend; no MCP server code in this repo.
**Steps:** TDD per step: EditPlan model round-trip test → validate_plan tests (red) → validator → PlanProgress + fake-executor resume test → execute_plan → snapshot/revert test → endpoint test → ruff.

### Task 8: AVE-12 · H · music/arc.py planner emitting an EditPlan, trim-to-peak, and snap_clips_to_beats wired into render_short behind a flag

**Story:** given scored clips and a song's structure, the planner places clips on the arc (best clip on the drop), trims the song so the drop lands on the cut's peak, emits a validated edit plan the panel can override, and `render_short` can snap cuts to beats.
**Environment / Contracts:** branch from `master` AFTER Tasks 7, 4, 3 are merged. Contracts (verbatim): module `api/src/ai_video_editor/music/arc.py` exporting `plan_arc(clips: list[ClipWithIntensity], analysis: SongAnalysis, format: Literal["story","arc_montage"], target_seconds: float) -> EditPlan` (uses `intensity.ClipWithIntensity`, `music.analysis.SongAnalysis`, `edit_plan.EditPlan` exactly as defined in Tasks 4, 3, 7), `trim_to_peak(analysis: SongAnalysis, peak_at_s: float, target_seconds: float) -> tuple[float, float]` (song trim start/end), and a `render_short(..., snap_to_beats: bool = False)` parameter in `shorts.py` that calls `music.sync.snap_clips_to_beats` with the `kill_on_beat` strategy.
**Context pack:** Load skills `editing-expert` (`engineering.md` "music + beats + flash", "9:16 layout"), `mentor` (hype montage rules: beat sync, biggest play on the drop, no 1K/2K in hype) · Read: `api/src/ai_video_editor/music/sync.py:177-191`, `shorts.py:821` (render_short), `shorts.py:1031` (build_shorts), `music/analysis.py` (Task 3), `intensity.py` (Task 4), `edit_plan.py` (Task 7) · Verify: `api/tests/test_watch_refine_loop.py` for how shorts tests stub ffmpeg; add a pure-function test module for arc.
**Success criteria:**
- `plan_arc` with `arc_montage`: the highest-intensity clip's window contains `analysis.structure.drop_s` after `trim_to_peak`; clips in `rising` are ordered by ascending intensity; `falling` holds the remainder; total duration within ±5% of `target_seconds` (tests on synthetic clips).
- `plan_arc` with `story`: clip order is chronological (by `start`) and only the song trim moves (test).
- The returned plan passes `validate_plan` with no messages (test) and sets `music.trim_to_peak = True` with the computed trim recorded in `music` (add optional `trim_start_s`/`trim_end_s` fields to `EditPlan.music`; additive only).
- `render_short(snap_to_beats=False)` output is byte-identical to today (regression); with `True`, each cut point moves to the nearest beat within the strategy tolerance (test with a fake beat grid, ffmpeg stubbed).
- `mentor` hard rule enforced in the planner: a clip whose topic is a 1K/2K is never placed on the drop in `arc_montage` (test).
- Full suite green; ruff clean.
**Not doing:** no new ffmpeg filters; no music ducking envelope (separate follow-up); no UI; no MCP tool (note `plan_arc_montage` as a sibling-repo follow-up in the report).
**Steps:** TDD per step: trim_to_peak test → placement tests (red) → plan_arc → validate_plan integration test → mentor-rule test → render_short regression + snap test → ruff.

### Task 9: AVE-13 blocker · overwolf_game_event emits zero League kills because the killfeed verifier matches the summoner name

**Story:** League of Legends recordings get their ground-truth kill candidates and kill anchors from the Overwolf log, verified against the champion name (League's killfeed shows champion names, not summoner names), so the Zeri game (9 kills in the log) yields 9 kill candidates instead of 0.
**Environment / Contracts:** branch from `master` (d3d8b1a or later). Test: `cd api && uv run python -m pytest -q`. Files: `api/src/ai_video_editor/overwolf_log.py` (`player_identity` currently parses only the `[GameService:Valorant] Session start info` line), `candidates/overwolf_events.py` (lines ~223-255: kill rows verified with `player_identity()["player_name"]`), `killfeed.py` (`verify_windows_have_kill(path, player_name, game, anchors)`), `routers/clips.py:list_kill_anchors` + `kill_anchors_for_video` (returns 0 anchors for this League file today). Contracts: `player_identity(log_dir=None, game: str | None = None) -> dict | None` returns for League `{"player_name": <summoner name>, "champion": <champion>, "game": "league"}` parsed from the newest `"summoner_info":{"name":...,"champion":...}` line in the Outplayed logs (the Valorant branch is unchanged); `overwolf_events` chooses the verification name per game: League -> champion name (if missing, keep the kill rows unverified with `metadata.killfeed_verified = False`, never drop); Valorant -> summoner name as today. `kill_anchors_for_video` must return League kill anchors (including `double_kill` clusters) from the dispatched-video events.
**Context pack:** memory of the format is in the log itself: `C:\Users\taile\Videos\ai_video_editor_output\harvest\2026-11-19_zeri\overwolf_log\background.html.5136.log` contains the Zeri game (session 2026-11-19 01:18-01:46, `summoner_info` line with `"champion":"Zeri"`, 9 `kill` events incl. two `double_kill`) — use a short redacted excerpt as a test fixture, never commit the full log. Read: `overwolf_log.py:433-470`, `overwolf_events.py:140-255`, `killfeed.py:276-360`, `routers/clips.py:230-260`.
**Success criteria:**
- Test: `player_identity(game="league")` on the fixture returns champion "Zeri" and player_name "Noodlz" (fixture may rename); Valorant identity test unchanged.
- Test: `detect_overwolf_events` (or the module's entry point) on a League dispatched-video fixture with the verifier stubbed to "no match for summoner, match for champion" keeps all 9 kill rows; with the verifier unavailable, kill rows are kept with `killfeed_verified: False`.
- Test: `kill_anchors_for_video` on the League fixture returns 9 kill anchors with two `double_kill` clusters.
- No change to Valorant behaviour (existing tests green). Full suite green; ruff clean.
**Not doing:** no OCR changes; no new candidate source; no changes to anchor offsets.
**Steps:** TDD per step: identity test (red) → parse summoner_info → overwolf_events per-game name selection test → implementation → anchors test → ruff.
