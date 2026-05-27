# Documentation

AI Video Editor — a local-first, deterministic highlight pipeline for
Outplayed gameplay recordings.

Start with [`../CLAUDE.md`](../CLAUDE.md) for the high-level picture,
then dive in:

| Doc | What it covers |
|---|---|
| [problem.md](problem.md) | The problem, why candidate-first beats "AI watches video" |
| [codebase-tour.md](codebase-tour.md) | Plain-language tour: what each file does, what's solid vs shaky |
| [architecture.md](architecture.md) | System design, layers, data flow, SOLID rationale |
| [api.md](api.md) | Full HTTP endpoint reference (Phase 1 + 2) |
| [candidate-sources.md](candidate-sources.md) | How each candidate source works |
| [cost-and-tracing.md](cost-and-tracing.md) | Anthropic spend model, guards, Langfuse |
| [mcp.md](mcp.md) | MCP server — drive the pipeline from Claude |
| [editing-tools.md](editing-tools.md) | Editing tools catalogue + per-game profiles |
| [roadmap.md](roadmap.md) | RAG + reranker, Postgres, Overwolf |
| [setup.md](setup.md) | Install, env vars, running, troubleshooting |
