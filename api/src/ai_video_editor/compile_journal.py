"""Spec history journal — undo-for-edits.

Every successful edit appends a snapshot of the post-edit spec to
`<compilation>/spec_history.jsonl`. Reverting walks the journal back
N entries, restores that spec, and re-renders. The revert itself
appends a new entry so it's also undoable.

Why JSONL: each line is a complete record, so partial writes during
a crash don't corrupt earlier history. Append-only means we never
seek or rewrite — `open('a')` is the entire write surface. Read
performance is fine for the size involved (~few KB per entry).

Storage layout:

    <compilation>/
      spec.json              ← current, mutable
      spec_history.jsonl     ← append-only, one line per snapshot
      compilation.mp4
      _parts/...

Schema of each JSONL line:

    {
      "ts": "2026-05-25T20:00:00+00:00",
      "action": "set_caption_mode",         # mutator name
      "details": {...} | null,              # optional action-specific
      "spec": {full spec snapshot},
    }

The full spec is duplicated each entry. Specs are small (~tens of KB)
so even a 100-edit history is ~1-2 MB on disk — fine.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime, timezone
from pathlib import Path

_JOURNAL_FILENAME = "spec_history.jsonl"
_SPEC_FILENAME = "spec.json"


def journal_path(folder: Path) -> Path:
    return Path(folder) / _JOURNAL_FILENAME


def spec_path(folder: Path) -> Path:
    return Path(folder) / _SPEC_FILENAME


def append_journal(
    folder: Path,
    spec: dict,
    *,
    action: str,
    details: dict | None = None,
) -> None:
    """Append one snapshot to the journal.

    Best-effort: an OS error here MUST NOT fail the edit. The user's
    edit already succeeded by the time we're called; losing one history
    line is annoying, breaking the edit because of it is unacceptable.
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "details": details,
        "spec": spec,
    }
    path = journal_path(folder)
    with contextlib.suppress(OSError), path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def read_journal(folder: Path) -> list[dict]:
    """Return all history entries oldest-first.

    Missing or unreadable lines are skipped (never raise) — same
    rationale as append: history is supplementary, never load-bearing.
    """
    path = journal_path(folder)
    if not path.is_file():
        return []
    entries: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return entries


def summarise_journal(folder: Path) -> list[dict]:
    """Compact view: timestamp + action + clip count + human display per entry.

    Used by the MCP `list_compilation_history` tool so the LLM doesn't
    have to swallow full spec snapshots just to summarise. The
    `display` field is the human-readable phrasing the webapp shows in
    the history rail (instead of raw action keys like
    `caption_mode:tiktok`).
    """
    out: list[dict] = []
    for i, entry in enumerate(read_journal(folder), start=1):
        spec = entry.get("spec") or {}
        action = entry.get("action") or ""
        details = entry.get("details") or {}
        out.append(
            {
                "version": i,
                "ts": entry.get("ts"),
                "action": action,
                "details": details,
                "display": format_action_display(action, details),
                "clip_count": len(spec.get("clips") or []),
            }
        )
    return out


