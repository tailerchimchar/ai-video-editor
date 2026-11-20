"""Local runner: the only process on the PC that acts on the git queue."""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import urllib.parse
from collections.abc import Callable
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel

from .. import editing, notify
from ..config import settings
from ..notify import Event
from ..vlm.watch_montage import watch_montage
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


class ApiError(Exception):
    """A 4xx answer from the API; `detail` is its parsed body."""

    def __init__(self, status: int, detail):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class ApiClient(Protocol):
    def asset_path(self, asset_id: str) -> tuple[str, float]: ...
    def compilation_output(self, compilation_id: str) -> str | None: ...
    def plan_version(self, compilation_id: str) -> str: ...
    def apply_plan(self, compilation_id: str, plan: dict) -> dict: ...
    def record_correction(self, body: dict) -> None: ...
    def recent_corrections(self, game: str) -> list[dict]: ...


class HttpApiClient:
    def __init__(self, base_url: str, client: httpx.Client | None = None):
        self._base = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=30.0)

    def asset_path(self, asset_id: str) -> tuple[str, float]:
        r = self._client.get(f"{self._base}/api/v1/assets/{urllib.parse.quote(asset_id, safe='')}")
        if r.status_code == 404:
            raise LookupError(f"asset {asset_id} not found")
        r.raise_for_status()
        body = r.json()
        dur = body.get("duration_seconds")
        if dur is None:
            raise LookupError(f"asset {asset_id} has no known duration")
        return body["path"], float(dur)

    def compilation_output(self, compilation_id: str) -> str | None:
        r = self._client.get(
            f"{self._base}/api/v1/edit/compile/{urllib.parse.quote(compilation_id, safe='')}"
        )
        if not r.is_success:
            return None
        return r.json().get("output_path")

    def _comp_url(self, compilation_id: str) -> str:
        return f"{self._base}/api/v1/compilations/{urllib.parse.quote(compilation_id, safe='')}"

    def plan_version(self, compilation_id: str) -> str:
        r = self._client.get(f"{self._comp_url(compilation_id)}/plan/version")
        self._check(r)
        return str(r.json()["version"])

    def apply_plan(self, compilation_id: str, plan: dict) -> dict:
        # Rendering can take minutes; the default 30s timeout is far too short.
        r = self._client.post(f"{self._comp_url(compilation_id)}/plan", json=plan, timeout=900.0)
        self._check(r)
        return r.json()

    def record_correction(self, body: dict) -> None:
        r = self._client.post(f"{self._base}/api/v1/mentor/record_correction", json=body)
        self._check(r)

    def recent_corrections(self, game: str) -> list[dict]:
        r = self._client.get(
            f"{self._base}/api/v1/mentor/recent_corrections",
            params={"game": game, "limit": 100},
        )
        self._check(r)
        return r.json().get("corrections", [])

    @staticmethod
    def _check(r: httpx.Response) -> None:
        """4xx is permanent (ApiError); 5xx raises HTTPStatusError (transient)."""
        if 400 <= r.status_code < 500:
            try:
                body = r.json()
                detail = body.get("detail", body) if isinstance(body, dict) else body
            except ValueError:
                detail = r.text
            raise ApiError(r.status_code, detail)
        r.raise_for_status()


class RunnerState(BaseModel):
    sleeping: bool = False
    buffered: list[dict] = []
    last_heartbeat: float = 0.0
    pending_push: bool = False
    version: str = _version()


def _inside(path: str, *roots: Path) -> bool:
    try:
        p = Path(path).resolve()
    except (OSError, ValueError):
        return False
    return any(p.is_relative_to(Path(r).resolve()) for r in roots)


_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"']+|/[^\s\"']+")


