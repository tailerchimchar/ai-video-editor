# MCP Expert

---
name: mcp-expert
description: Knowledge of the ai-video-editor MCP surface — how the sibling stdio server wraps the HTTP API, the coverage policy that every mutating endpoint owes a tool, the clip_ref standard, tool naming conventions, the job-poll pattern, and the current catalog of every tool that exists. Invoke when adding a new mutating endpoint, writing or updating an MCP tool docstring, diagnosing missing coverage, or answering "what tools do we have for X?"
---

## The sibling-repo split

MCP is not in this repo. It lives at `../mcp/` (GH: `ai-video-editor-mcp`, MIT). It is a **stdio FastMCP server** — one entry point (`main()` in `mcp/src/ai_video_editor_mcp/server.py:1307`) that Claude Code spawns as a subprocess and talks to over stdin/stdout.

The wiring lives in this repo's `.mcp.json`:

```
{
  "mcpServers": {
    "ai-video-editor": {
      "command": "uv",
      "args": ["run", "--directory", "C:\\Users\\taile\\source\\repos\\ai-video-editor\\mcp", "ai-video-editor-mcp"],
      "env": { "AI_VIDEO_EDITOR_URL": "http://localhost:8000" }
    }
  }
}
```

The sibling directory is called via `uv run --directory <path> ai-video-editor-mcp`. The env var `AI_VIDEO_EDITOR_URL` tells the server where the backend is (defaults to `http://localhost:8000`).

**The MCP is a thin HTTP wrapper.** Every tool is a call to an existing FastAPI endpoint. No backend logic lives in the MCP. From `mcp/src/ai_video_editor_mcp/server.py:1`:

> Thin stdio adapter over the HTTP API — every tool is a wrapper over an existing endpoint. No backend logic lives here. Job-based steps poll internally so one tool call = one completed step.

**Backend implication (from api/CLAUDE.md):** never import from `mcp` in this repo, and never add MCP tool code in this repo. Adding tool code here is a coupling regression the split exists to prevent. The sibling repo must be installable without dragging in FastAPI, sqlite, faster-whisper, etc.

## The MCP coverage policy

Quoted verbatim from api/CLAUDE.md (the authoritative source):

> **Every editing operation must have a corresponding MCP tool.** The user drives all editing through Claude / MCP — if an action is only reachable via raw HTTP, it doesn't exist from their perspective.
>
> When you add a new mutating endpoint, you owe BOTH halves:
>
> 1. The HTTP endpoint in `routers/` (mutator + render + journal).
> 2. A matching MCP tool in `../mcp/src/ai_video_editor_mcp/server.py` that calls it. The tool's docstring is what the LLM sees — write it like a user manual, not internal docs.
>
> Read-only/inspection endpoints (GET) get tools too when they're part of an iterative workflow (e.g. `list_compilation_clips`, `list_compilation_history`).

**Practical read.** Adding a POST/PATCH/DELETE endpoint to a router without a matching `@mcp.tool()` in the sibling repo is a silent breakage of the policy. It won't fail lint; it won't fail tests; the LLM just can't invoke it. Treat "no MCP tool" as "endpoint doesn't exist for the user."

GET endpoints get a tool only when they're part of an iterative loop — the LLM has to inspect state between actions. `list_compilation_clips` (before an edit) and `list_compilation_history` (before a revert) are the canonical examples. A pure debug/health GET does not need a tool.

## Naming convention

From api/CLAUDE.md:

> Naming convention for compilation-editing tools:
> - Prefix with the noun: `compilation_…`, `intro_…`, `clip_…`
> - Use a verb that matches the user's phrasing: "add the intro after clip 3" → `insert_compilation_intro_after`, not `add_intro_v2`
> - Keep `clip_ref` parameter naming consistent (1-based index / UUID prefix / `"M:SS"` time — same as everywhere else)

Noun-first names are how the LLM finds relevant tools. When Claude scans the tool list for "insert an intro after clip 3", it can filter on `intro_` or `compilation_` and locate the right function immediately. Verb-first (`add_intro`, `add_zoom_effect_v2`) buries the noun under a common verb and defeats scanning.

