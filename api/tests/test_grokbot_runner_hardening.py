"""Runner hardening: poison items, symlinks, liveness, expiry, retention."""

import json
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from conftest import FakeRepo, FakeUploader, env

from ai_video_editor import editing
from ai_video_editor.config import settings
from ai_video_editor.grokbot.git_queue import GitQueueRepo, UnsafePathError
from ai_video_editor.grokbot.queue import AppliedLedger, parse_sleep_flag
from ai_video_editor.grokbot.runner import Runner, start_heartbeat
from ai_video_editor.notify.models import Event
from ai_video_editor.notify.sinks.webhook import WebhookSink


class Api:
    def __init__(self):
        self.path = str(Path(settings.outplayed_media_dir) / "a.mp4")

    def asset_path(self, asset_id):
        return (self.path, 30.0)


def make_runner(tmp_path, repo=None, **kw):
    repo = repo or FakeRepo(tmp_path / "repo")
    repo.root.mkdir(exist_ok=True)
    events = []
    clock = SimpleNamespace(t=1000.0)
    defaults = dict(
        repo=repo,
        api=Api(),
        uploader=FakeUploader(),
        ledger=AppliedLedger(tmp_path / "l.db"),
        emit=events.append,
        trim=lambda src, out, s, e, **k: (Path(out).write_bytes(b""), (True, None))[1],
        previews_dir=tmp_path / "prev",
        clock=lambda: clock.t,
    )
    defaults.update(kw)
    runner = Runner(**defaults)
    return SimpleNamespace(runner=runner, repo=repo, events=events, clock=clock)


def preview(**kw):
    return env("preview_request", asset_id="a1", start_s=1.0, end_s=5.0, question="q?", **kw)


def names(h):
    return [e.event for e in h.events]


def failing_rejected_writes(repo):
    real = repo.write

    def write(path, text):
        if path.startswith("rejected/") or path == "SLEEP":
            raise OSError("cannot write")
        real(path, text)

    repo.write = write


# ----- I4: poison items never wedge the tick -----


def test_parse_reject_with_failing_write_still_ledgers_and_continues(tmp_path):
    h = make_runner(tmp_path)
    failing_rejected_writes(h.repo)
    h.repo.files["queue/bad.json"] = "{not json"
    good = preview()
    h.repo.add(good)
    out = h.runner.tick()
    assert sorted(out) == ["applied", "rejected"]
    oc = h.runner.ledger.outcomes()
    assert oc["bad"] == "rejected" and oc[good["id"]] == "applied"
    assert "queue.rejected" in names(h)
    h.events.clear()
    assert h.runner.tick() == []  # never re-read


def test_failing_sleep_write_is_isolated(tmp_path):
    h = make_runner(tmp_path)
    failing_rejected_writes(h.repo)
    s = env("sleep")
    h.repo.add(s)
    out = h.runner.tick()
    assert out == ["rejected"]
    assert h.runner.ledger.outcomes()[s["id"]] == "rejected"
    assert h.runner.tick() == []


def test_failing_wake_remove_is_isolated(tmp_path):
    h = make_runner(tmp_path)

    def remove(path):
        raise OSError("nope")

    h.repo.remove = remove
    w = env("wake")
    good = preview()
    h.repo.add(w)
    h.repo.add(good)
    h.runner.tick()
    oc = h.runner.ledger.outcomes()
    assert oc[w["id"]] == "rejected"
    assert oc[good["id"]] == "applied"


# ----- I5: symlinks in the queue repo are refused -----


def _symlink_or_skip(link: Path, target: Path, *, is_dir=False):
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"symlinks not available here: {e}")


def _git_repo(tmp_path):
    root = tmp_path / "q"
    (root / "queue").mkdir(parents=True)
    run = lambda argv, **kw: SimpleNamespace(returncode=0, stdout="", stderr="")  # noqa: E731
    return root, GitQueueRepo(root, run=run)


def test_symlinked_queue_item_is_skipped(tmp_path):
    root, repo = _git_repo(tmp_path)
    secret = tmp_path / "transcript.json"
    secret.write_text('{"text": "private"}')
    (root / "queue" / "ok.json").write_text("{}")
    _symlink_or_skip(root / "queue" / "x.json", secret)
    assert repo.list_items() == [("queue/ok.json", "{}")]


def test_symlinked_sleep_is_never_written_or_trusted(tmp_path):
    root, repo = _git_repo(tmp_path)
    victim = tmp_path / "runner-ledger.db"
    victim.write_text("precious")
    _symlink_or_skip(root / "SLEEP", victim)
    with pytest.raises(UnsafePathError):
        repo.write("SLEEP", "")
    with pytest.raises(UnsafePathError):
        repo.remove("SLEEP")
    assert victim.read_text() == "precious"
    assert parse_sleep_flag(root) is False


