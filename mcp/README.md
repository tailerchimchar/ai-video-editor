# ai-video-editor-mcp

> Part of the **ai-video-editor** project. Sibling repos live under the
> same parent folder:
>
> ```
> ai-video-editor/
> ├── api/    ← FastAPI backend (GH: ai-video-editor-api)
> ├── mcp/    ← this repo (GH: ai-video-editor-mcp)
> └── (future) web/, utils/
> ```

MCP server for the [ai-video-editor-api](https://github.com/tailerchimchar/ai-video-editor-api)
backend. **Thin stdio adapter over the HTTP API** — every tool is a
wrapper over an existing endpoint. No backend logic, no DB, no LLM
calls. Just HTTP + polling.

```
Claude Code / Desktop
   │  MCP stdio
   ▼
ai-video-editor-mcp   (this repo — FastMCP, mcp + httpx only)
   │  HTTP localhost:8000
   ▼
ai-video-editor backend (separate repo)
```

## Why a separate repo?

- **Decoupled deps.** Installing the MCP doesn't pull in FastAPI,
  aiosqlite, anthropic, faster-whisper, fastembed, sqlite-vec…
- **Independent PRs.** You can ship a new MCP tool without touching the
  backend.
- **OSS-friendly.** Contributors can hack on the MCP side without
  running the whole pipeline.

## API contract

This server speaks to backend version `0.1.0+`. The relevant endpoints
live under `/api/v1/` of the backend; full reference is in the
backend's `docs/api.md`. If you add a new tool here, the matching
endpoint must already exist on the backend.

When the backend's HTTP contract changes, this server's version bumps
in lockstep — the supported backend range goes in this README.

## Install / run

Prerequisites: **uv** (Python package manager). The
[ai-video-editor-api](https://github.com/tailerchimchar/ai-video-editor-api)
backend must be running (`uv run dev` in `../api/`).

```bash
uv sync
uv run ai-video-editor-mcp     # stdio, point Claude at this
```

Point at a non-default backend with `AI_VIDEO_EDITOR_URL`:

```bash
AI_VIDEO_EDITOR_URL=http://localhost:9000 uv run ai-video-editor-mcp
```

## Wire it into Claude Code

The backend repo ships a `.mcp.json` that auto-discovers this server
via `uv run --directory ../mcp`. To register manually:

```bash
claude mcp add ai-video-editor -- uv run --directory \
  C:\Users\taile\source\repos\ai-video-editor\mcp ai-video-editor-mcp
```

For Claude Desktop, add the equivalent `mcpServers` block to its config.

## Tools

The full per-tool catalogue lives in the backend's
[`docs/mcp.md`](https://github.com/tailerchimchar/ai-video-editor-api/blob/main/docs/mcp.md)
and [`docs/editing-tools.md`](https://github.com/tailerchimchar/ai-video-editor-api/blob/main/docs/editing-tools.md)
— those are the source of truth.

Briefly, the tools cover: scanning recordings, generating + ranking
highlight candidates, cutting highlight folders, transcribing (local
Whisper), semantic clip search, per-clip editing primitives
(zoom/caption/focus), and compilation-reel editing (compile, iterate
on individual clips, insert/extend/remove, finalize).

## Layout

```
src/ai_video_editor_mcp/
├── __init__.py
└── server.py        ← FastMCP server; @mcp.tool() per backend op
tests/
└── test_server.py   ← thin httpx-mock smoke tests
pyproject.toml       ← deps: mcp[cli] + httpx (no backend deps)
```

## Contributing

1. Add a new tool? Decorate with `@mcp.tool()` in `server.py`, call the
   matching backend endpoint via the shared `_client()` / `_compile_edit`
   helpers. Mirror the docstring style of existing tools — the
   docstring is what Claude sees when picking tools.
2. Run `uv run ruff check src/ tests/` before opening a PR.
3. The backend endpoint must already exist before you add the tool.

## License

MIT (or match the backend's license — TBD).