Verb should mirror the user's phrasing. Common good verbs seen in the current catalog: `list_`, `get_`, `create_`, `update_`, `set_`, `clear_`, `insert_`, `remove_`, `reorder_`, `revert_`, `regenerate_`, `extract_`, `finalize_`, `tiktokify_`, `analyze_`, `detect_`, `split_`, `ingest_`, `scan_`, `search_`.

Where in-noun ambiguity is possible ("intro" as compilation-level vs library-level), the endpoint prefix disambiguates: `set_default_intro` (library-level) vs `set_compilation_intro` (attach to a compilation).

## The `clip_ref` standard

`clip_ref` is the single input format the LLM uses to point at a specific clip in a compilation. Every clip-scoped tool takes it. The resolver at `api/src/ai_video_editor/compile.py:770` accepts, in priority order:

1. **Integer or numeric string** → 1-based clip index. `"2"` means clip #2 (the second clip in the reel). Preferred by the LLM when a natural number came out of the user's prompt ("edit the second clip").
2. **UUID prefix** → a leading substring of a clip's UUID id, minimum 4 chars. Preferred when the LLM already has the id from `list_compilation_clips`.
3. **`"M:SS"` or seconds** → time string. Tries REEL time first (position within the concat output), then falls back to SOURCE time (position within the underlying asset). Preferred when the user says "the moment at 0:32."

Every tool takes `clip_ref: str` and documents these forms in its docstring the same way — see `zoom_compilation_clip` at `mcp/src/ai_video_editor_mcp/server.py:378` for the canonical phrasing:

> `clip_ref` accepts a 1-based index ('2'), a UUID prefix, or a time string ('0:32' tries reel time first, then source time).

Adding a NEW clip-scoped tool with a different ref format ("0-based" / "clip name" / "hash") breaks the LLM's mental model. The resolver is centralized on purpose — call `resolve_clip_ref(spec, body.clip_ref)` server-side in the router.

## Tool docstrings are the LLM UI

The docstring inside `@mcp.tool()` is what Claude sees when deciding whether to call the tool. It is a user manual, not internal docs. Optimize for the LLM reader:

1. **First sentence** = "what this does + when to use it." Example, `tiktokify_clip_captions` at `mcp/src/ai_video_editor_mcp/server.py:886`:

   > Switch a clip's captions to TikTok style — explode + restyle.

2. **Detail paragraphs** = clarify semantics, list constraints and prerequisites, name common failure modes. `transcribe_asset` at `mcp/src/ai_video_editor_mcp/server.py:97` calls out CPU-slow behavior + how to recover:

   > Short clips finish in seconds; long VODs are slow on CPU and may report 'timeout' here while still running — re-check with get_transcript / get_job.

3. **Parameter shape** when non-obvious. `edit_clip_captions` at `mcp/src/ai_video_editor_mcp/server.py:907` shows an inline example of the `segments` list because Whisper's word-shape is not intuitive.

4. **Return contract** when the caller needs to branch on it. Example, `detect_champion` at `mcp/src/ai_video_editor_mcp/server.py:1130`:

   > Returns the detection result: `{name, confidence, source: "cv", datadragon_version, sample_seconds}` — or `{name: null, reason: "no_match"}` when the best match fell below threshold.

Bad docstrings describe internals ("calls `_edit` with the zoom payload") instead of behavior. That tells the LLM nothing about when to invoke.

## Job-based tools poll internally

Long-running actions (transcribe, rank, compile, split, ingest, cut_highlights) run as background jobs on the backend. The pattern in the MCP is: one tool call = one completed step. The tool submits the POST, gets a `job_id`, polls `GET /jobs/{id}` every 2s until `status ∈ {completed, failed}`, and returns the completed job's result — the LLM never sees a raw job id.

The helper at `mcp/src/ai_video_editor_mcp/server.py:32`:

```python
def _wait_for_job(client: httpx.Client, job_id: str) -> dict:
    """Poll a background job until it finishes (or times out)."""
    deadline = time.monotonic() + _JOB_TIMEOUT_S
    while time.monotonic() < deadline:
        job = client.get(f"{API}/jobs/{job_id}").json()
        if job.get("status") in ("completed", "failed"):
            return job
        time.sleep(2)
    return {"status": "timeout", "error": f"job {job_id} did not finish in {_JOB_TIMEOUT_S}s"}
```