def test_symlinked_rejected_parent_is_refused(tmp_path):
    root, repo = _git_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink_or_skip(root / "rejected", outside, is_dir=True)
    with pytest.raises(UnsafePathError):
        repo.write("rejected/a.json", "{}")
    assert list(outside.iterdir()) == []


def test_symlink_walk_without_os_symlinks(tmp_path, monkeypatch):
    """Same refusals where the platform cannot create links: fake `is_symlink`."""
    root, repo = _git_repo(tmp_path)
    (root / "rejected").mkdir()
    (root / "queue" / "x.json").write_text("{}")
    linked = {root / "rejected", root / "queue" / "x.json", root / "SLEEP"}
    real = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self in linked or real(self))
    with pytest.raises(UnsafePathError):
        repo.write("rejected/a.json", "{}")
    with pytest.raises(UnsafePathError):
        repo.write("SLEEP", "")
    assert repo.list_items() == []
    assert not (root / "rejected" / "a.json").exists()


def test_write_refuses_escaping_paths(tmp_path):
    _root, repo = _git_repo(tmp_path)
    for bad in ("../x.json", "rejected/../../x.json"):
        with pytest.raises(UnsafePathError):
            repo.write(bad, "{}")


def test_oversized_queue_file_read_is_capped(tmp_path):
    root, repo = _git_repo(tmp_path)
    (root / "queue" / "big.json").write_text("x" * 500_000)
    [(_, text)] = repo.list_items()
    assert len(text) == 64 * 1024 + 1


# ----- I6: heartbeat liveness -----


def test_heartbeat_thread_runs_while_tick_is_blocked(tmp_path):
    h = make_runner(tmp_path)
    release = threading.Event()
    h.repo.pull = lambda: release.wait(5)
    ticker = threading.Thread(target=h.runner.tick, daemon=True)
    ticker.start()
    stop = threading.Event()
    h.clock.t += 301  # a heartbeat is due (never at startup)
    start_heartbeat(h.runner, stop, interval=0.01)
    deadline = time.monotonic() + 2
    while "heartbeat" not in names(h) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ticker.is_alive()  # the tick is still stuck in pull
    assert "heartbeat" in names(h)
    stop.set()
    release.set()
    ticker.join(5)


def test_heartbeat_is_priority(tmp_path):
    h = make_runner(tmp_path)
    h.runner.heartbeat()
    assert h.events[-1].priority is True


def test_webhook_cap_never_drops_priority_events():
    posted = []
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: posted.append(r) or httpx.Response(200))
    )
    sink = WebhookSink("https://x", "k", max_per_hour=2, client=client)
    for _ in range(5):
        sink.send(Event(event="queue.rejected"))
    sink.send(Event(event="heartbeat", priority=True))
    sink.flush()
    assert sink.dropped == 3
    bodies = [json.loads(r.content) for r in posted]
    assert [b["event"] for b in bodies] == ["queue.rejected", "queue.rejected", "heartbeat"]
    assert all("priority" not in b for b in bodies)


def test_trim_clip_passes_timeout(monkeypatch, tmp_path):
    seen = {}

    def run(cmd, **kw):
        seen.update(kw)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(editing.subprocess, "run", run)
    editing.trim_clip("a.mp4", str(tmp_path / "o.mp4"), 0.0, 1.0, timeout_seconds=120)
    assert seen["timeout"] == 120
    editing.trim_clip("a.mp4", str(tmp_path / "o.mp4"), 0.0, 1.0)
    assert seen["timeout"] is None


def test_trim_timeout_is_transient_then_rejected_after_three(tmp_path):
    calls = []

    def trim(src, out, s, e, *, timeout_seconds=None):
        calls.append(timeout_seconds)
        raise subprocess.TimeoutExpired("ffmpeg", timeout_seconds)

    h = make_runner(tmp_path, trim=trim)
    it = preview()
    h.repo.add(it)
    assert h.runner.tick() == []
    assert h.runner.state.attempts[it["id"]] == 1
    assert h.runner.tick() == []
    assert h.runner.tick() == ["rejected"]
    assert calls == [120.0, 120.0, 120.0]
    rej = json.loads(h.repo.files[f"rejected/{it['id']}.json"])
    assert rej["reason"] == "trim timed out 3 times"
    assert it["id"] not in h.runner.state.attempts


