"""Compilation workspace cleanup — orphan part-file removal with guardrails.

Iterative editing produces stale cached `_parts/*.mp4` files: clips
removed from the spec leave their part behind; inserting/removing clips
shifts every later clip's label index so old `_nN` variants stop being
valid. Without pruning, `_parts/` grows forever.

Design — pure/IO split mirroring `compile.py`:

    valid_part_filenames(spec) -> set[str]    # pure: what to keep
    find_orphans(folder, spec)  -> list[Path] # IO: scan _parts/
    cleanup_compilation(folder, spec=None, *, dry_run=False) -> CleanupReport

`render_spec` calls `cleanup_compilation` after a successful concat so
every iterative edit prunes itself with no per-handler wiring. The MCP
`cleanup_compilation` tool exposes the same entry-point for explicit
invocation / `dry_run` previews.

Safety guardrails (defense in depth — every layer assumes the others
might fail):

1. **Workspace containment.** Refuse any folder that isn't a direct
   child of `<workspace_dir>/compilations/`. A bug passing `C:\Windows`
   would raise `CleanupSafetyError` instead of deleting things.
2. **Strict filename regex.** Only files matching the exact
   `part_<8-hex>(_n<digits>)?.mp4` shape are eligible for deletion —
   anything else in `_parts/` (notes, debris, future cache shapes) is
   left alone.
3. **Symlink refusal.** `is_symlink()` paths are never unlinked —
   we don't want a symlinked-into-system-files trick to do damage.
4. **Audit log.** Every non-dry-run deletion is recorded in
   `<folder>/_cleanup.log` (append-only, JSON-lines).
5. **Errors don't propagate to renders.** `safe_cleanup_for_render`
   catches `CleanupSafetyError` so a misconfigured workspace can't
   undo a successful render.

Extension points (intentional):
- A new *kind* of cached file (e.g. thumbnails) adds its own
  `valid_<kind>_filenames` + a sibling `cleanup_<kind>` function; both
  compose at the `cleanup_compilation` level without touching
  existing logic.
- The "keep both label variants for instant flip" policy lives in
  `valid_part_filenames` — adjust there if the cache policy changes.
"""

from __future__ import annotations

import contextlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

# Strict shape for a cached part file. `id8` is the first 8 chars of a
# UUID (hex). `_nN` (decimal) marks the label-overlay variant. Anything
# in `_parts/` that doesn't match this shape is NOT considered an
# orphan — the cleanup leaves it alone rather than risk deleting a
# file we don't own. Keep this in sync with `_render_clip_part`'s
# filename construction in `compile.py`.
_PART_FILENAME_RE = re.compile(r"^part_[0-9a-f]{8}(_n\d+)?\.mp4$")

_AUDIT_LOG_NAME = "_cleanup.log"


class CleanupSafetyError(RuntimeError):
    """Raised when the safety preconditions for a cleanup are not met.

    Callers that want a "best-effort, never fail the render" surface
    should use `safe_cleanup_for_render` instead — it catches this
    exception and returns a structured report.
    """


@dataclass
class CleanupReport:
    """Outcome of a cleanup pass — safe to serialise into render summaries.

    `deleted_files` are relative basenames so reports stay portable
    across machines. `dry_run=True` reports what *would* be deleted
    without touching the filesystem.
    """

    deleted_files: list[str] = field(default_factory=list)
    deleted_bytes: int = 0
    kept_files: int = 0
    skipped_files: list[str] = field(default_factory=list)
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "deleted": len(self.deleted_files),
            "deleted_files": self.deleted_files,
            "freed_bytes": self.deleted_bytes,
            "kept": self.kept_files,
            "skipped": self.skipped_files,
            "dry_run": self.dry_run,
            "errors": self.errors,
        }


# ----- safety -----


def compilations_root() -> Path:
    """Canonical absolute path of the compilations directory.

    Read at call-time (not import-time) so tests can monkeypatch
    `settings.workspace_dir` before exercising the safety check.
    """
    return (settings.workspace_dir / "compilations").resolve(strict=False)


def is_safe_compilation_folder(folder: Path) -> bool:
    """True iff `folder` is a direct child of `<workspace>/compilations/`.

    The root itself is rejected (we don't want to sweep across reels);
    so are deeper paths (`compilations/<id>/_parts` would let a caller
    target the cache directory directly, which is what we'd be reading
    from anyway — but the function takes the COMPILATION folder, not
    its _parts subfolder).

    Symlinks are resolved before the check so a link planted outside
    the workspace can't pass.
    """
    try:
        folder_abs = Path(folder).resolve(strict=False)
        root = compilations_root()
    except (OSError, RuntimeError):
        return False
    if folder_abs == root:
        return False
    try:
        rel = folder_abs.relative_to(root)
    except ValueError:
        return False
    return len(rel.parts) == 1


# ----- pure -----


def valid_part_filenames(spec: dict) -> set[str]:
    """Filenames in `_parts/` the spec currently references — pure.

    For each clip we keep **both** the no-label part and the part
    matching the clip's *current* label position. That preserves the
    "instant label flip" property documented in `compile.render_spec`:
    toggling `show_clip_numbers` picks up the cached counterpart with
    no re-encode.

    Anything not in this set (and matching the strict part-file
    shape) is a true orphan — left over from a removed clip, or a
    stale `_nN` from before an insert/remove shifted positions.
    """
    valid: set[str] = set()
    for idx, clip in enumerate(spec.get("clips") or [], start=1):
        cid = clip.get("id")
        if not cid:
            continue
        id8 = cid[:8]
        valid.add(f"part_{id8}.mp4")
        valid.add(f"part_{id8}_n{idx}.mp4")
    return valid


