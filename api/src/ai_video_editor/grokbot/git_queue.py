"""The queue repo on disk: pull, read items, write results, commit and push."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

# Only paths the runner writes; never `add -A` on the whole tree.
_TRACKED = ("queue", "rejected", "SLEEP")


class QueueRepo(Protocol):
    root: Path

    def pull(self) -> None: ...
    def list_items(self) -> list[tuple[str, str]]: ...
    def write(self, relative_path: str, text: str) -> None: ...
    def remove(self, relative_path: str) -> None: ...
    def commit_push(self, message: str) -> bool: ...
    def retry_push(self) -> bool: ...


class GitQueueRepo:
    """Git via argv lists only. Network failures are logged, never raised."""

    def __init__(self, root: Path, *, git: str = "git", run: Callable = subprocess.run):
        self.root = Path(root)
        self._git = git
        self._run = run

    def _g(self, *args: str):
        return self._run(
            [self._git, "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def pull(self) -> None:
        r = self._g("pull", "--ff-only")
        if r.returncode == 0:
            return
        logger.warning("queue pull failed: %s", (r.stderr or "")[:200])
        self._resync()

    def _resync(self) -> None:
        """Diverged from origin: hard-reset, but only with nothing unpushed."""
        status = self._g("status", "-sb")
        head = (getattr(status, "stdout", "") or "").splitlines()[:1]
        if status.returncode != 0 or (head and "ahead" in head[0]):
            return  # local commits exist; keep retrying the push instead
        branch = (self._g("rev-parse", "--abbrev-ref", "HEAD").stdout or "").strip()
        if not branch or branch == "HEAD":
            return
        if self._g("fetch").returncode != 0:
            return
        self._g("reset", "--hard", f"origin/{branch}")

    def list_items(self) -> list[tuple[str, str]]:
        out = []
        for p in sorted((self.root / "queue").glob("*.json")):
            try:
                out.append((f"queue/{p.name}", p.read_text(encoding="utf-8", errors="replace")))
            except OSError as e:
                logger.warning("cannot read %s: %s", p, e)
        return out

    def write(self, relative_path: str, text: str) -> None:
        target = self.root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def remove(self, relative_path: str) -> None:
        (self.root / relative_path).unlink(missing_ok=True)

    def commit_push(self, message: str) -> bool:
        """Commit what the runner wrote, then push. True when pushed."""
        for path in _TRACKED:  # one call each: a missing path must not abort the rest
            self._g("add", "-A", "--", path)
        r = self._g("commit", "-m", message)
        if r.returncode != 0:
            logger.warning("queue commit failed: %s", (r.stderr or "")[:200])
        return self.retry_push()

    def retry_push(self) -> bool:
        r = self._g("push")
        if r.returncode != 0:
            # The local commit stays; the next tick retries the push.
            logger.warning("queue push failed (will retry): %s", (r.stderr or "")[:200])
            return False
        return True
