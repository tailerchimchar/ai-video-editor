"""Local runner: the only process on the PC that acts on the git queue."""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel

from .. import editing, notify
from ..config import settings
from ..notify import Event
from .drive import Uploader, uploader_from_settings
from .git_queue import GitQueueRepo, QueueRepo
from .queue import (
    AppliedLedger,
    DeployItem,
    EditPlanItem,
    PreviewRequestItem,
    QueueRejected,
    SleepItem,
    VerdictItem,
    WakeItem,
    parse_item,
    parse_sleep_flag,
)

logger = logging.getLogger(__name__)


def _version() -> str:
    try:
        return metadata.version("ai-video-editor")
    except metadata.PackageNotFoundError:
        return "dev"


class ApiClient(Protocol):
    def asset_path(self, asset_id: str) -> tuple[str, float]: ...
    def compilation_output(self, compilation_id: str) -> str | None: ...


class HttpApiClient:
    def __init__(self, base_url: str, client: httpx.Client | None = None):
        self._base = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=30.0)

    def asset_path(self, asset_id: str) -> tuple[str, float]:
        r = self._client.get(f"{self._base}/api/v1/assets/{asset_id}")
        if r.status_code == 404:
            raise LookupError(f"asset {asset_id} not found")
        r.raise_for_status()
        body = r.json()
        dur = body.get("duration_seconds")
        if dur is None:
            raise LookupError(f"asset {asset_id} has no known duration")
        return body["path"], float(dur)

    def compilation_output(self, compilation_id: str) -> str | None:
        r = self._client.get(f"{self._base}/api/v1/compilations/{compilation_id}")
        if not r.is_success:
            return None
        return r.json().get("output_path")


class RunnerState(BaseModel):
    sleeping: bool = False
    buffered: list[dict] = []
    last_heartbeat: float = 0.0
    version: str = _version()


_DEFERRED = (EditPlanItem, VerdictItem, DeployItem)