def _is_eligible_part_file(path: Path) -> bool:
    """A file is eligible for cleanup consideration only if it matches
    the strict part-file shape AND isn't a symlink. Anything else
    (notes, debris, future cache shapes, hostile symlinks) gets left
    in place — we'd rather leak a few KB than mis-delete."""
    if not path.is_file():
        return False
    if path.is_symlink():
        return False
    return bool(_PART_FILENAME_RE.match(path.name))


def find_orphans(folder: Path, spec: dict) -> tuple[list[Path], list[Path]]:
    """Scan `<folder>/_parts/` and split into (orphans, skipped).

    `orphans` matches the strict part-file shape AND is not referenced
    by the spec. `skipped` is anything that looks part-file-like
    enough to mention but failed the eligibility check (symlink,
    weird shape) — surfaced in the report so the user can audit it.

    Returns empty lists when `_parts/` doesn't exist (initial-compile
    state). Never recurses into subdirectories.
    """
    parts_dir = Path(folder) / "_parts"
    if not parts_dir.is_dir():
        return [], []
    valid = valid_part_filenames(spec)
    orphans: list[Path] = []
    skipped: list[Path] = []
    for entry in sorted(parts_dir.iterdir()):
        if not entry.is_file():
            continue  # subdirs / sockets / pipes ignored silently
        # Anything that doesn't match the strict shape OR is a symlink
        # is left alone. We mention symlinks in `skipped` so the user
        # has a paper trail; unrecognised filenames are silent (could
        # be user's own files).
        if entry.is_symlink():
            skipped.append(entry)
            continue
        if not _PART_FILENAME_RE.match(entry.name):
            continue
        if entry.name in valid:
            continue
        orphans.append(entry)
    return orphans, skipped


# ----- IO -----


def _load_spec_if_needed(folder: Path, spec: dict | None) -> dict:
    """Cleanup callers from render_spec already have the spec in hand;
    the MCP tool only has the folder. Load lazily so we don't pay the
    JSON read in the common (auto-cleanup) path."""
    if spec is not None:
        return spec
    return json.loads((Path(folder) / "spec.json").read_text(encoding="utf-8"))


def _append_audit_log(folder: Path, deleted: list[str]) -> None:
    """JSON-lines append. One record per cleanup call (not per file)
    so the log stays compact even after many edits."""
    if not deleted:
        return
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "deleted": deleted,
    }
    log_path = folder / _AUDIT_LOG_NAME
    with contextlib.suppress(OSError), log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def cleanup_compilation(
    folder: Path | str,
    spec: dict | None = None,
    *,
    dry_run: bool = False,
    enforce_safety: bool = True,
) -> CleanupReport:
    """Prune orphan cached parts in a compilation folder. Idempotent.

    `spec` is optional — when omitted, `spec.json` is read from
    `folder`. Pass it explicitly from `render_spec` to skip the
    re-read.

    `dry_run=True` returns the would-delete list without touching
    disk — useful for an MCP "what would this remove?" preview.

    `enforce_safety=True` (default) requires `folder` to live under
    `<workspace_dir>/compilations/`. Set False ONLY in tests that
    use temp dirs outside the configured workspace.

    Raises `CleanupSafetyError` if the workspace containment check
    fails — callers that want a non-fatal surface should use
    `safe_cleanup_for_render`.

    Per-file failures (Windows file-in-use, permission errors) are
    captured in `errors` but never raised: a render that succeeded
    shouldn't fail post-hoc because one file was locked.
    """
    folder = Path(folder)
    if enforce_safety and not is_safe_compilation_folder(folder):
        raise CleanupSafetyError(
            f"refusing to clean {folder}: not under "
            f"{compilations_root()} (workspace containment check failed)"
        )

    spec = _load_spec_if_needed(folder, spec)
    report = CleanupReport(dry_run=dry_run)

    orphans, skipped = find_orphans(folder, spec)
    report.skipped_files = [p.name for p in skipped]

    parts_dir = folder / "_parts"
    if parts_dir.is_dir():
        # Count files that survive cleanup (eligible & matching the
        # part-file shape, minus orphans we're about to delete).
        all_eligible = sum(1 for p in parts_dir.iterdir() if _is_eligible_part_file(p))
        report.kept_files = all_eligible - len(orphans)

    for path in orphans:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if dry_run:
            report.deleted_files.append(path.name)
            report.deleted_bytes += size
            continue
        try:
            path.unlink()
        except OSError as exc:
            report.errors.append(f"{path.name}: {exc}")
            continue
        report.deleted_files.append(path.name)
        report.deleted_bytes += size

    if not dry_run and report.deleted_files:
        _append_audit_log(folder, report.deleted_files)

    return report


def safe_cleanup_for_render(folder: Path, spec: dict) -> dict:
    """Render-time wrapper: never raise, always return a serialisable dict.

    `render_spec` calls this so a cleanup hiccup (locked file,
    misconfigured workspace, safety violation) never turns a
    successful render into a failure. Returns the same shape as
    `CleanupReport.to_dict()` plus an `ok` flag.
    """
    try:
        report = cleanup_compilation(folder, spec)
        return {"ok": True, **report.to_dict()}
    except CleanupSafetyError as exc:
        return {"ok": False, "error": str(exc)[:500], "reason": "safety"}
    except Exception as exc:
        with contextlib.suppress(Exception):
            return {"ok": False, "error": str(exc)[:500]}
        return {"ok": False, "error": "unknown"}
