# Final fix wave report — expert-panel branch

- Branch: `worktree-agent-a0222354f009f24f6` (fast-forwarded to master `a4d91e6` first)
- Worktree: `C:\Users\taile\source\repos\ai-video-editor\.claude\worktrees\agent-a0222354f009f24f6`
- Status: DONE_WITH_CONCERNS (small ones, listed at the end)

Test command used for every run, from `api/`, with dummy env vars
(`OUTPLAYED_MEDIA_DIR=C:\tmp\om`, `WORKSPACE_DIR=C:\tmp\ws`, no .env):
`uv run python -m pytest -q -p no:cacheprovider [paths]`.

Baseline on master: `884 passed, 1 skipped`.
Final full suite: `928 passed, 1 skipped in 15.88s`. `uv run ruff check src/`: `All checks passed!`

## Commits

| SHA | Subject |
|---|---|
| 6b5be41 | F1: one kill vocabulary in taxonomy; arc drop rule fires on real data |
| 290207d | F2: plan effects are dry-run in preflight; params models forbid extras |
| 2227718 | F3: plan effects apply as one atomic batch with one render |
| 2b79830 | F6: an ignored music trim is a warning in PlanResponse.problems |
| 30c3083 | F4: legacy analyzer sidecars are restamped on hit |
| 9d98bd5 | F5: skill docs describe effective_prompt_version and reserve_call |
| 5e65275 | Say pre-filter inactive under the rules backend; highlights indent |
| d1fe7b4 | Plan formats derive from taxonomy; arc_montage is a valid plan format |
| fbb0027 | Comments: BUCKET_ORDER excludes new topics; corrections_top_k group |

## F1 — mentor "1K/2K never on the drop" never fired

Changes:
- `taxonomy.py`: `EVENT_ALIASES` (doublekill/triplekill/quadrakill/pentakill/ace -> `multi_kill_2..5`), `canonical_event(event)`, `kills_for_event(event)` (lower-cases, folds aliases, 0 for non-kill events). The one place kill vocabulary lives.
- `intensity.py`: `_KILLS_PER_EVENT` / `_MULTI_KILL_TIER` and the stale `TODO(taxonomy)` removed; `score_clip` uses `kills_for_event` (multi_kill_5 now scores like ace; triplekill like multi_kill_3). `ClipWithIntensity.event_type: str | None = None` added (additive), filled by `score_session` from `entry["event"]`, fallback `event_type`.
- `music/arc.py`: `NOT_ON_THE_DROP` replaced by `MIN_DROP_KILLS = 3` and `_can_carry_drop`: a kill clip carries the drop only when `kills_for_event(clip.event_type) >= 3` (ace = 5).
- `bucketing.py`: hype = `kills_for_event >= 3` or `clutch`; voice_over = any 1K/2K kill event or the remaining non-kill set. Routing on all existing fixtures unchanged (test_bucketing, test_shorts_bucketing, test_taxonomy_response_shapes green); `triplekill` now routes hype and `doublekill` voice_over (both were `skipped` before).
- `decisions/backends/rules.py`: `_topic()` gives `Topic.multikill` for any 2K+ kill event; `_TOPIC_BY_EVENT` keeps only objective/clutch.
- `candidates/service.py`: `_VISIBLE_EVENTS` keeps only non-kill events; `_is_visible_event` adds every kill event via `kills_for_event` (now also triplekill and multi_kill_5).

Tests: `test_taxonomy.py` (alias + kill-count parametrized, routing), `test_intensity.py` (event_type carried; LoL/Val names score alike), `test_music_arc.py` (1K/2K tests switched from `topic` to `event_type`; doublekill added; end-to-end index.json -> `score_session` -> `plan_arc` puts the 3K on the drop while a screaming 1K has the highest intensity), `test_decisions.py` (doublekill/triplekill/multi_kill_5/pentakill topic), `test_candidate_label_promotion.py` (aliases visible).

RED: `27 failed, 94 passed` (ImportError for helpers, `DID NOT RAISE ValueError`, routing/topic/promotion asserts). GREEN: `180 passed in 1.71s` (F1 files plus bucketing/response-shape suites).