class Runner:
    def __init__(
        self,
        *,
        repo: QueueRepo,
        api: ApiClient,
        uploader: Uploader | None,
        ledger: AppliedLedger,
        emit: Callable[[Event], None] = notify.emit,
        trim=editing.trim_clip,
        previews_dir: Path,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.repo = repo
        self.api = api
        self.uploader = uploader
        self.ledger = ledger
        self._emit = emit
        self._trim = trim
        self.previews_dir = Path(previews_dir)
        self._clock = clock
        self.state = RunnerState()
        self._heartbeat_sent = False
        self._wrote = False
        self._outcomes: list[str] = []

    # -- emitting ------------------------------------------------------
    def _send(self, event: str, payload: dict) -> None:
        ev = Event(event=event, payload=payload)
        if self.state.sleeping:
            self.state.buffered.append(ev.model_dump())
        else:
            self._emit(ev)

    def _write(self, path: str, text: str) -> None:
        self.repo.write(path, text)
        self._wrote = True

    def _mark(self, id: str, kind: str, outcome: str) -> None:
        self.ledger.mark(id, kind, outcome)
        self._outcomes.append(outcome)

    def _reject(self, filename: str, id: str, kind: str, reason: str) -> None:
        self._write(f"rejected/{filename}", json.dumps({"id": id, "reason": reason}))
        self._send("queue.rejected", {"id": id, "reason": reason})
        self._mark(id, kind, "rejected")

    # -- tick ----------------------------------------------------------
    def tick(self) -> list[str]:
        self._wrote = False
        self._outcomes = []
        self.repo.pull()
        self.state.sleeping = parse_sleep_flag(self.repo.root)

        control, normal, pending = [], [], 0
        for path, text in self.repo.list_items():
            filename = Path(path).name
            if self.ledger.seen(Path(path).stem):
                continue
            try:
                item = parse_item(text)
            except QueueRejected as e:
                self._reject(filename, Path(path).stem, "?", e.reason)
                continue
            if self.ledger.seen(item.id):
                continue
            pending += 1
            is_control = isinstance(item, (SleepItem, WakeItem))
            (control if is_control else normal).append((filename, item))

        cap = settings.grokbot_queue_depth_cap
        if pending > cap:
            self._send("queue.depth", {"depth": pending, "cap": cap})

        woke = False
        for _filename, item in control:
            if isinstance(item, SleepItem):
                self._write("SLEEP", "")
                self.state.sleeping = True
            else:
                self.repo.remove("SLEEP")
                self._wrote = True
                if self.state.sleeping:
                    woke = True
                    self.state.sleeping = False
                    events, self.state.buffered = self.state.buffered, []
                    self._emit(
                        Event(
                            event="wake.summary",
                            payload={"count": len(events), "events": events},
                        )
                    )
            self._mark(item.id, item.kind, "applied")

        # Items wait for the tick after a wake, and for the whole sleep.
        if not self.state.sleeping and not woke:
            for filename, item in normal:
                self._handle(filename, item)

        if self._wrote:
            self.repo.commit_push(f"runner: {len(self._outcomes)} outcomes")
        return self._outcomes

    def _handle(self, filename: str, item) -> None:
        if isinstance(item, PreviewRequestItem):
            self._preview(filename, item)
        elif isinstance(item, _DEFERRED):
            self._defer(item)

    def _defer(self, item) -> None:
        self._mark(item.id, item.kind, "deferred")
        self._send("queue.deferred", {"id": item.id, "kind": item.kind})

    def _preview(self, filename: str, item: PreviewRequestItem) -> None:
        if item.asset_id is None:
            self._defer(item)  # compilation-window previews arrive with phase 2
            return
        try:
            path, duration = self.api.asset_path(item.asset_id)
        except LookupError as e:
            self._reject(filename, item.id, item.kind, str(e))
            return
        except Exception as e:  # transient: leave unmarked, retry next tick
            logger.warning("asset lookup failed for %s: %s", item.id, e)
            return
        if item.end_s > duration:
            self._reject(
                filename,
                item.id,
                item.kind,
                f"end_s {item.end_s} exceeds asset duration {duration}",
            )
            return
        self.previews_dir.mkdir(parents=True, exist_ok=True)
        out = self.previews_dir / f"{item.id}.mp4"
        ok, err = self._trim(path, str(out), item.start_s, item.end_s)
        if not ok:
            self._reject(filename, item.id, item.kind, f"trim failed: {err}")
            return
        file_id = None
        if self.uploader is not None:
            try:
                file_id = self.uploader.upload(out, name=out.name, subfolder="previews")
            except Exception as e:
                logger.warning("preview upload failed for %s: %s", item.id, e)
        self._send(
            "preview.ready",
            {
                "request_id": item.id,
                "drive": {"mp4": file_id},
                "question": item.question,
                "local_path": str(out),
            },
        )
        self._mark(item.id, item.kind, "applied")

    # -- heartbeat -----------------------------------------------------
    def heartbeat_due(self) -> bool:
        return (
            not self._heartbeat_sent
            or self._clock() - self.state.last_heartbeat >= settings.grokbot_heartbeat_seconds
        )

    def heartbeat(self) -> None:
        self.state.last_heartbeat = self._clock()
        self._heartbeat_sent = True
        depth = sum(1 for p, _ in self.repo.list_items() if not self.ledger.seen(Path(p).stem))
        try:
            from ..notify import sinks

            dropped = getattr(getattr(sinks, "_webhook", None), "dropped", 0)
        except Exception:
            dropped = 0
        # Liveness goes out even while sleeping (it is not buffered).
        self._emit(
            Event(
                event="heartbeat",
                payload={
                    "runner_version": self.state.version,
                    "queue_depth": depth,
                    "sleep": self.state.sleeping,
                    "dropped_events": dropped,
                },
            )
        )


def main() -> None:
    repo_dir = settings.grokbot_queue_repo_dir
    if repo_dir is None:
        print("GROKBOT_QUEUE_REPO_DIR is not set", file=sys.stderr)
        sys.exit(1)
    grok_dir = Path(settings.workspace_dir) / "grokbot"
    grok_dir.mkdir(parents=True, exist_ok=True)
    runner = Runner(
        repo=GitQueueRepo(repo_dir),
        api=HttpApiClient("http://localhost:8000"),
        uploader=uploader_from_settings(),
        # Outside the queue repo so `git add -A` never commits it.
        ledger=AppliedLedger(grok_dir / "runner-ledger.db"),
        previews_dir=grok_dir / "previews",
    )
    while True:
        try:
            runner.tick()
            if runner.heartbeat_due():
                runner.heartbeat()
        except Exception:
            logger.exception("runner tick failed")
        time.sleep(settings.grokbot_runner_poll_seconds)


if __name__ == "__main__":
    main()
