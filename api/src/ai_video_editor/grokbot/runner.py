"""Local runner: the only process on the PC that acts on the git queue."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from importlib import metadata
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel, Field

from .. import editing, notify
from ..config import settings
from ..edit_plan import EditPlan, plan_hash
from ..notify import Event
from ..vlm.watch_montage import watch_montage
from .drive import Uploader, uploader_from_settings
from .git_queue import GitQueueRepo, QueueRepo
from .paths import inside as _inside
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
    def plan_hash(self, compilation_id: str) -> str | None: ...
    def plan_complete(self, compilation_id: str) -> bool: ...
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

    def _plan_state(self, compilation_id: str) -> dict:
        r = self._client.get(f"{self._comp_url(compilation_id)}/plan/version")
        self._check(r)
        return r.json()

    def plan_version(self, compilation_id: str) -> str:
        return str(self._plan_state(compilation_id)["version"])

    def plan_hash(self, compilation_id: str) -> str | None:
        return self._plan_state(compilation_id).get("plan_hash")

    def plan_complete(self, compilation_id: str) -> bool:
        return bool(self._plan_state(compilation_id).get("plan_complete"))

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
    # Queue files whose id is not final in the ledger, as of the last tick.
    queue_depth: int = 0
    # Transient-failure attempts per item id (trim timeouts).
    attempts: dict[str, int] = Field(default_factory=dict)
    # Monotonic time of the last retention prune; None = never.
    last_prune: float | None = None


TRIM_TIMEOUT_SECONDS = 120.0
MAX_TRIM_ATTEMPTS = 3
DEFERRED_TTL = timedelta(hours=24)
PRUNE_EVERY_SECONDS = 24 * 3600


def _created_at(created: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(created.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _is_uuid_name(name: str) -> bool:
    try:
        return str(uuid.UUID(name)) == name
    except ValueError:
        return False


_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\"'\r\n]+|/[^\"'\r\n]+")


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
        # Guards `state`: the heartbeat thread reads it while a tick runs.
        self.lock = threading.RLock()
        self._heartbeat_sent = False
        self._wrote = False
        self._outcomes: list[str] = []
        self._prior: dict[str, str] = {}

    # -- emitting ------------------------------------------------------
    def _send(self, event: str, payload: dict) -> None:
        ev = Event(event=event, payload=payload)
        with self.lock:
            if self.state.sleeping:
                self.state.buffered.append(ev.model_dump(exclude={"priority"}))
                return
        self._emit(ev)

    def _write(self, path: str, text: str) -> None:
        self.repo.write(path, text)
        self._wrote = True

    def _mark(self, id: str, kind: str, outcome: str) -> None:
        self.ledger.mark(id, kind, outcome)
        self._outcomes.append(outcome)

    def _reject(self, filename: str, id: str, kind: str, reason: str, **extra) -> None:
        # The ledger mark must land even when the rejected/ write cannot, or one
        # unwritable name would be re-read and re-fail on every tick.
        try:
            self._write(f"rejected/{filename}", json.dumps({"id": id, "reason": reason, **extra}))
        except Exception:
            logger.exception("could not write rejected/%s", filename)
        try:
            self._send("queue.rejected", {"id": id, "reason": reason, **extra})
        finally:
            self._mark(id, kind, "rejected")

    def _reject_internal(self, filename: str, id: str, kind: str, e: Exception) -> None:
        logger.error("item %s failed: %s", id, e, exc_info=e)
        try:
            self._reject(filename, id, kind, f"internal error: {type(e).__name__}")
        except Exception:
            logger.exception("could not record rejection for %s", id)

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
    def _expired(self, item, outcomes: dict[str, str], now: datetime) -> bool:
        """A deferred item older than the TTL is never replayed by a flag flip."""
        if outcomes.get(item.id) != "deferred" or not self._flag_on(item):
            return False
        created = _created_at(item.created)
        return created is None or now - created > DEFERRED_TTL

    def _scan(self, outcomes: dict[str, str]):
        """Parse every queue file once: (control, normal, entries, expired kinds)."""
        control, normal, entries, expired = [], [], [], Counter()
        now = datetime.now(timezone.utc)
        for path, text in self.repo.list_items():
            filename, stem = Path(path).name, Path(path).stem
            if self._done(stem, outcomes):
                entries.append((stem, None))
                continue
            try:
                try:
                    item = parse_item(text)
                except QueueRejected as e:
                    entries.append((stem, None))
                    self._reject(filename, stem, "?", e.reason)
                    continue
                entries.append((item.id, item))
                if self._expired(item, outcomes, now):
                    self._mark(item.id, item.kind, "expired")
                    expired[item.kind] += 1
                    continue
            except Exception as e:  # one bad file never aborts the tick
                self._reject_internal(filename, stem, "?", e)
                continue
            if self._done(item.id, outcomes, item):
                continue
            is_control = isinstance(item, (SleepItem, WakeItem))
            (control if is_control else normal).append((filename, item))
        return control, normal, entries, expired

    def _control(self, filename: str, item) -> bool:
        """Apply one sleep/wake item; True when it woke the runner."""
        woke = False
        if isinstance(item, SleepItem):
            self._write("SLEEP", "")
            with self.lock:
                self.state.sleeping = True
        else:
            self.repo.remove("SLEEP")
            self._wrote = True
            with self.lock:
                if self.state.sleeping:
                    woke = True
                    self.state.sleeping = False
                    events, self.state.buffered = self.state.buffered, []
            if woke:
                self._emit(
                    Event(
                        event="wake.summary",
                        payload={"count": len(events), "events": events},
                        priority=True,
                    )
                )
        self._mark(item.id, item.kind, "applied")
        return woke

    def _depth(self, entries) -> int:
        outcomes = self.ledger.outcomes()
        return sum(1 for id, item in entries if not self._done(id, outcomes, item))

    def tick(self) -> list[str]:
        self._wrote = False
        self._outcomes = []
        self.repo.pull()
        with self.lock:
            self.state.sleeping = parse_sleep_flag(self.repo.root)

        outcomes = self.ledger.outcomes()
        self._prior = outcomes
        control, normal, entries, expired = self._scan(outcomes)
        if expired:
            self._send("queue.expired", {"count": sum(expired.values()), "kinds": dict(expired)})

        pending = len(control) + len(normal)
        cap = settings.grokbot_queue_depth_cap
        if pending > cap:
            self._send("queue.depth", {"depth": pending, "cap": cap})

        woke = False
        for filename, item in control:
            try:
                woke = self._control(filename, item) or woke
            except Exception as e:
                self._reject_internal(filename, item.id, item.kind, e)

        # Items wait for the tick after a wake, and for the whole sleep.
        if not self.state.sleeping and not woke:
            for filename, item in normal:
                try:
                    self._handle(filename, item)
                except Exception as e:
                    self._reject_internal(filename, item.id, item.kind, e)

        with self.lock:
            self.state.queue_depth = self._depth(entries)

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
            self._reject(filename, item.id, item.kind, f"api {e.status}: {_safe_detail(e.detail)}")
        except httpx.HTTPError as e:  # transient: leave unmarked, retry next tick
            logger.warning("plan %s hit a transient API error: %s", item.id, e)

    def _run_plan(self, filename: str, item: EditPlanItem) -> None:
        current = self.api.plan_version(item.compilation_id)
        resumed = False
        if current != item.base_version:
            # A timed-out POST may have been applied server-side: the plan endpoint is
            # idempotent by plan hash, so the same hash means "already applied".
            plan_dict = item.plan.model_dump(mode="json")
            if self.api.plan_hash(item.compilation_id) != plan_hash(
                EditPlan.model_validate(plan_dict)
            ):
                self._reject(
                    filename,
                    item.id,
                    item.kind,
                    f"base_version {item.base_version} != current {current}",
                    current=current,
                )
                return
            # The hash is saved before any step runs, so only a complete run counts as
            # applied; a partial one is re-POSTed and the executor resumes from its snapshot.
            if self.api.plan_complete(item.compilation_id):
                resumed = True
            else:
                self.api.apply_plan(item.compilation_id, plan_dict)
        else:
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
        if resumed:
            payload["resumed"] = True
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
            self._reject(filename, item.id, item.kind, f"api {e.status}: {_safe_detail(e.detail)}")
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
        try:
            ok, err = self._trim(
                path, str(out), item.start_s, item.end_s, timeout_seconds=TRIM_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired:
            tries = self.state.attempts.get(item.id, 0) + 1
            self.state.attempts[item.id] = tries
            if tries >= MAX_TRIM_ATTEMPTS:
                self.state.attempts.pop(item.id, None)
                self._reject(
                    filename, item.id, item.kind, f"trim timed out {MAX_TRIM_ATTEMPTS} times"
                )
            else:  # transient: leave unmarked, retry next tick
                logger.warning("trim for %s timed out (attempt %d)", item.id, tries)
            return
        self.state.attempts.pop(item.id, None)
        if not ok:
            self._reject(filename, item.id, item.kind, f"trim failed: {_safe_detail(err)}")
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
        with self.lock:
            self.state.last_heartbeat = self._clock()
            self._heartbeat_sent = True
            depth = self.state.queue_depth
            sleeping = self.state.sleeping
            version = self.state.version
        try:
            from ..notify import sinks

            dropped = getattr(getattr(sinks, "_webhook", None), "dropped", 0)
        except Exception:
            dropped = 0
        # Liveness goes out even while sleeping (it is not buffered), and is
        # never dropped by the webhook rate cap.
        self._emit(
            Event(
                event="heartbeat",
                payload={
                    "runner_version": version,
                    "queue_depth": depth,
                    "sleep": sleeping,
                    "dropped_events": dropped,
                },
                priority=True,
            )
        )

    # -- retention -----------------------------------------------------
    def maybe_prune(self) -> None:
        """At most once per 24 h: expire old Drive previews and local preview files."""
        now = self._clock()
        with self.lock:
            last = self.state.last_prune
            if last is not None and now - last < PRUNE_EVERY_SECONDS:
                return
            self.state.last_prune = now
        days = settings.grokbot_drive_preview_retention_days
        if self.uploader is not None:
            try:
                n = self.uploader.prune(subfolder="previews", older_than_days=days)
                logger.info("pruned %d Drive previews older than %d days", n, days)
            except Exception as e:
                logger.warning("Drive preview prune failed: %s", e)
        try:
            self._prune_local(days)
        except Exception as e:
            logger.warning("local preview prune failed: %s", e)

    def _prune_local(self, days: int) -> None:
        """Delete only runner-made `<uuid>.mp4` regular files in previews_dir."""
        if not self.previews_dir.is_dir() or self.previews_dir.is_symlink():
            return
        cutoff = time.time() - days * 86400
        for p in self.previews_dir.iterdir():
            if p.suffix != ".mp4" or not _is_uuid_name(p.stem):
                continue
            if p.is_symlink() or not p.is_file():
                continue
            if p.stat().st_mtime < cutoff:
                p.unlink()


def run_once(runner: Runner) -> None:
    """One tick plus the daily prune; a failure is logged, never raised."""
    try:
        runner.tick()
    except Exception:
        logger.exception("runner tick failed")
    try:
        runner.maybe_prune()
    except Exception:
        logger.exception("runner prune failed")


def heartbeat_once(runner: Runner) -> None:
    try:
        if runner.heartbeat_due():
            runner.heartbeat()
    except Exception:
        logger.exception("runner heartbeat failed")


def start_heartbeat(
    runner: Runner, stop: threading.Event, *, interval: float | None = None
) -> threading.Thread:
    """Heartbeat on its own daemon thread, so a long tick never silences it."""
    period = interval if interval is not None else min(30.0, settings.grokbot_heartbeat_seconds)

    def loop() -> None:
        while not stop.is_set():
            heartbeat_once(runner)
            stop.wait(period)

    t = threading.Thread(target=loop, daemon=True, name="grokbot-heartbeat")
    t.start()
    return t


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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logger.info("grokbot runner starting")
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
    start_heartbeat(runner, threading.Event())
    while True:
        run_once(runner)
        time.sleep(settings.grokbot_runner_poll_seconds)


if __name__ == "__main__":
    main()
