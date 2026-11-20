import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_video_editor.config import Settings, settings
from ai_video_editor.grokbot.git_queue import GitQueueRepo
from ai_video_editor.grokbot.queue import AppliedLedger, QueueRejected, parse_item
from ai_video_editor.grokbot.runner import Runner, check_repo_dir, run_once


def env(kind, **kw):
    d = {
        "id": str(uuid.uuid4()),
        "kind": kind,
        "author_bot": "lead",
        "created": "2026-11-20T00:00:00Z",
        "schema_version": 1,
    }
    d.update(kw)
    return d


class FakeRepo:
    def __init__(self, root: Path):
        self.root = root
        self.files: dict[str, str] = {}
        self.pushed: list[str] = []
        self.pulls = 0

    def pull(self):
        self.pulls += 1

    def list_items(self):
        return sorted((p, t) for p, t in self.files.items() if p.startswith("queue/"))

    def write(self, relative_path, text):
        self.files[relative_path] = text
        target = self.root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def remove(self, relative_path):
        self.files.pop(relative_path, None)
        (self.root / relative_path).unlink(missing_ok=True)

    push_ok = True
    retries = 0

    def commit_push(self, message):
        self.pushed.append(message)
        return self.push_ok

    def retry_push(self):
        self.retries += 1
        return self.push_ok

    def add(self, item_dict):
        self.files[f"queue/{item_dict['id']}.json"] = json.dumps(item_dict)


class FakeApi:
    def __init__(self, duration=30.0):
        self.duration = duration
        self.path = str(Path(settings.outplayed_media_dir) / "a.mp4")
        self.calls = []

    def asset_path(self, asset_id):
        self.calls.append(asset_id)
        return (self.path, self.duration)

    def compilation_output(self, cid):
        self.calls.append(cid)
        return None


class FakeUploader:
    def __init__(self):
        self.uploads = []

    def upload(self, path, *, name, subfolder):
        self.uploads.append((name, subfolder))
        return "fid1"


@pytest.fixture
def h(tmp_path):
    repo = FakeRepo(tmp_path / "repo")
    repo.root.mkdir()
    api = FakeApi()
    up = FakeUploader()
    events = []
    trims = []

    def trim(src, out, s, e):
        trims.append((src, out, s, e))
        Path(out).write_bytes(b"")
        return True, None

    clock = SimpleNamespace(t=1000.0)
    runner = Runner(
        repo=repo,
        api=api,
        uploader=up,
        ledger=AppliedLedger(tmp_path / "l.db"),
        emit=lambda ev: events.append(ev),
        trim=trim,
        previews_dir=tmp_path / "prev",
        clock=lambda: clock.t,
    )
    return SimpleNamespace(
        repo=repo,
        api=api,
        up=up,
        events=events,
        trims=trims,
        runner=runner,
        clock=clock,
        tmp=tmp_path,
    )


def preview(**kw):
    return env("preview_request", asset_id="a1", start_s=1.0, end_s=5.0, question="q?", **kw)


def names(h):
    return [e.event for e in h.events]


def test_tick_renders_preview_uploads_and_emits(h):
    it = preview()
    h.repo.add(it)
    assert h.runner.tick() == ["applied"]
    assert h.trims and h.trims[0][2:] == (1.0, 5.0)
    assert h.up.uploads == [(f"{it['id']}.mp4", "previews")]
    ev = next(e for e in h.events if e.event == "preview.ready")
    assert ev.payload["request_id"] == it["id"]
    assert ev.payload["drive"] == {"mp4": "fid1"}
    assert ev.payload["question"] == "q?"
    assert h.runner.ledger.outcomes()[it["id"]] == "applied"
    assert h.repo.pushed == []  # nothing written into the queue repo


def test_tick_skips_applied_items(h):
    h.repo.add(preview())
    h.runner.tick()
    h.events.clear()
    h.trims.clear()
    assert h.runner.tick() == []
    assert not h.trims and not h.events
    assert h.repo.pushed == []


