# Setup

## Prerequisites

- **uv** (Python package manager)
- **FFmpeg** + **ffprobe** — `winget install Gyan.FFmpeg`. If not on
  PATH, set `FFMPEG_PATH` to the full `ffmpeg.exe` path (ffprobe is
  auto-derived as its sibling).

## Install & run

```bash
uv sync                 # install deps      (bun analogy: "bun install")
cp .env.example .env     # then edit .env
uv run dev               # serve :8000       (bun analogy: "bun run dev")
```

`uv run dev` maps to `uvicorn src.ai_video_editor.main:app --reload`
(see `[project.scripts]`). Interactive docs: `http://localhost:8000/docs`.

Lint/format before committing:

```bash
uv run ruff check src/ && uv run ruff format src/
```

## Environment variables (`.env`)

Secrets live only in `.env`, which is **gitignored** — never commit keys.

| Var | Required | Purpose |
|---|---|---|
| `OUTPLAYED_MEDIA_DIR` | yes | Root of Outplayed recordings to scan |
| `WORKSPACE_DIR` | yes | Output dir (clips, renders, rankings) |
| `FFMPEG_PATH` | if not on PATH | Full path to `ffmpeg.exe` |
| `ANTHROPIC_API_KEY` | for `rank` | Anthropic key |
| `RANKER_MODEL` | no | Default `claude-haiku-4-5` |
| `ANTHROPIC_MAX_RANK_CALLS` | no | Per-process LLM-call cap (default 25) |
| `LANGFUSE_PUBLIC_KEY` / `_SECRET_KEY` / `_BASE_URL` | no | Tracing; disabled if absent |
| `RIOT_API_KEY` | for Riot source | Dev key (expires every 24 h) |
| `RIOT_ID` | for Riot source | `gameName#tagLine` |
| `RIOT_REGION` | no | Regional routing: `americas`/`asia`/`europe`/`sea` (default `americas`) |
| `OUTPLAYED_CLIP_MAX_SECONDS` | no | Clip vs recording cutoff (default 120) |
| `ANALYZE_AUDIO_MAX_SECONDS` | no | Skip audio analysis past this (default 3600) |
| `MIN_FREE_DISK_MB` | no | Disk-preflight margin (default 1024) |
| `ANALYZE_*` | no | Peak threshold / padding / max candidates |

## Troubleshooting

- **Job stuck `running`** → check ffmpeg/ffprobe resolves
  (`FFMPEG_PATH`). Errors are captured into the job's `error` field.
- **`rank` fails with billing 400** → Anthropic balance is empty; not a
  code error. Add credits or lower usage.
- **`SpendCapError`** → hit `ANTHROPIC_MAX_RANK_CALLS`; restart the
  server or raise the cap. No API call was made.
- **Riot returns no candidates** → dev key expired (24 h), or the
  recording predates the recent-match lookback window, or it's not a
  League file. All expected; other sources still work.
- **Config changes not taking effect** → `settings` loads once at
  import; fully restart the server (kill stray `python.exe` first).
- **Disk full during audio** → the preflight should prevent it; raise
  free space or lower `ANALYZE_AUDIO_MAX_SECONDS`.
