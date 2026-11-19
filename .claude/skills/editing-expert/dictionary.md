# editing-expert dictionary

Format per entry: **term** · plain definition · where it lives in our code.

- **edit plan** · typed JSON the panel hands the session; every entry maps to one MCP tool · schema in SKILL.md; validator planned in `api/src/ai_video_editor/registry.py`.
- **spec.json** · the source of truth for a compilation's clips, order and effects · `WORKSPACE/compilations/<name>/spec.json`; see engineering.md.
- **journal** · append-only history of spec mutations, enables revert · `spec_history.jsonl`; `revert_compilation`.
- **ROI preset** · named screen region for zoom/focus (killfeed, minimap, ...) · `_ROI_PRESETS` in `edits.py`.
- **kill anchor** · ground-truth kill timestamp from the Outplayed log, used to snap windows · `anchoring.py`, `list_kill_anchors`.
- **killfeed pop** · the killfeed row animating in; needs ≥1s after the kill · mentor hard rule; VLM flag `killfeed_pop_cut_off`.
- **beat flash** · white flash on detected beats · `music/effects.py`, wired in `shorts.py`.
- **beat snap** · shifting cuts onto beats · `music/sync.py:snap_clips_to_beats`, not wired.
