# Task 7 report (AVE-9): edit_plan, validator, snapshot/resume executor, POST /compilations/{id}/plan

Branch: worktree-agent-a898f0739b9523971. Worktree: C:\Users\taile\source\repos\ai-video-editor\.claude\worktrees\agent-a898f0739b9523971
(This report is inside the worktree because the isolation rule blocked the shared path.)

## Implemented
- `api/src/ai_video_editor/edit_plan.py`: `EditPlan` (+ SourceClip, PlanMusic with optional `trim_start_s`/`trim_end_s`, PlanEffect, PlanCard, PlanSfx, Rejected), `validate_plan(plan, known_sounds=None)`, `PlanProgress`, `PlanExecutor` Protocol, `execute_plan`, `load_progress`, `STEPS`.
  - Validation: unknown kind, NOT_RENDERABLE kind, unknown sound (defaults to stems under `WORKSPACE/media_library/*/sfx/`; injectable), unknown format, params failing `params_model`, `order` ref not in `source_clips`.
  - `execute_plan`: records `snapshot_version` (journal length) first, runs source_clips, order, cards, effects, sfx, music (6 fixed steps), writes `plan_progress.json` atomically after each step; resumes same plan hash at `completed_steps`; a different plan starts fresh.
- `routers/plan_executor.py`: `EditsPlanExecutor` via `_do_edit`. Supports reorder (order) and effects zoom/focus/caption, and verifies source_clips refs. Refs are resolved against the journal snapshot (positional refs would shift after reorder; found by the endpoint test).
- `routers/plan.py`: `POST /api/v1/compilations/{id}/plan`; 422 `{problems}` before any render, 409 on a step failure (re-POST resumes), 200 `{progress, problems: []}`. Registered in `main.py`.
- registry.py: resolved the FORMATS TODO (comment only; FORMATS is the single list, plan format values unchanged).

## Tests / TDD
- RED: `uv run python -m pytest -q tests/test_edit_plan.py` -> `ModuleNotFoundError: No module named 'ai_video_editor.edit_plan'` (collection error).
- GREEN: same command, 16 passed (unit), then 19 passed with endpoint tests. The endpoint test also caught a real bug (positional refs shifting after reorder), fixed.
- Resume test uses a fake executor raising on step 3: `completed_steps == 2`, second call runs cards..music. `revert_to_version(int(snapshot_version))` restores the pre-plan spec (test).
- Full suite: 771 passed, 1 skipped. `ruff check src/` clean; touched files formatted.

## Concerns
- Real executor cannot yet apply cards, sfx, music, or effect kinds other than zoom/focus/caption (no mutators exist); it raises `UnsupportedPlanStepError` (HTTP 409) rather than skipping silently. `source_clips` start/end retiming is not applied (only ref verification).
- A completed plan re-posted unchanged is a no-op (progress hash matches); revert does not clear `plan_progress.json`.
- `snapshot_version` of "0" (empty journal) would break `_clip_id`; normal compilations always have a journal entry.
- Matching MCP tool `apply_edit_plan` is a sibling-repo follow-up (mcp/ not touched).
- Two commits (13adbf4 + 4c5b73a ruff follow-up).

## Files
api/src/ai_video_editor/edit_plan.py, routers/plan.py, routers/plan_executor.py, main.py, registry.py (comment), api/tests/test_edit_plan.py

## Fix round (review: three Important + empty journal)
Merged master first (clean). Changes:
1. Pre-flight: `PlanExecutor.preflight(plan) -> list[str]`; `execute_plan` calls it before snapshot/mutation and raises `PlanNotExecutableError`; route returns 409 `{"detail": {"unsupported": [...]}}`. Cards, sfx, non-mutator effect kinds, unresolved music song, and an empty journal are all refused up front. Tests: cards plan -> 409, journal untouched, no plan_progress.json.
2. Music wired: `apply_music` resolves `song_id` via `Library.get` (new additive method; `Library.add` now also records `audio_path`, since rows had no audio file path) or a plain existing file path, sets `music_path` through `_do_edit`; trim fields ignored with a logged note.
3. Revert invalidation: `revert_to_version` (also used by `revert_steps`) deletes plan_progress.json (`PLAN_PROGRESS_FILENAME` now lives in compile_journal); `execute_plan` also discards progress when plan hash differs or the current journal version is below the recorded snapshot. Re-post after revert re-applies; unchanged completed plan stays a no-op (intended, documented in docstring, tested).
4. Empty journal: preflight message "compilation has no journal entries" (409).
Also: explicit `_APPLY` dict replaces getattr dispatch.
Covering run: `cd api && uv run python -m pytest -q tests/test_edit_plan.py tests/test_compile_journal.py tests/test_music_library.py` -> 41 passed. Full: 808 passed, 1 skipped. ruff check src/ clean.