def test_preview_beyond_duration_is_rejected(h):
    it = env("preview_request", asset_id="a1", start_s=20.0, end_s=35.0, question="q")
    h.repo.add(it)
    assert h.runner.tick() == ["rejected"]
    assert not h.trims
    rej = json.loads(h.repo.files[f"rejected/{it['id']}.json"])
    assert rej["id"] == it["id"] and "duration" in rej["reason"]
    assert h.repo.pushed == ["runner: 1 outcomes"]
    assert "queue.rejected" in names(h)
    assert h.runner.ledger.outcomes()[it["id"]] == "rejected"


def test_malformed_item_is_rejected_with_reason(h):
    h.repo.files["queue/bad.json"] = "{not json"
    assert h.runner.tick() == ["rejected"]
    rej = json.loads(h.repo.files["rejected/bad.json"])
    assert rej["reason"].startswith("invalid JSON")
    assert "queue.rejected" in names(h)
    h.events.clear()
    assert h.runner.tick() == []


def test_sleep_buffers_and_wake_summarizes(h):
    s = env("sleep")
    p = preview()
    h.repo.add(s)
    h.repo.add(p)
    out = h.runner.tick()
    assert (h.repo.root / "SLEEP").is_file()
    assert not h.trims
    assert "preview.ready" not in names(h)
    assert h.runner.ledger.outcomes().get(p["id"]) is None
    assert out == ["applied"]
    h.repo.add(env("wake"))
    h.runner.tick()
    assert not (h.repo.root / "SLEEP").exists()
    summaries = [e for e in h.events if e.event == "wake.summary"]
    assert len(summaries) == 1
    assert h.runner.state.buffered == []
    h.runner.tick()
    assert h.trims
    assert "preview.ready" in names(h)


def test_buffered_emits_reach_wake_summary(h):
    h.repo.add(env("sleep"))
    h.runner.tick()
    h.repo.files["queue/bad.json"] = "nope"
    h.runner.tick()
    assert "queue.rejected" not in names(h)
    h.repo.add(env("wake"))
    h.runner.tick()
    s = next(e for e in h.events if e.event == "wake.summary")
    assert [b["event"] for b in s.payload["events"]] == ["queue.rejected"]


def test_deferred_kinds_are_ledgered_not_executed(h):
    items = [
        env("edit_plan", compilation_id="c", base_version="v", plan={"format": "short"}),
        env("verdict", game="lol", clip_ref="c", verdict="payoff", rule="r"),
        env("deploy", tag="v1.2.3"),
    ]
    for i in items:
        h.repo.add(i)
    assert h.runner.tick() == ["deferred"] * 3
    oc = h.runner.ledger.outcomes()
    assert all(oc[i["id"]] == "deferred" for i in items)
    assert h.api.calls == []
    assert names(h).count("queue.deferred") == 3


def test_heartbeat_payload_and_cadence(h):
    assert h.runner.heartbeat_due()
    h.runner.heartbeat()
    p = h.events[-1].payload
    assert h.events[-1].event == "heartbeat"
    assert set(p) == {"runner_version", "queue_depth", "sleep", "dropped_events"}
    assert p["sleep"] is False and p["dropped_events"] == 0
    assert not h.runner.heartbeat_due()
    h.clock.t += 301
    assert h.runner.heartbeat_due()


def test_queue_depth_cap_emits_once(h):
    for _ in range(12):
        h.repo.add(env("deploy", tag="v1.0.0"))
    h.runner.tick()
    assert names(h).count("queue.depth") == 1


