"""Console-script shim for the runner: a readable error when settings fail.

`ai_video_editor.config` builds `Settings()` at import time, so a runner
started from the wrong directory (Task Scheduler defaults to System32)
fails before `runner.main()` could catch anything: `api/.env` is
cwd-relative. This module imports nothing from the app at import time;
it imports the real entry point inside a try and turns the settings
`ValidationError` into one line plus exit code 1.
"""

from __future__ import annotations

import importlib
import sys

from pydantic import ValidationError


def settings_error_message(exc: ValidationError) -> str:
    missing = sorted(
        {
            str(err["loc"][0]).upper()
            for err in exc.errors(include_input=False)
            if err.get("type") == "missing" and err.get("loc")
        }
    )
    invalid = sorted(
        {
            str(err["loc"][0]).upper()
            for err in exc.errors(include_input=False)
            if err.get("type") != "missing" and err.get("loc")
        }
    )
    msg = f"run from the api directory so api/.env loads (missing: {', '.join(missing) or 'none'})"
    if invalid:
        msg += f"; invalid: {', '.join(invalid)}"
    return msg


def run(target: str) -> None:
    """Import `module:function` and call it; settings errors exit 1 with a hint."""
    module, _, func = target.partition(":")
    try:
        entry = getattr(importlib.import_module(module), func)
    except ValidationError as e:
        print(settings_error_message(e), file=sys.stderr)
        sys.exit(1)
    entry()


def runner() -> None:
    run("ai_video_editor.grokbot.runner:main")
