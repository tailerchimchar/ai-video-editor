import json

import httpx
import pytest
from test_grokbot_runner_phase2 import h, plan_item, rejected, verdict_item  # noqa: F401

from ai_video_editor.grokbot.runner import ApiError
from ai_video_editor.vlm.watch_montage import WatchResult


def test_critic_failure_still_marks_applied_and_reports_error(h):  # noqa: F811
    h.runner._watch = lambda path: WatchResult(
        critique=None, panel_count=0, total_seconds=0.0, error="vlm down"
    )
    h.repo.add(plan_item())
    assert h.runner.tick() == ["applied"]
    ev = next(e for e in h.events if e.event == "render.done")
    assert ev.payload["critic"] is None
    assert ev.payload["critic_error"] == "vlm down"
    assert ev.payload["drive"]["mp4"] == "fid1"
    assert h.runner.tick() == []


def test_crash_between_post_and_mark_does_not_duplicate(h):  # noqa: F811
    it = verdict_item(quote="that was sick")
    h.repo.add(it)
    # A prior run posted, then died before marking: ledger says "applying", mentor has the row.
    h.runner.ledger.mark(it["id"], "verdict", "applying")
    h.api.recorded.append({"source_context": f"grokbot:{it['id']}"})
    assert h.runner.tick() == ["applied"]
    assert len(h.api.recorded) == 1


def test_applying_without_mentor_row_reposts(h):  # noqa: F811
    it = verdict_item(quote="that was sick")
    h.repo.add(it)
    h.runner.ledger.mark(it["id"], "verdict", "applying")
    assert h.runner.tick() == ["applied"]
    assert len(h.api.recorded) == 1


def test_transient_verdict_failure_stays_applying_then_succeeds(h):  # noqa: F811
    it = verdict_item(quote="that was sick")
    h.repo.add(it)
    h.api.record_error = httpx.ConnectError("down")
    assert h.runner.tick() == []
    assert h.runner.ledger.outcomes()[it["id"]] == "applying"
    h.api.record_error = None
    assert h.runner.tick() == ["applied"]
    assert len(h.api.recorded) == 1


@pytest.mark.parametrize("kw", [{"quote": "ok"}, {"quote": "fine fine", "rule": "x "}])
def test_short_quote_or_rule_goes_to_decisions_log(h, kw):  # noqa: F811
    h.repo.add(verdict_item(**kw))
    assert h.runner.tick() == ["logged"]
    assert h.api.recorded == []
    line = json.loads((h.tmp / "grok" / "decisions.log").read_text().splitlines()[0])
    assert "shorter than 3" in line["reason"]


def test_transient_plan_errors_leave_item_unmarked(h):  # noqa: F811
    h.api.version_error = httpx.ReadTimeout("slow")
    h.repo.add(plan_item())
    assert h.runner.tick() == []
    assert h.events == [] and h.runner.ledger.outcomes() == {}
    h.api.version_error = None
    assert h.runner.tick() == ["applied"]


def test_permanent_4xx_on_version_is_rejected(h):  # noqa: F811
    h.api.version_error = ApiError(404, "Compilation not found")
    h.repo.add(plan_item())
    assert h.runner.tick() == ["rejected"]


def test_rejection_detail_is_truncated_and_paths_stripped(h):  # noqa: F811
    detail = "boom C:\\Users\\bob\\secret.mp4 and /home/bob/x.mp4 " + "z" * 900
    h.api.apply_error = ApiError(409, detail)
    h.repo.add(plan_item())
    assert h.runner.tick() == ["rejected"]
    reason = rejected(h).payload["reason"]
    assert len(reason) <= 520
    assert "bob" not in reason and "<path>" in reason


def _hash_of(item):
    from ai_video_editor.edit_plan import EditPlan, plan_hash

    return plan_hash(EditPlan.model_validate(item["plan"]))


def test_stale_but_same_hash_resumes_without_applying(h):  # noqa: F811
    it = plan_item()
    h.api.version = "9"
    h.api.hash = _hash_of(it)
    h.repo.add(it)
    assert h.runner.tick() == ["applied"]
    assert h.api.applied == []
    ev = next(e for e in h.events if e.event == "render.done")
    assert ev.payload["resumed"] is True


def test_stale_different_hash_still_rejects(h):  # noqa: F811
    h.api.version = "9"
    h.api.hash = "other"
    h.repo.add(plan_item())
    assert h.runner.tick() == ["rejected"]
    assert h.api.applied == []


def test_applying_is_not_seen(h):  # noqa: F811
    h.runner.ledger.mark("y", "verdict", "applying")
    assert not h.runner.ledger.seen("y")
    h.runner.ledger.mark("z", "verdict", "applied")
    assert h.runner.ledger.seen("z")


def test_path_with_spaces_is_scrubbed(h):  # noqa: F811
    h.api.apply_error = ApiError(409, "failed on C:\\My Videos\\clip one.mp4")
    h.repo.add(plan_item())
    h.runner.tick()
    reason = rejected(h).payload["reason"]
    assert "Videos" not in reason and "clip" not in reason


def test_final_ledger_outcomes_are_immutable(h):  # noqa: F811
    led = h.runner.ledger
    led.mark("x", "verdict", "applied")
    led.mark("x", "verdict", "rejected")
    assert led.outcomes()["x"] == "applied"