def _safe_detail(detail, limit: int = 500) -> str:
    """API detail for a rejection reason: no absolute paths, bounded length."""
    return _PATH_RE.sub("<path>", str(detail))[:limit]


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
        watch=watch_montage,
        previews_dir: Path,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.repo = repo
        self.api = api
        self.uploader = uploader
        self.ledger = ledger
        self._emit = emit
        self._trim = trim
        self._watch = watch
        self.previews_dir = Path(previews_dir)
        self._clock = clock
        self.state = RunnerState()
        self._heartbeat_sent = False
        self._wrote = False
        self._outcomes: list[str] = []
        self._prior: dict[str, str] = {}

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

    def _reject(self, filename: str, id: str, kind: str, reason: str, **extra) -> None:
        self._write(f"rejected/{filename}", json.dumps({"id": id, "reason": reason, **extra}))
        self._send("queue.rejected", {"id": id, "reason": reason, **extra})
        self._mark(id, kind, "rejected")

    def _flag_on(self, item) -> bool:
        if isinstance(item, EditPlanItem):
            return bool(settings.grokbot_execute_plans)
        if isinstance(item, VerdictItem):
            return bool(settings.grokbot_record_verdicts)
        return False

    def _done(self, id: str, outcomes: dict[str, str], item=None) -> bool:
        """Seen and final. A "deferred" item is not final once its flag is on."""
        outcome = outcomes.get(id)
        if outcome is None:
            return False
        if outcome == "applying":
            return False
        if outcome != "deferred":
            return True
        if item is None:  # kind unknown yet: let it through if any flag is on
            return not (settings.grokbot_execute_plans or settings.grokbot_record_verdicts)
        return not self._flag_on(item)

    # -- tick ----------------------------------------------------------
    def tick(self) -> list[str]:
        self._wrote = False
        self._outcomes = []
        self.repo.pull()
        self.state.sleeping = parse_sleep_flag(self.repo.root)

        control, normal, pending = [], [], 0
        outcomes = self.ledger.outcomes()
        self._prior = outcomes
        for path, text in self.repo.list_items():
            filename = Path(path).name
            if self._done(Path(path).stem, outcomes):
                continue
            try:
                item = parse_item(text)
            except QueueRejected as e:
                self._reject(filename, Path(path).stem, "?", e.reason)
                continue
            if self._done(item.id, outcomes, item):
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
                try:
                    self._handle(filename, item)
                except Exception as e:
                    logger.exception("item %s failed", item.id)
                    try:
                        self._reject(
                            filename, item.id, item.kind, f"internal error: {type(e).__name__}"
                        )
                    except Exception:
                        logger.exception("could not record rejection for %s", item.id)

        if self._wrote:
            self.state.pending_push = not self.repo.commit_push(
                f"runner: {len(self._outcomes)} outcomes"
            )
        elif self.state.pending_push:
            self.state.pending_push = not self.repo.retry_push()
        return self._outcomes

    def _handle(self, filename: str, item) -> None:
        if isinstance(item, PreviewRequestItem):
            self._preview(filename, item)
        elif isinstance(item, EditPlanItem) and settings.grokbot_execute_plans:
            self._edit_plan(filename, item)
        elif isinstance(item, VerdictItem) and settings.grokbot_record_verdicts:
            self._verdict(filename, item)
        elif isinstance(item, _DEFERRED):
            self._defer(item)

    def _upload(self, path: Path, name: str, subfolder: str) -> str | None:
        if self.uploader is None:
            return None
        try:
            return self.uploader.upload(path, name=name, subfolder=subfolder)
        except Exception as e:
            logger.warning("upload of %s failed: %s", name, e)
            return None

    def _edit_plan(self, filename: str, item: EditPlanItem) -> None:
        try:
            self._run_plan(filename, item)
        except ApiError as e:
            self._reject(
                filename, item.id, item.kind, f"api {e.status}: {_safe_detail(e.detail)}"
            )
        except httpx.HTTPError as e:  # transient: leave unmarked, retry next tick
            logger.warning("plan %s hit a transient API error: %s", item.id, e)

    def _run_plan(self, filename: str, item: EditPlanItem) -> None:
        current = self.api.plan_version(item.compilation_id)
        if current != item.base_version:
            self._reject(
                filename,
                item.id,
                item.kind,
                f"base_version {item.base_version} != current {current}",
                current=current,
            )
            return
        self.api.apply_plan(item.compilation_id, item.plan.model_dump(mode="json"))
        output = self.api.compilation_output(item.compilation_id)
        if not output or not Path(output).is_file():
            self._reject(filename, item.id, item.kind, "plan applied but no rendered output found")
            return
        result = self._watch(output)
        c = result.critique
        mp4 = self._upload(Path(output), f"{item.id}.mp4", "renders")
        crit = None
        if c is not None:
            critique_path = Path(output).parent / "critique.json"
            critique_path.write_text(c.model_dump_json(indent=2), encoding="utf-8")
            crit = self._upload(critique_path, f"{item.id}-critique.json", "digests")
        payload = {
            "plan_id": item.id,
            "drive": {"mp4": mp4, "critique": crit},
            "critic": (
                {
                    "verdict": c.overall_verdict,
                    "opener": c.opener_strength,
                    "climax": c.climax_strength,
                    "pacing": c.pacing,
                }
                if c is not None
                else None
            ),
            "copy": item.copy.model_dump(mode="json") if item.copy else None,
        }
        if c is None:
            payload["critic_error"] = _safe_detail(result.error or "critic returned nothing")
        self._send("render.done", payload)
        # The plan is applied; a resend would be stale, so never leave it unmarked.
        self._mark(item.id, item.kind, "applied")

    def _log_decision(self, item: VerdictItem, reason: str) -> None:
        self.previews_dir.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "id": item.id,
            "game": item.game,
            "clip_ref": item.clip_ref,
            "verdict": item.verdict,
            "rule": item.rule,
            "reason": reason,
            "logged_at": datetime.now(timezone.utc).isoformat(),
        }
        with (self.previews_dir.parent / "decisions.log").open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
        self._mark(item.id, item.kind, "logged")

    def _verdict(self, filename: str, item: VerdictItem) -> None:
        # Only a verdict that quotes Noodlz may reach the mentor corrections.
        if not item.has_quote:
            self._log_decision(item, "no quote")
            return
        if len(item.quote.strip()) < 3 or len(item.rule.strip()) < 3:
            self._log_decision(item, "quote or rule shorter than 3 characters")
            return
        source = f"grokbot:{item.id}"
        try:
            if self._prior.get(item.id) == "applying":
                # A crash or 5xx may have left the post half-done: check before re-posting.
                recorded = self.api.recent_corrections(item.game)
                if any(c.get("source_context") == source for c in recorded):
                    self._mark(item.id, item.kind, "applied")
                    return
            self.ledger.mark(item.id, item.kind, "applying")
            self.api.record_correction(
                {
                    "game": item.game,
                    "feedback": item.quote,
                    "rule": item.rule,
                    "clip_ref": item.clip_ref,
                    "source_context": source,
                    "tags": ["grokbot"],
                    "verdict": item.verdict,
                }
            )
        except ApiError as e:
            self._reject(
                filename, item.id, item.kind, f"api {e.status}: {_safe_detail(e.detail)}"
            )
            return
        except httpx.HTTPError as e:  # transient: stays "applying", re-checked next tick
            logger.warning("verdict %s hit a transient API error: %s", item.id, e)
            return
        self._mark(item.id, item.kind, "applied")

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
        if not _inside(path, settings.outplayed_media_dir, settings.workspace_dir):
            self._reject(filename, item.id, item.kind, "asset path outside allowed roots")
            return
        out = self.previews_dir / f"{item.id}.mp4"
        if out.resolve().parent != self.previews_dir.resolve():
            self._reject(filename, item.id, item.kind, "preview path escapes previews dir")
            return
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


