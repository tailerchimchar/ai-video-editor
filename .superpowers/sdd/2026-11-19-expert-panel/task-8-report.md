# Task 8 report (AVE-12)

Status: DONE_WITH_CONCERNS

## Implemented
- `api/src/ai_video_editor/music/arc.py`: pure `plan_arc`, `trim_to_peak`. Placement is a registry `_PLACEMENTS` (mode -> (plan format, rule)); `story` -> `story_short`, `arc_montage` -> `arc_montage`. Rules are small named functions.
  - arc_montage: best non-1K/2K clip on the drop; remaining clips greedy by intensity until within +-5% of target; rising share (by song rising/falling length) filled ascending, rest falling (descending). If no clip is eligible for the drop, raises ValueError.
  - story: chronological by start; only the song trim moves to the best clip's midpoint.
  - Mentor rule: topics `kill` and `multi_kill_2` never on the drop (`NOT_ON_THE_DROP`).
  - Plan music: `trim_to_peak=True`, `trim_start_s`/`trim_end_s` set (fields already existed on master).
- `shorts.py`: `render_short(..., snap_to_beats: bool = False)`; `_snap_plan_to_beats` uses `snap_clips_to_beats(strategy="kill_on_beat", tolerance 0.5s)`. Only montage with music; only anchored clips move (unanchored are un-trimmed pre-cut files); non-fatal fallback to unsnapped on any failure. Anchor offset is adjusted by the shift so flash times stay correct.

## TDD evidence
RED: `cd api && OUTPLAYED_MEDIA_DIR=/tmp/om WORKSPACE_DIR=/tmp/ws uv run python -m pytest -q tests/test_music_arc.py` -> ModuleNotFoundError `ai_video_editor.music.arc` (collection error).
RED: `... pytest -q tests/test_shorts_snap_to_beats.py` -> 5 failed, 1 passed (`render_short() got an unexpected keyword argument 'snap_to_beats'`).
GREEN: both files -> `17 passed`.
Full suite: `836 passed, 1 skipped`. `ruff check src/` clean; touched files formatted.

## Files
- new: `api/src/ai_video_editor/music/arc.py`, `api/tests/test_music_arc.py` (11 tests), `api/tests/test_shorts_snap_to_beats.py` (6 tests)
- changed: `api/src/ai_video_editor/shorts.py` (+57)

## Self-review / concerns
- `plan_arc` signature has no song id, so `PlanMusic.song_id` is the analysis `file_sha256`, not the library uuid; the caller must map it (or override song_id) before execution.
- Sync's `kill_on_beat` accumulates montage time using shifted durations, including unanchored clips whose duration does not actually change: small timing drift only when anchored and unanchored clips are mixed.
- 1K/2K are excluded only from the drop (per brief), not from rising/falling.
- `ruff format --check src/` not run repo-wide (existing shorts.py was not ruff-formatted before; I did not reformat it).
- MCP tool `plan_arc_montage` is a sibling-repo follow-up.
