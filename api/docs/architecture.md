# Architecture

## Layers (Single Responsibility)

```
HTTP            routers/{assets,clips,projects,jobs,analyze}.py
                  │  (validation, job lifecycle — no ffmpeg/HTTP/LLM here)
Orchestration   candidates/service.py   ranker.py   highlights.py
                  │  (compose sources)   (1 LLM call)  (cut kept → folder)
I/O shells      candidates/{audio,riot,transcript}   editing.trim_clip
                  │  ffmpeg / ffprobe / numpy / Riot HTTP  (shared ffmpeg)
Persistence     database.py  (aiosqlite, raw SQL, Postgres-portable)
Cross-cutting   config.py (pydantic-settings)   tracing.py (Langfuse)
```

Each layer depends only on the one below it. Routers never call ffmpeg
or Anthropic directly; they enqueue a job and delegate.

## Data flow

```
POST /assets/scan          walk OUTPLAYED_MEDIA_DIR → assets table
POST /assets/{id}/candidates   job → service.compute_candidates(asset)
                                    → highlight_candidates rows
GET  /assets/{id}/candidates   inspect raw candidates
POST /assets/{id}/rank         job → ranker.rank_candidates()
                                    → workspace/rankings/{id}.json
GET  /assets/{id}/rankings     ranked suggestions
POST /assets/{id}/highlights   job → highlights.build_highlights()
                                    → organized folder + index.md/json
GET  /assets/{id}/highlights   the folder's index.json
GET  /jobs/{id}                poll any background job
```

`highlights/<game>/<date>_<champion>/` — folder name is a pure function
of the recording + candidates (`highlights.relative_folder`), so the
writer (POST) and reader (GET) always agree without storing the path.

Long-running work (ffmpeg, audio decode, LLM) runs in
`asyncio.to_thread` background tasks tracked in the `jobs` table. The
client polls; nothing blocks a request.

## Candidate model

One table, `highlight_candidates`, with a `source` discriminator:

```
id, video_id, source, start_seconds, end_seconds,
event_type, confidence, metadata (JSON), created_at
```

Adding a source is **open/closed**: new `candidates/<x>.py` exposing
`detect_*() -> list[dict]`, register it in `service.compute_candidates`,
add the string to `CandidateSource` in `models.py`. The ranker and
routers don't change.

## Pure core / impure shell

Logic that benefits from unit tests is kept pure and separated from I/O:

- `riot._game_window_ms / _pick_match / _kill_candidates` — pure
  correlation, tested offline with synthetic matches (no Riot key).
- `audio` RMS/clustering — pure numpy given a decoded buffer.
- The HTTP/ffmpeg shells around them are thin and swappable.

## Background jobs

`jobs(id, project_id, type, status, output_path, error, created_at,
completed_at)`. Types: `clip`, `render`, `candidates`, `rank`. Error
handling is defensive — even the failure-writing path is wrapped so a
disk-full condition can't turn into an unhandled task crash.

## Safety guards

- **Disk preflight** before audio WAV extraction: refuses if free space
  < estimated WAV size + margin (`MIN_FREE_DISK_MB`).
- **Duration cap** `ANALYZE_AUDIO_MAX_SECONDS` skips pathologically long
  recordings.
- **Spend cap** `ANTHROPIC_MAX_RANK_CALLS` bounds LLM calls per process.
- **Bounded LLM client**: explicit `max_retries` + `timeout`.
- Source files are read-only; all writes go to `WORKSPACE_DIR`.