def run_once(runner: Runner) -> None:
    """One loop body. Tick and heartbeat fail independently."""
    try:
        runner.tick()
    except Exception:
        logger.exception("runner tick failed")
    try:
        if runner.heartbeat_due():
            runner.heartbeat()
    except Exception:
        logger.exception("runner heartbeat failed")


def check_repo_dir(repo_dir: Path | None) -> str | None:
    """Return a refusal reason, or None when the dir is a usable queue repo."""
    if repo_dir is None:
        return "GROKBOT_QUEUE_REPO_DIR is not set"
    root = Path(repo_dir)
    if not (root / ".git").exists():
        return f"{root} is not a git repo (no .git)"
    if not (root / "queue").is_dir():
        return f"{root} has no queue/ directory"
    return None


def main() -> None:
    repo_dir = settings.grokbot_queue_repo_dir
    problem = check_repo_dir(repo_dir)
    if problem:
        print(problem, file=sys.stderr)
        sys.exit(1)
    grok_dir = Path(settings.workspace_dir) / "grokbot"
    grok_dir.mkdir(parents=True, exist_ok=True)
    runner = Runner(
        repo=GitQueueRepo(repo_dir),
        api=HttpApiClient(settings.grokbot_api_base_url),
        uploader=uploader_from_settings(),
        # Outside the queue repo so it is never committed.
        ledger=AppliedLedger(grok_dir / "runner-ledger.db"),
        previews_dir=grok_dir / "previews",
    )
    while True:
        run_once(runner)
        time.sleep(settings.grokbot_runner_poll_seconds)


if __name__ == "__main__":
    main()
