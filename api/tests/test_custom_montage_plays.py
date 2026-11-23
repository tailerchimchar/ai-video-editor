"""Montage v2: play specs through build_custom_montage (ffmpeg and VLM stubbed)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_video_editor import shorts
from ai_video_editor.config import settings
from ai_video_editor.routers.shorts import CustomMontageBody


def _specs():
    return [
        {
            "clip_path": "a.mp4",
            "start_seconds": 10.0,
            "end_seconds": 20.0,
            "play_id": "p1",
            "is_last": False,
            "kill_offsets_s": [4.0],
        },
        {
            "clip_path": "a.mp4",
            "start_seconds": 30.0,
            "end_seconds": 38.0,
            "play_id": "p1",
            "is_last": True,
            "kill_offsets_s": [2.0, 6.0],
        },
    ]


@pytest.fixture
def stubbed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    monkeypatch.setattr(settings, "story_kill_sfx", True)
    monkeypatch.setattr(settings, "vlm_enabled", True)
    monkeypatch.setattr(settings, "shorts_watch_refine_enabled", False)
    monkeypatch.setattr(shorts, "_resolve_clip_path_for_custom", lambda p: (Path(p), 100.0))

    def no_analyzer(*a, **k):
        raise AssertionError("analyzer must not run for locked play windows")

    monkeypatch.setattr(shorts, "_analyze_and_window", no_analyzer)
    state = SimpleNamespace(plans=[], sfx=[], trims=[], review_calls=0)

    def fake_render(plan, out_path, **kw):
        state.plans.append(plan)
        Path(out_path).write_bytes(b"video")
        return True, None

    monkeypatch.setattr(shorts, "render_short", fake_render)
    monkeypatch.setattr(shorts, "trim_clip", lambda *a, **k: state.trims.append(a) or (True, None))
    monkeypatch.setattr("ai_video_editor.sfx_library.kaching_path", lambda **k: Path("kaching.wav"))

    def fake_sfx(src, out, *, sfx):
        state.sfx.append(sfx)
        Path(out).write_bytes(b"sfx")
        return True, None

    monkeypatch.setattr("ai_video_editor.edits.apply_sfx", fake_sfx)
    return state


def _fix(kind="trim_end"):
    return SimpleNamespace(fix=kind, clip_ref="01", fix_seconds=2.0, issue="x")


def test_locked_clips_skip_analyzer_and_fixes(stubbed, monkeypatch):
    def review(plan, out, game):
        stubbed.review_calls += 1
        return SimpleNamespace(is_cohesive=False, fixes=[_fix()])

    monkeypatch.setattr(shorts, "_run_vlm_review", review)
    monkeypatch.setattr(settings, "shorts_auto_fix_enabled", True)
    monkeypatch.setattr(settings, "shorts_max_review_iter", 2)
    res = shorts.build_custom_montage(clip_paths=[], name="m", play_specs=_specs())
    assert res["ok"]
    assert stubbed.trims == []  # no re-cut
    first = stubbed.plans[-1].clips[0]
    assert (first.start_seconds, first.end_seconds) == (10.0, 20.0)
    assert first.locked_window and first.play_id == "p1"


def test_only_last_segment_has_anchor(stubbed, monkeypatch):
    monkeypatch.setattr(shorts, "_run_vlm_review", lambda *a: None)
    shorts.build_custom_montage(clip_paths=[], name="m", play_specs=_specs(), run_vlm_check=False)
    a, b = stubbed.plans[0].clips
    assert a.anchor_offset_seconds is None
    assert b.anchor_offset_seconds == 6.0


def test_kaching_overlaid_at_remapped_times(stubbed, monkeypatch):
    shorts.build_custom_montage(clip_paths=[], name="m", play_specs=_specs(), run_vlm_check=False)
    (sfx,) = stubbed.sfx
    assert [e["at_seconds"] for e in sfx] == [4.0, 12.0, 16.0]  # 10 s clip, then +2, +6
    assert all(e["volume"] == 0.8 and e["sfx_path"].endswith("kaching.wav") for e in sfx)


def test_kaching_off_when_setting_off(stubbed, monkeypatch):
    monkeypatch.setattr(settings, "story_kill_sfx", False)
    shorts.build_custom_montage(clip_paths=[], name="m", play_specs=_specs(), run_vlm_check=False)
    assert stubbed.sfx == []


def test_sfx_failure_keeps_render(stubbed, monkeypatch):
    monkeypatch.setattr("ai_video_editor.edits.apply_sfx", lambda *a, **k: (False, "boom"))
    res = shorts.build_custom_montage(
        clip_paths=[], name="m", play_specs=_specs(), run_vlm_check=False
    )
    assert res["ok"] and res["shorts"][0]["ok"]


def test_router_accepts_play_specs(client, monkeypatch):
    seen = {}

    async def fake_job(job_id, body):
        seen["body"] = body

    monkeypatch.setattr("ai_video_editor.routers.shorts._run_custom_job", fake_job)
    r = client.post("/api/v1/shorts/custom_montage", json={"play_specs": _specs()})
    assert r.status_code == 200, r.text
    assert CustomMontageBody.model_validate(seen["body"]).play_specs == _specs()
    assert client.post("/api/v1/shorts/custom_montage", json={}).status_code == 400