Note: clips with no kill event (teamfight, clutch, None) stay drop-eligible; the brief's ">= 3" is read as the rule for kill clips, otherwise existing teamfight/None pools could not plan at all.

## F2 — registry params pass validation, fail mid-run

Changes:
- `registry.py`: all params models inherit `_Params` with `extra="forbid"`. `CaptionParams.text: str = Field(min_length=1)` (required); `CaptionParams.aspect` removed (the compilation owns the aspect; zoom/focus params never had it). Caption description now says text is required in a plan, only standalone `caption_clip` captions from the transcript, and the plan's `at` is not applied by whole-clip effects.
- `edit_plan.py`: `PlanEffect.at` docstring: accepted for the panel's intent, not applied today.
- `routers/edits.py`: pure `effect_payload(body)` (raises ValueError); `_effect_from_body` wraps it into the 400.
- `routers/plan_executor.py`: `_effect_body` (rejects unknown keys, builds `AddEffectBody`) and `_dry_run`; `preflight` dry-runs every zoom/focus/caption effect and reports `effects[<ref>@<at>]: '<kind>': <reason>` (TypeError for `clip_ref`/`kind` collisions, ValidationError, missing text).
- `arsenal.md` regenerated (`uv run python -m ai_video_editor.registry --write`); byte test green.

Tests (`test_edit_plan.py`): `test_params_the_mutators_cannot_take_are_rejected` (caption no text, caption+aspect, zoom+clip_ref, zoom+kind) and `test_preflight_dry_runs_every_effect` (3 cases on the real executor against the compilation fixture). RED: `7 failed, 25 passed`. GREEN: `69 passed` (edit_plan + registry + music_arc).

## F3 — resume could apply an effect twice

Changes:
- `compile.py`: `add_effects(spec, [(clip_idx, effect), ...])` batch mutator, reusing `add_effect`.
- `routers/plan_executor.py` `apply_effects`: resolves every ref and payload first, then ONE mutator, ONE `_do_edit` (one render, one journal entry `plan:effects` with all effects in `details`).
- `routers/edits.py` `_do_edit`: saves `spec.json` after `render_spec` returns, not before, so a render that raises leaves the spec untouched (otherwise the failed batch would still be on disk and a resume would add it again). Docstring updated. This applies to every edit endpoint; the full suite stays green.

Tests: `test_effects_step_is_atomic_when_its_render_fails` (fake render raises on the batch: zero effects in spec, progress stopped before `effects`, journal = compile + order, exactly one render attempt for the batch) and `test_effects_step_applies_every_effect_in_one_journal_entry` (both effects added, one `plan:effects` entry, one render with both clips dirty). RED: `2 failed, 32 passed` (`[1, 0] == [0, 0]`; `['plan:effect:zoom', 'plan:effect:caption'] != ['plan:effects']`). GREEN: full suite `920 passed, 1 skipped`.

## F4 — legacy analyzer sidecars accepted forever

Changes (`vlm/clip_analyzer.py`): `_load_cached` returns `(analysis, stamped_version)`; on a hit whose stamp is not the current `effective_prompt_version` (the legacy integer), the sidecar is re-saved with the current version and model, no VLM call.

Test (`test_vlm_clip_analyzer.py::test_legacy_sidecar_is_restamped_then_expires_with_a_new_correction`): legacy hit -> file carries the effective version, still the cached analysis, 0 calls; append a correction -> miss, 1 call, fresh analysis. RED: `assert 7 == '3f3a85700594'`. GREEN: `11 passed`.

## F5 — stale skill docs