`_JOB_TIMEOUT_S` is 300 (5 min). Long jobs (multi-game splits, hour-long transcription, compile with lots of clips) can exceed that. On timeout the tool returns `{"status": "timeout", ...}` and the LLM can `get_job(job_id)` later. The rank / transcribe / compile / ingest docstrings all mention this.

Two shapes of job-based tool:

- **Submit + wait + fetch structured result** (typical). Example, `rank_asset` at `mcp/src/ai_video_editor_mcp/server.py:125`: submit, wait, then `GET /assets/{id}/rankings` for the actual ranked list; return only the `keep`-flagged rows sorted by hype.
- **Submit + wait + return the raw job dict** (when there's no separate GET). Example, `split_vod_into_games` at `mcp/src/ai_video_editor_mcp/server.py:1188` — the `output_path` of the completed job carries a comma-separated list of new child asset ids; no separate GET.

The shared helper `_edit` at `mcp/src/ai_video_editor_mcp/server.py:213` handles the third-most-common shape: submit, wait, return `{status, output}` — used by asset-level `zoom_clip` / `caption_clip` / `focus_clip` at `mcp/src/ai_video_editor_mcp/server.py:222,248,273`.

The wrapper `_compile_edit` at `mcp/src/ai_video_editor_mcp/server.py:373` is the *non-job* pattern for compilation-scoped edits (the mutation renders synchronously in the request; no job). Used by every `*_compilation_clip` tool. Do not add `_wait_for_job` calls to compilation mutators — they don't return job ids.

## How to add a new tool

The checklist when a new user-facing action lands:

1. **HTTP endpoint in `api/src/ai_video_editor/routers/`.** Pydantic request body, response model, mutator → render → journal (`append_journal` is auto-called by the edits router's shared helpers when relevant). Follow existing patterns in `routers/edits.py` — never call ffmpeg from a router directly; go through `compile.py` / `edits.py` / `splitter.py`.
2. **API tests.** The MCP has no unit tests today; the API endpoint is what has coverage. Any behavior you want to guarantee is exercised at the HTTP layer.
3. **`@mcp.tool()` in `mcp/src/ai_video_editor_mcp/server.py`.** Follow the naming convention (noun-prefix + verb matching user phrasing). Wire through `_client()` / `_wait_for_job` if the endpoint is job-based, `_edit` for asset-level edits, `_compile_edit` for compilation-scoped edits, or a fresh `with _client() as c:` call for one-off shapes.
4. **Docstring.** LLM-facing user manual. What the tool does. When to use it. Params (including `clip_ref` semantics if applicable). Return shape when non-obvious.
5. **Prerequisites in the docstring.** If the tool requires the asset to have been transcribed / ranked first, say so. The LLM won't guess.
6. **Restart the Claude Code session.** Tools are discovered on stdio startup; a running Claude Code session doesn't see the new tool until restart. The recently-added `backfill_asset_durations` and `split_vod_into_games` tools both required a session restart before Claude could invoke them.

## Session lifecycle and tool discovery

The MCP subprocess starts once per Claude Code session. FastMCP's `mcp.run()` in `mcp/src/ai_video_editor_mcp/server.py:1307` initializes the stdio server and Claude reads the tool manifest at connect time. **Adding a new `@mcp.tool()` requires a session restart to become visible.**

Symptoms of a stale session:
- New tool doesn't appear in Claude's function list
- `ToolSearch` doesn't surface the tool name
- The backend endpoint responds normally to direct `curl`

Fix: exit Claude Code, restart. The `.mcp.json` command re-runs, FastMCP re-registers all `@mcp.tool()` functions, tools become available.

The backend does NOT need to restart when adding an MCP tool — only when the underlying HTTP endpoint changes.

## Testing

No unit tests exist in the `mcp/` sibling repo today. The API endpoint is where behavior is tested (see `api/tests/` — 332 tests as of `33347ab`). The MCP is a thin adapter — its correctness reduces to:

1. The HTTP endpoint exists and behaves as the tool docstring claims.
2. The tool serializes the payload correctly.
3. Job polling terminates.

Smoke-test a new tool by:
1. Booting the API (`cd api && uv run dev`).
2. Restarting Claude Code so the tool becomes visible.
3. Invoking the tool via `mcp__ai-video-editor__<tool_name>` from Claude and inspecting the return.

For a raw HTTP smoke test bypassing MCP: `curl -X POST http://localhost:8000/api/v1/<path>` against the endpoint directly.

## Tool catalog (as of `33347ab`)

Every tool currently registered, grouped by domain. Line refs point at the `@mcp.tool()` decorator in `mcp/src/ai_video_editor_mcp/server.py`. Total: 46 tools.

### Assets — inventory, ingest, source management (10)

- `scan_assets()` — walk `OUTPLAYED_MEDIA_DIR`, index new .mp4s. Free, local. `L44`
- `list_assets(game=None, limit=20)` — filtered inventory, newest first. `L52`
- `regenerate_asset_thumbnail(asset_id)` — extract/refresh poster JPG. `L1216`
- `backfill_asset_durations()` — probe NULL `duration_seconds` rows in bulk. One-shot maintenance; idempotent. `L1233`
- `ingest_vod_url(url, game)` — yt-dlp download into `OUTPLAYED_MEDIA_DIR/<game>/`; auto-tagged `source_origin='downloaded'`. Job-based. `L1251`
- `delete_vod_source(asset_id)` — reclaim disk for `downloaded` assets; refuses manually-placed files. `L1168`
- `split_vod_into_games(asset_id)` — ffmpeg blackdetect → per-game child assets linked via `parent_asset_id`. Job-based. `L1188`
- `get_job(job_id)` — poll any background job by id. Used to re-check jobs that timed out during their originating tool call. `L183`
- `detect_champion(asset_id, at_seconds=None, min_confidence=0.45)` — CV template match against Data Dragon portraits. LoL-specific. Job-based. `L1130`
- `get_feedback_summary(compilation_id=None)` — aggregate user-edit stats (extend medians per event_type, action counts). Advisory only. `L1281`

### Candidates & analysis — the deterministic pipeline (5)

- `generate_candidates(asset_id)` — run outplayed_clip + riot_api + audio_peak + transcript_keyword sources. Free, local. Job-based. `L73`
- `list_candidates(asset_id)` — raw (un-ranked) candidate rows. `L90`
- `rank_asset(asset_id)` — one Anthropic call (~$0.005), returns `keep`-flagged suggestions sorted by hype. Job-based. `L125`
- `cut_highlights(asset_id)` — trim each kept ranked suggestion into `WORKSPACE/highlights/<game>/<date>_<champion>/`. Job-based. `L141`
- `analyze_asset(asset_id, cut=False)` — one-shot orchestrator: generate → rank → (optional) cut. The simplest entry point. `L169`

### Batch clips — no-LLM pooling (1)

- `batch_clip_highlights(game, limit=30)` — pool `<game>/` Outplayed event clips into `highlights/<game>/clips_<date>/` newest-first. $0, no ranker. Job-based. `L155`

### Transcripts, embeddings, search (4)

- `transcribe_asset(asset_id)` — local Whisper STT (private, $0). Long VODs may timeout mid-run — re-check with `get_job`. Job-based. `L97`
- `get_transcript(asset_id)` — stored segments (start/end/text). `L117`
- `index_asset(asset_id)` — embed transcript into `sqlite-vec` embeddings table. Requires prior `transcribe_asset`. Job-based. `L190`
- `search_clips(query, limit=10, asset_id=None)` — NL semantic search over indexed transcripts. Returns matched clips (distance = lower is closer). `L201`

### Asset-scoped edits — one-off renders to `WORKSPACE/edits/` (3)

Not journaled, not compilation-scoped. Distinct from the compilation-editing family below. `_edit` helper at `mcp/src/ai_video_editor_mcp/server.py:213`.

- `zoom_clip(asset_id, start, end, factor=2.0, roi='center', aspect='16:9')` — crop+scale on an ROI. `roi` accepts preset names or `full`. Job-based. `L223`
- `caption_clip(asset_id, start, end, text=None, aspect='16:9')` — burn captions; `text=None` auto-pulls transcript segments overlapping [start,end]. Job-based. `L249`
- `focus_clip(asset_id, start, end, x=0.5, y=0.5, radius=0.2, dim=0.3, aspect='16:9')` — spotlight. Job-based. `L274`

### Compilations — top-level lifecycle (5)

- `compile_highlights(asset_id, aspect='16:9', order='hook', limit=None, fade_seconds=0.3, music_path=None, music_volume=0.25)` — full pipeline: per-clip render → concat → optional music mix. `order ∈ {hook, hype, chronological, narrative}`. Job-based. `L303`
- `list_compilations(asset_id=None, limit=20)` — recent renders, newest first. `L354`
- `list_compilation_clips(compilation_id)` — clips with reel + source timestamps, current effects, caption counts. The pre-edit map. `L365`
- `finalize_compilation(compilation_id)` — clean final render (labels off). Sugar over `set_compilation_labels(id, enabled=False)`. `L487`
- `set_compilation_labels(compilation_id, enabled=True)` — flip iteration `#NN` overlay on/off. `L477`

### Compilation-scoped effects (6)

Add effects to a clip inside a rendered compilation. `_compile_edit` helper at `mcp/src/ai_video_editor_mcp/server.py:373`. Non-job (synchronous mutation + re-render inside the request).

- `zoom_compilation_clip(compilation_id, clip_ref, factor=1.5, roi='center')` — `L379`
- `focus_compilation_clip(compilation_id, clip_ref, x=0.5, y=0.5, radius=0.2, dim=0.3)` — `L395`
- `caption_compilation_clip(compilation_id, clip_ref, text)` — overlay branded caption ("PENTAKILL"). `L413`
- `extend_compilation_clip(compilation_id, clip_ref, before=0.0, after=0.0)` — grow source window. Only this clip re-renders. `L424`
- `insert_compilation_clip(compilation_id, asset_id, start, end, position=None, event_type='manual', text=None)` — manual insertion for moments the ranker missed. `L437`
- `remove_compilation_clip(compilation_id, clip_ref)` — drop a clip and re-concat. `L470`

### Compilation-scoped captions (5)

- `add_clip_caption(compilation_id, clip_ref, start, end, text, style_preset=None)` — insert single caption segment at time. `L838`
- `remove_clip_caption(compilation_id, clip_ref, segment_index)` — delete one segment by 0-based index. Out-of-range silently ignored. `L871`
- `tiktokify_clip_captions(compilation_id, clip_ref)` — destructive explode + restyle. `L886`
- `edit_clip_captions(compilation_id, clip_ref, segments)` — replace clip captions with edited FULL segment list. Drop `words` field on edited text (renderer even-splits). Master transcript unchanged. `L907`
- `tiktok_caption_clip(compilation_id, clip_ref)` / `segment_caption_clip(compilation_id, clip_ref)` — legacy caption_mode toggles. `L529, L548`

### Compilation-scoped reorder (2)

- `reorder_compilation_clips(compilation_id, mode)` — mode ∈ `{chronological, hype, funny, story, hook}`. Preserves intro positions. `L941`
- `reorder_compilation_clips_explicit(compilation_id, clip_ids)` — literal drag-and-drop commit. Full id set must match (no adds/drops via this path). `L966`

### Intros — library management (7)

- `list_intro_presets()` — logo PNGs in `<workspace>/intros/_presets/`. `L690`
- `list_intros()` — existing intros under `<workspace>/intros/`. `L702`
- `get_intro(name)` — config + rendered mp4 path. `L710`
- `create_intro(name, logo_source_path=None, preset=None, ...)` — image-mode intro. Provide EITHER `logo_source_path` OR `preset`, never both. `L558`
- `create_text_intro(name, text, ...)` — text-only procedural intro. `L617`
- `update_intro(name, ...)` — patch config fields and re-render. Only fields passed change. `L729`
- `render_intro(name)` — re-render from current `intro.json` (~1s). Use after hand-editing JSON; prefer `update_intro` otherwise. `L717`

### Intros — compilation attachment (4)

- `set_compilation_intro(compilation_id, intro_name=None)` — prepend at position 1. Replaces existing intro. `L767`
- `insert_compilation_intro_after(compilation_id, after_clip, intro_name=None)` — chapter-card style. Does NOT replace start-position intro. `L787`
- `insert_compilation_intro_at(compilation_id, position, intro_name=None)` — insert at a specific 1-based reel position. `L817`
- `clear_compilation_intro(compilation_id)` — remove start-position intro. `L1012`

### Default intro (2)

- `set_default_intro(intro_name)` — mark workspace default. Pass `None` to clear. `L990`
- `get_default_intro()` — current default (or null). `L1005`

### History and revert (2)

- `list_compilation_history(compilation_id)` — journal entries oldest-first. Every action + details + `clip_count` per version. `L1020`
- `revert_compilation(compilation_id, steps=1, to_version=None)` — walk back OR jump to specific journal version. Re-renders. Revert is itself journaled → undoable. `L1041`

### Maintenance and thumbnails (3)

- `regenerate_clip_thumbnails(compilation_id, force=False)` — backfill filmstrip JPGs for pre-filmstrip compilations. `L1066`
- `regenerate_thumbnail(compilation_id)` — re-extract compilation poster from current `compilation.mp4`. Frame chosen from highest-hype non-intro clip midpoint. `L1084`
- `cleanup_compilation(compilation_id, dry_run=False)` — sweep orphan `_parts/` files. Usually auto-called after every iterative edit; use manually only after rapid batch edits via raw HTTP or crash recovery. `dry_run=True` previews. `L1099`

### SFX library (1)

- `extract_sfx(asset_id, game, sound_name, start, end)` — cut audio span into `<workspace>/media_library/<game>/sfx/<sound_name>.wav`. Game aliases (`league`, `lol`, `League of Legends`) all resolve. Job-based. `L495`

## Anti-patterns

- **Adding tool code in this repo.** MCP tools live in `../mcp/src/ai_video_editor_mcp/server.py`. Never add `@mcp.tool()` decorators or import from `mcp` in the backend. See api/CLAUDE.md L131 for the rule.
- **Backend importing from mcp.** The sibling repo can be installed without the API's dependencies (FastAPI, sqlite, faster-whisper). Backend imports from `mcp` couple the two and defeat the split.
- **Docstrings that describe internals.** "Calls `_edit` with the zoom payload" tells the LLM nothing. Write what the tool DOES from the user's perspective, when to use it, and what the caller should do with the result.
- **Inventing new `clip_ref` formats.** The resolver at `compile.py:770` is the contract. A new tool that takes `clip_id: str` (0-based) or `clip_name: str` breaks the LLM's mental model — every clip tool takes `clip_ref: str` in the same three-form format.
- **Skipping job polling.** Returning a raw `job_id` and expecting the LLM to poll makes the tool half a step: `rank_asset → get_job → get_rankings` is three tool calls where one would do. Use `_wait_for_job` internally and return the final result. `get_job` exists as a safety net for tools that timed out; it is not the default flow.
- **Verb-first tool names.** `add_intro_v2` buries the noun. `insert_compilation_intro_after` scans well. See `set_compilation_intro` vs. `set_default_intro` — the noun disambiguates.
- **Adding a mutating endpoint without the MCP mirror.** Silent breakage of the coverage policy. The endpoint works via curl but does not exist to the LLM. If you can't invoke it from Claude after a session restart, you skipped step 3 of the checklist.
- **Forgetting the session restart after adding a tool.** Symptom: the tool works via `curl` but Claude claims no such function exists. Fix: exit and restart Claude Code so FastMCP re-registers.
- **Duplicated ranker calls in tools.** Only `rank_asset` (and `analyze_asset` via `rank_asset`) should hit the LLM. Every other tool is $0. If a new tool needs LLM-generated output, first ask whether a candidate + rank pass fits; a second LLM call is a design smell.
- **Modifying source assets from an MCP tool.** Source files are immutable (api/CLAUDE.md L245). Every write goes to `WORKSPACE_DIR`. The only exception is `delete_vod_source`, which explicitly gates on `source_origin='downloaded'` and refuses manually-placed files.
- **Assuming the compilation state cached by an earlier `list_compilation_clips` call is fresh.** Every mutating tool re-renders and shifts `#NN` indexes. Re-call `list_compilation_clips` between edits when the LLM needs current state (or use UUID prefixes as `clip_ref`s so indexes don't matter).