def format_action_display(action: str, details: dict | None) -> str:
    """Turn raw action+details into a human-readable line.

    Single source of truth for history phrasing — keep in sync as new
    mutators ship. Falls back to the raw action key when details are
    missing so it always renders SOMETHING.
    """
    details = details or {}
    clip = _format_clip_ref(details.get("clip_ref"))

    if action == "initial_compile":
        order = details.get("order") or "default"
        limit = details.get("limit")
        bits = [f"{order} order"]
        if limit:
            bits.append(f"limit {limit}")
        return f"Initial compile ({', '.join(bits)})"

    if action.startswith("add_effect:"):
        kind = action.split(":", 1)[1]
        effect = details.get("effect") or {}
        if kind == "zoom":
            factor = effect.get("factor")
            roi = effect.get("roi")
            bits: list[str] = []
            if factor:
                bits.append(f"{factor}x")
            if isinstance(roi, str) and roi not in ("center", "full"):
                # Strip game suffix for readability: "minimap_lol" → "minimap"
                bits.append(roi.removesuffix("_lol"))
            extra = f" ({' · '.join(bits)})" if bits else ""
            return f"Added zoom to clip {clip}{extra}"
        if kind == "focus":
            return f"Added focus to clip {clip}"
        if kind == "caption":
            text = effect.get("text") or ""
            preview = (text[:24] + "…") if len(text) > 24 else text
            return f"Added caption to clip {clip} · {preview!r}"
        return f"Added {kind} effect to clip {clip}"

    if action == "extend_clip":
        before = float(details.get("before") or 0)
        after = float(details.get("after") or 0)
        parts: list[str] = []
        if before:
            parts.append(f"{before:+.1f}s before")
        if after:
            parts.append(f"{after:+.1f}s after")
        deltas = " · ".join(parts) if parts else "no change"
        return f"Extended clip {clip} ({deltas})"

    if action.startswith("caption_mode:"):
        mode = action.split(":", 1)[1]
        return f"Clip {clip} → {mode} captions"

    if action == "edit_captions":
        n = details.get("segment_count")
        suffix = f" · {n} segments" if isinstance(n, int) else ""
        return f"Edited captions on clip {clip}{suffix}"

    if action == "add_caption":
        preview = details.get("text_preview") or ""
        return f"Added caption to clip {clip} · {preview!r}"

    if action == "remove_caption":
        seg_idx = details.get("segment_index")
        return f"Removed caption from clip {clip} (segment #{seg_idx})"

    if action == "tiktokify":
        return f"Clip {clip} → TikTok captions (word-by-word)"

    if action.startswith("labels:"):
        state = action.split(":", 1)[1]
        return f"Iteration labels {state}"

    if action == "insert_clip":
        position = details.get("position")
        where = f" at position {position}" if position else " (chronological)"
        return f"Inserted manual clip{where}"

    if action == "remove_clip":
        return f"Removed clip {clip}"

    if action == "set_intro":
        intro = details.get("intro_name") or "default"
        return f"Set intro to {intro!r}"

    if action == "clear_intro":
        return "Removed intro"

    if action == "insert_intro_at_position":
        intro = details.get("intro_name") or "default"
        after_clip = details.get("after_clip")
        position = details.get("position")
        if after_clip:
            return f"Inserted intro {intro!r} after clip {_format_clip_ref(after_clip)}"
        if position:
            return f"Inserted intro {intro!r} at position {position}"
        return f"Inserted intro {intro!r}"

    if action.startswith("reorder:"):
        mode = action.split(":", 1)[1]
        return f"Reordered clips by {mode}"

    if action == "revert":
        target = details.get("to_version")
        return f"Reverted to v{target:02d}" if isinstance(target, int) else "Reverted"

    # Fallback — keep something readable even for unknown actions
    return action.replace("_", " ").replace(":", " · ") if action else "Edit"


def _format_clip_ref(ref: object) -> str:
    """Display a clip_ref string. Numeric refs get `#NN` styling; others
    pass through (UUID prefixes, time strings)."""
    if ref is None:
        return "?"
    s = str(ref)
    if s.isdigit():
        return f"#{int(s):02d}"
    return s


class RevertError(RuntimeError):
    """Raised when a revert can't proceed (e.g. not enough history)."""


def revert_to_version(folder: Path, version: int) -> dict:
    """Restore the spec from a specific journal version (1-based).

    Writes the restored spec back to `spec.json`. Returns the restored
    spec dict so the caller can immediately re-render.

    Raises `RevertError` when:
      - `version` is out of range (no history that far back)
      - the journal is empty (nothing to revert to)
    """
    entries = read_journal(folder)
    if not entries:
        raise RevertError("no journal entries — nothing to revert to")
    if version < 1 or version > len(entries):
        raise RevertError(f"version {version} out of range (have {len(entries)} entries)")
    target = entries[version - 1]
    restored = target.get("spec")
    if not isinstance(restored, dict):
        raise RevertError(f"journal entry {version} has no usable spec snapshot")
    spec_path(folder).write_text(json.dumps(restored, indent=2), encoding="utf-8")
    return restored


def revert_steps(folder: Path, steps: int = 1) -> tuple[dict, int]:
    """Walk back `steps` entries from the current state.

    Equivalent of "undo N times." `steps=1` restores the previous
    snapshot (i.e. the second-most-recent journal entry, since the
    most-recent entry is the current state).

    Returns (restored_spec, version_restored). Raises `RevertError`
    when there aren't enough entries.
    """
    entries = read_journal(folder)
    if not entries:
        raise RevertError("no journal entries — nothing to revert")
    target_version = len(entries) - steps
    if target_version < 1:
        raise RevertError(
            f"can't go back {steps} steps; only {len(entries) - 1} undo levels available"
        )
    restored = revert_to_version(folder, target_version)
    return restored, target_version