def test_git_queue_repo_uses_argv_lists(tmp_path):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        rc = 1 if "push" in argv else 0
        return SimpleNamespace(returncode=rc, stdout="", stderr="boom")

    (tmp_path / "queue").mkdir()
    (tmp_path / "queue" / "a.json").write_text("{}")
    repo = GitQueueRepo(tmp_path, run=run)
    repo.pull()
    assert repo.list_items() == [("queue/a.json", "{}")]
    repo.write("rejected/a.json", "{}")
    repo.commit_push("msg")
    assert all(isinstance(c, list) for c in calls)
    assert calls[0] == ["git", "-C", str(tmp_path), "pull", "--ff-only"]
    assert any(c[3:5] == ["commit", "-m"] and c[5] == "msg" for c in calls)
    assert calls[-1][3] == "push"


def test_blank_repo_dir_is_none():
    assert Settings(grokbot_queue_repo_dir="  ").grokbot_queue_repo_dir is None
    assert Settings(grokbot_queue_repo_dir="").grokbot_queue_repo_dir is None


def test_main_refuses_non_repo(tmp_path):
    assert "not set" in check_repo_dir(None)
    assert ".git" in check_repo_dir(tmp_path)
    (tmp_path / ".git").mkdir()
    assert "queue" in check_repo_dir(tmp_path)
    (tmp_path / "queue").mkdir()
    assert check_repo_dir(tmp_path) is None


def test_poison_item_is_isolated(h):
    bad = preview()
    good = env("deploy", tag="v1.0.0")
    h.repo.add(bad)
    h.repo.add(good)

    def boom(*a):
        raise RuntimeError("x")

    h.api.asset_path = boom
    h.runner._trim = boom  # asset_path boom is treated as transient; force a real raise
    out = h.runner.tick()
    assert "deferred" in out
    assert good["id"] in h.runner.ledger.outcomes()


def test_unexpected_handler_error_is_rejected(h):
    bad = preview()
    h.repo.add(bad)

    def boom(*a):
        raise RuntimeError("x")

    h.runner._trim = boom
    assert h.runner.tick() == ["rejected"]
    rej = json.loads(h.repo.files[f"rejected/{bad['id']}.json"])
    assert rej["reason"] == "internal error: RuntimeError"
    assert "queue.rejected" in names(h)


def test_heartbeat_survives_failing_tick(h):
    def boom():
        raise RuntimeError("x")

    h.repo.pull = boom
    run_once(h.runner)
    assert "heartbeat" in names(h)


def test_failed_push_is_retried(h):
    h.repo.push_ok = False
    h.repo.files["queue/bad.json"] = "nope"
    h.runner.tick()
    assert h.runner.state.pending_push
    h.runner.tick()
    assert h.repo.retries == 1
    h.repo.push_ok = True
    h.runner.tick()
    assert not h.runner.state.pending_push


def test_pull_failure_resets_only_without_local_commits(tmp_path):
    def make(status):
        calls = []

        def run(argv, **kw):
            calls.append(argv[3:])
            rc = 1 if argv[3] == "pull" else 0
            out = status if argv[3] == "status" else "main\n"
            return SimpleNamespace(returncode=rc, stdout=out, stderr="")

        return calls, GitQueueRepo(tmp_path, run=run)

    calls, repo = make("## main...origin/main [behind 2]\n")
    repo.pull()
    assert ["reset", "--hard", "origin/main"] in calls
    calls, repo = make("## main...origin/main [ahead 1, behind 2]\n")
    repo.pull()
    assert not any(c[0] in ("reset", "fetch") for c in calls)


def test_item_id_must_be_uuid():
    base = env("sleep")
    base["id"] = "C:evil"
    with pytest.raises(QueueRejected):
        parse_item(json.dumps(base))
    base["id"] = str(uuid.uuid4()).upper()
    assert parse_item(json.dumps(base)).id == base["id"].lower()


def test_asset_path_outside_roots_rejected(h):
    h.api.path = "C:/Windows/evil.mp4"
    it = preview()
    h.repo.add(it)
    assert h.runner.tick() == ["rejected"]
    assert not h.trims
    reason = json.loads(h.repo.files[f"rejected/{it['id']}.json"])["reason"]
    assert "outside allowed roots" in reason
