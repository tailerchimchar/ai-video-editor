import json
from types import SimpleNamespace

import pytest
from test_grokbot_runner import FakeRepo, FakeUploader, env

from ai_video_editor.config import settings
from ai_video_editor.grokbot.queue import AppliedLedger
from ai_video_editor.grokbot.runner import ApiError, Runner
from ai_video_editor.vlm.watch_montage import MontageCritique, WatchResult


def plan_item(**kw):
    d = {
        "compilation_id": "c1",
        "base_version": "3",
        "plan": {
            "format": "hype_montage",
            "source_clips": [{"clip_ref": "1", "start": 0.0, "end": 10.0, "intensity": 0.5}],
            "order": ["1"],
        },
        "copy": {"title": "T", "caption": "C", "hashtags": ["#a"]},
    }
    d.update(kw)
    return env("edit_plan", **d)


def verdict_item(**kw):
    d = {"game": "league", "clip_ref": "12m03s", "verdict": "payoff", "rule": "keep the kill beat"}
    d.update(kw)
    return env("verdict", **d)


class P2Api:
    def __init__(self, tmp):
        self.version = "3"
        self.apply_error = None
        self.applied = []
        self.recorded = []
        self.record_error = None
        self.hash = None
        self.version_error = None
        self.out = tmp / "out" / "compilation.mp4"
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.out.write_bytes(b"x")

    def asset_path(self, asset_id):
        raise AssertionError

    def compilation_output(self, cid):
        return str(self.out)

    def plan_version(self, cid):
        if self.version_error:
            raise self.version_error
        return self.version

    def plan_hash(self, cid):
        return self.hash

    def apply_plan(self, cid, plan):
        if self.apply_error:
            raise self.apply_error
        self.applied.append((cid, plan))
        return {"progress": {}, "problems": []}

    def record_correction(self, body):
        if self.record_error:
            raise self.record_error
        self.recorded.append(body)

    def recent_corrections(self, game):
        return list(self.recorded)


def fake_watch(path):
    crit = MontageCritique(
        overall_verdict="ship_it",
        opener_strength=4,
        climax_strength=5,
        pacing="just_right",
        top_recommendation="none",
        flags=[],
    )
    return WatchResult(critique=crit, panel_count=1, total_seconds=10.0)


@pytest.fixture
def h(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "grokbot_execute_plans", True, raising=False)
    monkeypatch.setattr(settings, "grokbot_record_verdicts", True, raising=False)
    repo = FakeRepo(tmp_path / "repo")
    repo.root.mkdir()
    api = P2Api(tmp_path)
    up = FakeUploader()
    events = []
    runner = Runner(
        repo=repo,
        api=api,
        uploader=up,
        ledger=AppliedLedger(tmp_path / "l.db"),
        emit=events.append,
        trim=lambda *a: (True, None),
        previews_dir=tmp_path / "grok" / "previews",
        watch=fake_watch,
    )
    return SimpleNamespace(repo=repo, api=api, up=up, events=events, runner=runner, tmp=tmp_path)


def rejected(h):
    return next(e for e in h.events if e.event == "queue.rejected")


def test_stale_plan_is_rejected_with_current_version(h):
    h.api.version = "5"
    h.repo.add(plan_item())
    assert h.runner.tick() == ["rejected"]
    ev = rejected(h)
    assert ev.payload["reason"] == "base_version 3 != current 5"
    assert ev.payload["current"] == "5"
    assert h.api.applied == []


def test_plan_applied_rendered_critiqued_uploaded_and_reported(h):
    it = plan_item()
    h.repo.add(it)
    assert h.runner.tick() == ["applied"]
    assert h.api.applied[0][0] == "c1"
    subfolders = {s for _, s in h.up.uploads}
    assert {"renders", "digests"} <= subfolders
    crit = h.api.out.parent / "critique.json"
    assert json.loads(crit.read_text())["overall_verdict"] == "ship_it"
    ev = next(e for e in h.events if e.event == "render.done")
    assert ev.payload == {
        "plan_id": it["id"],
        "drive": {"mp4": "fid1", "critique": "fid1"},
        "critic": {"verdict": "ship_it", "opener": 4, "climax": 5, "pacing": "just_right"},
        "copy": {"title": "T", "caption": "C", "hashtags": ["#a"], "thumbnail_t": None},
    }


def test_plan_api_422_becomes_rejection_with_detail(h):
    h.api.apply_error = ApiError(422, {"problems": ["bad effect"]})
    h.repo.add(plan_item())
    assert h.runner.tick() == ["rejected"]
    assert "bad effect" in rejected(h).payload["reason"]


def test_verdict_with_quote_reaches_mentor_once(h):
    it = verdict_item(quote="that was sick", quote_ts="00:12:03")
    h.repo.add(it)
    assert h.runner.tick() == ["applied"]
    assert h.runner.tick() == []
    assert h.api.recorded == [
        {
            "game": "league",
            "feedback": "that was sick",
            "rule": "keep the kill beat",
            "clip_ref": "12m03s",
            "source_context": f"grokbot:{it['id']}",
            "tags": ["grokbot"],
            "verdict": "payoff",
        }
    ]


@pytest.mark.parametrize("quote", ["", "   \n"])
def test_verdict_without_quote_goes_to_decisions_log_only(h, quote):
    it = verdict_item(quote=quote)
    h.repo.add(it)
    assert h.runner.tick() == ["logged"]
    assert h.api.recorded == []
    log = h.tmp / "grok" / "decisions.log"
    lines = log.read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["id"] == it["id"]


def test_flags_off_keep_items_deferred(h, monkeypatch):
    monkeypatch.setattr(settings, "grokbot_execute_plans", False)
    monkeypatch.setattr(settings, "grokbot_record_verdicts", False)
    h.repo.add(plan_item())
    h.repo.add(verdict_item(quote="q q q"))
    assert h.runner.tick() == ["deferred", "deferred"]
    assert h.api.applied == [] and h.api.recorded == []


def test_deferred_items_are_reprocessed_when_flag_turns_on(h, monkeypatch):
    monkeypatch.setattr(settings, "grokbot_record_verdicts", False)
    h.repo.add(verdict_item(quote="nice one"))
    assert h.runner.tick() == ["deferred"]
    monkeypatch.setattr(settings, "grokbot_record_verdicts", True)
    assert h.runner.tick() == ["applied"]
    assert len(h.api.recorded) == 1
    assert h.runner.tick() == []
