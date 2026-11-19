# Task 10 report
Change: api/src/ai_video_editor/candidates/overwolf_events.py (_KILLFEED_NAME_BY_GAME, ~line 38-44): league entry now returns None with explanatory comment and `# TODO(league-killfeed-crop)`; header comment updated. The existing empty-name path keeps kill rows with killfeed_verified=False and never calls the verifier.
Test: api/tests/test_overwolf_league.py: replaced test_champion_name_verifies_all_nine_kills (asserted the old behaviour) with test_league_kills_survive_uncalibrated_killfeed_ocr (stub returns all False; asserts verifier never called, 9 kills, all unverified).
RED: `cd api && uv run python -m pytest -q tests/test_overwolf_league.py` -> 1 failed, 14 passed (verifier was called with Zeri).
GREEN: same command passes; full suite 943 passed, 1 skipped; ruff check clean; format check clean on touched files.
Branch worktree-agent-ab77d6399c300c5e5, worktree .claude/worktrees/agent-ab77d6399c300c5e5 (ff-merged master 9a5e71b first).
