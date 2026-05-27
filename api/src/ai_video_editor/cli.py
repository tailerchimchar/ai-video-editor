"""Dev entry points.

Two scripts on purpose:

- ``dev`` — **no reload** by default. Safe for long-running jobs
  (transcribe, analyze, compile on big VODs). Was previously
  ``reload=True``, which caused WatchFiles to murder in-flight Whisper
  jobs whenever any .py file changed — burnt entire GPU-minute sessions
  before the cause was identified. The new default protects long jobs;
  opt back in when you actually need hot reload via ``dev-reload``.

- ``dev-reload`` — explicit opt-in to uvicorn's ``--reload``. Use for
  pure-UI / endpoint-tweak sessions where no long job is in flight.
"""

import uvicorn


def dev() -> None:
    """Run the API with NO auto-reload. Long jobs survive code edits."""
    uvicorn.run("ai_video_editor.main:app", reload=False, port=8000)


def dev_reload() -> None:
    """Run the API WITH auto-reload — use only when no long jobs are
    in flight. WatchFiles kills the worker on any .py edit; any
    background job (transcribe / analyze / compile) running at that
    moment dies silently and leaves an orphan `running` row in `jobs`."""
    uvicorn.run("ai_video_editor.main:app", reload=True, port=8000)
