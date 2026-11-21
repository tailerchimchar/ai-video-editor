"""`grokbot-note`: one plain-text event from the Claude session on the PC to the Lead.

The Lead relays it to Noodlz (see docs/grokbot/lead.md, `claude.note`).
Text only: no paths, no names, no secrets. Flushes the webhook sink so the
event is posted before the process exits.
"""

from __future__ import annotations

import argparse
import sys

from ..notify import Event, emit, sinks

MAX_CHARS = 1200
URGENCIES = ("info", "needs_you")


def note_event(text: str, *, urgency: str = "info") -> Event:
    text = " ".join(text.split())
    if not text:
        raise ValueError("note text is empty")
    if len(text) > MAX_CHARS:
        raise ValueError(f"note text over {MAX_CHARS} chars")
    if urgency not in URGENCIES:
        raise ValueError(f"urgency must be one of {URGENCIES}")
    return Event(
        event="claude.note",
        payload={"text": text, "urgency": urgency, "from": "claude-pc"},
        priority=True,
    )


def send_note(text: str, *, urgency: str = "info", emit_fn=emit, flush: bool = True) -> Event:
    event = note_event(text, urgency=urgency)
    emit_fn(event)
    if flush:
        sink = sinks._webhook_factory()
        if sink is not None:
            sink.flush(timeout=15)
    return event


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Send a claude.note event to the Lead bot.")
    parser.add_argument("text", help="plain text, no paths or names")
    parser.add_argument("--needs-you", action="store_true", help="mark it as needing Noodlz")
    args = parser.parse_args(argv)
    try:
        send_note(args.text, urgency="needs_you" if args.needs_you else "info")
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)
    print("sent")