def test_trim_failure_reason_is_scrubbed(tmp_path):
    err = "C:/Users/someone/Videos/match.mp4: Invalid data " + "x" * 3000
    h = make_runner(tmp_path, trim=lambda *a, **k: (False, err))
    it = preview()
    h.repo.add(it)
    h.runner.tick()
    reason = json.loads(h.repo.files[f"rejected/{it['id']}.json"])["reason"]
    assert "someone" not in reason and len(reason) < 600


# ----- smoke (b): heartbeat depth counts only non-final ids -----


def test_heartbeat_depth_is_zero_after_only_item_applied(tmp_path):
    h = make_runner(tmp_path)
    it = preview()
    h.repo.files["queue/not-the-id.json"] = json.dumps(it)  # stem != id
    assert h.runner.tick() == ["applied"]
    h.runner.heartbeat()
    assert h.events[-1].payload["queue_depth"] == 0


def test_heartbeat_depth_counts_waiting_items(tmp_path):
    h = make_runner(tmp_path)
    h.repo.add(env("sleep"))
    h.repo.add(preview())
    h.repo.add(preview())
    h.runner.tick()
    h.runner.heartbeat()
    assert h.events[-1].payload["queue_depth"] == 2


# ----- I7: stale deferred items expire instead of replaying -----


def _old(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def verdict(created):
    return env(
        "verdict",
        game="league",
        clip_ref="1",
        verdict="payoff",
        rule="keep it",
        quote="nice one",
        created=created,
    )


class MentorApi(Api):
    def __init__(self):
        super().__init__()
        self.recorded = []

    def record_correction(self, body):
        self.recorded.append(body)

    def recent_corrections(self, game):
        return []


def test_old_deferred_items_expire_with_one_event(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "grokbot_record_verdicts", False)
    api = MentorApi()
    h = make_runner(tmp_path, api=api)
    old1, old2, young = verdict(_old(30)), verdict(_old(48)), verdict(_old(1))
    for it in (old1, old2, young):
        h.repo.add(it)
    assert h.runner.tick() == ["deferred"] * 3
    monkeypatch.setattr(settings, "grokbot_record_verdicts", True)
    out = h.runner.tick()
    assert sorted(out) == ["applied", "expired", "expired"]
    oc = h.runner.ledger.outcomes()
    assert oc[old1["id"]] == oc[old2["id"]] == "expired"
    assert oc[young["id"]] == "applied"
    assert [b["source_context"] for b in api.recorded] == [f"grokbot:{young['id']}"]
    expired = [e for e in h.events if e.event == "queue.expired"]
    assert len(expired) == 1
    assert expired[0].payload == {"count": 2, "kinds": {"verdict": 2}}
    h.events.clear()
    assert h.runner.tick() == []
    assert "queue.expired" not in names(h)


def test_deferred_items_stay_deferred_while_flags_off(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "grokbot_record_verdicts", False)
    h = make_runner(tmp_path, api=MentorApi())
    it = verdict(_old(72))
    h.repo.add(it)
    h.runner.tick()
    assert h.runner.tick() == []
    assert h.runner.ledger.outcomes()[it["id"]] == "deferred"


def test_new_old_item_is_processed_not_expired(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "grokbot_record_verdicts", True)
    h = make_runner(tmp_path, api=MentorApi())
    it = verdict(_old(72))
    h.repo.add(it)
    assert h.runner.tick() == ["applied"]


# ----- I10: retention prune -----


def test_prune_runs_at_most_once_a_day(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "grokbot_drive_preview_retention_days", 7)
    h = make_runner(tmp_path)
    h.runner.maybe_prune()
    h.runner.maybe_prune()
    assert h.runner.uploader.prunes == [("previews", 7)]
    h.clock.t += 24 * 3600 + 1
    h.runner.maybe_prune()
    assert len(h.runner.uploader.prunes) == 2


def test_prune_deletes_only_old_runner_previews(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "grokbot_drive_preview_retention_days", 7)
    h = make_runner(tmp_path)
    prev = h.runner.previews_dir
    prev.mkdir(parents=True)
    old_id, new_id = prev / f"{uuid.uuid4()}.mp4", prev / f"{uuid.uuid4()}.mp4"
    other = prev / "keep-me.mp4"
    for p in (old_id, new_id, other):
        p.write_bytes(b"")
    ten_days = time.time() - 10 * 86400
    os.utime(old_id, (ten_days, ten_days))
    os.utime(other, (ten_days, ten_days))
    h.runner.maybe_prune()
    assert not old_id.exists()
    assert new_id.exists() and other.exists()


def test_prune_failures_are_contained(tmp_path):
    class Boom(FakeUploader):
        def prune(self, **kw):
            raise RuntimeError("drive down")

    h = make_runner(tmp_path, uploader=Boom())
    h.runner.maybe_prune()  # never raises
    assert h.runner.state.last_prune is not None