- `.claude/skills/ai-expert/SKILL.md`: cache-key contract now cites `corrections.effective_prompt_version` (RUBRIC_VERSION + hint file bytes + top-K rendered corrections, `CORRECTIONS_TOP_K` default 12); the spend-guard section now describes `anthropic_surface.reserve_call(feature, cap, env_name)` with the rank and decisions call sites; the "new LLM call" checklist and anti-pattern lines say to use a new `reserve_call` feature; one paragraph on regenerating `api/tests/fixtures/taxonomy/vlm_schemas.json` (command verified to reproduce the committed file byte for byte).
- `.claude/skills/editing-expert/engineering.md:93` and `.claude/skills/mentor/SKILL.md:137`: same `effective_prompt_version` wording.
- Doc-only, no behavioural test. `test_skill_budgets.py`: green (mentor 143 lines). Grep for `game_hints_effective_version|_rank_call_count` in `.claude/`: no matches.

## F6 — arc planner's music trim silently not rendered

Changes: `EditsPlanExecutor.warnings(plan)` returns `music: trim (start=…, end=…, trim_to_peak=…) not applied yet; the full song plays from 0 s` when any trim field is set; `routers/plan.py` returns it in `PlanResponse.problems` (field docstring: non-fatal). The `logger.info` line and the unused logger are gone. Executor module docstring and the arsenal `music` row (registry description) state it. The plan is not returned by the endpoint, so there is no `rejected[]` to extend. Music offset not implemented (follow-up).

Tests: `test_ignored_music_trim_is_reported_as_a_problem`, `test_plan_without_a_music_trim_reports_no_problems`. RED: `ValueError: not enough values to unpack (expected 1, got 0)`. GREEN: `73 passed`.

## One-liners

- `highlights.py:309` closing-brace indent: `ruff format` on the file.
- `shorts.py` `BUCKET_ORDER`: comment that new Topic members are left out until consumers migrate.
- `config.py`: `corrections_top_k` in its own "Corrections memory" group, noting 10 -> 12 (it replaced `prompts._CORRECTIONS_LIMIT = 10`, verified in f76c35a).
- Pre-filter wording: `bucketing.prefilter_summary(n_dropped, backend)`; `compute_candidates` diagnostics gain `prefilter_backend` (`decisions.select_backend()`); the candidates job summary always says `(pre-filter inactive (rules backend keeps everything))` under rules, `(<n> pre-filtered out)` otherwise; `index.json` gains `prefilter` and `index.md` a `- Pre-filter: …` line. Tests in `test_decisions.py` (3), RED `3 failed`, GREEN in full suite `926 passed, 1 skipped`.
- `arc_montage`: `_FORMATS_NOT_RENDERABLE` removed; the arsenal says every format is a valid plan and the executor refuses `cards` and `sfx`.
- `taxonomy.PLAN_FORMAT_BY_FORMAT` added; `registry.FORMATS = tuple(PLAN_FORMAT_BY_FORMAT.values())` (same order, same values). The only `TODO(taxonomy)` left in src was in intensity.py, removed in F1. Tests in `test_registry.py` (2), RED `2 failed, 17 passed`, GREEN full suite `928 passed, 1 skipped`.

## Concerns / not done

1. `shorts.py` was not `ruff format`ted: the file has ~150 hunks of pre-existing formatting debt and the change is a 2-line comment, so formatting would bury it. `compile.py`, `routers/edits.py`, `bucketing.py`, `candidates/service.py`, `decisions/backends/rules.py`, `highlights.py` were formatted because they were touched, which adds some unrelated reflow hunks to those diffs.
2. `.claude/skills/ai-expert/SKILL.md` is 534 lines (538 on master). It is not in `test_skill_budgets.py` (only gaming/viewer/editing-expert and mentor are), so I did not cut 130+ lines of it in a fix wave.
3. `_do_edit` now saves the spec after the render for every edit endpoint, not only plans. A render that raises no longer leaves a mutated `spec.json` with a stale output. I think this is strictly better, but it is wider than the brief's file list.
4. `prefilter_backend` is the configured backend (`select_backend()`); if an LLM backend fails and falls back to rules mid-run, the note still names the configured backend.
5. Commit trailer follows global-constraints.md (`Claude Fable 5.1`); the session harness suggested `Claude Opus 5.5`. Amend if the harness line was the intended one.
6. Not touched (another agent owns them): `overwolf_log.py`, `candidates/overwolf_events.py`, `killfeed.py`, `routers/clips.py`.
