"""Montage v2: play specs through build_custom_montage (ffmpeg and VLM stubbed)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_video_editor import shorts
from ai_video_editor.config import settings

_REAL_RENDER_SHORT = shorts.render_short


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
        if kw.get("on_rendered"):
            kw["on_rendered"](plan)
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
    assert [x.model_dump() for x in seen["body"].play_specs] == _specs()
    assert client.post("/api/v1/shorts/custom_montage", json={}).status_code == 400


def test_malformed_spec_returns_clear_error(stubbed):
    bad = _specs()
    del bad[1]["start_seconds"]
    res = shorts.build_custom_montage(clip_paths=[], name="m", play_specs=bad)
    assert res["ok"] is False
    assert res["error"].startswith("play_specs[1]:") and "start_seconds" in res["error"]
    inverted = _specs()
    inverted[0]["end_seconds"] = 5.0
    res = shorts.build_custom_montage(clip_paths=[], name="m", play_specs=inverted)
    assert res["ok"] is False and "play_specs[0]" in res["error"]
    assert stubbed.plans == []


def test_kill_times_in_montage_uses_every_kill(stubbed, monkeypatch):
    shorts.build_custom_montage(clip_paths=[], name="m", play_specs=_specs(), run_vlm_check=False)
    plan = stubbed.plans[0]
    assert shorts._kill_times_in_montage(plan) == (4.0, 12.0, 16.0)


def _three_segment_specs():
    return [
        *_specs(),
        {
            "clip_path": "b.mp4",
            "start_seconds": 50.0,
            "end_seconds": 60.0,
            "play_id": "p2",
            "is_last": True,
            "kill_offsets_s": [5.0],
        },
    ]


def test_kaching_follows_beat_snapped_ends(stubbed, monkeypatch, tmp_path):
    # Real render_short (ffmpeg stubbed): the beat at 18.4 s pulls p1's last
    # segment end from montage 18.0 to 18.4, so p2's kill lands at 23.4 not 23.0.
    monkeypatch.setattr(shorts, "render_short", _REAL_RENDER_SHORT)
    monkeypatch.setattr(settings, "shorts_beat_sync_enabled", True)
    monkeypatch.setattr(settings, "shorts_beat_flash_enabled", False)
    music = tmp_path / "song.mp3"
    music.write_bytes(b"m")
    track = SimpleNamespace(beats=SimpleNamespace(beats=(18.4,)))
    monkeypatch.setattr("ai_video_editor.music.analysis.load_or_analyze", lambda _p: track)

    def fake_run(cmd, **_kw):
        Path(cmd[-1]).write_bytes(b"video")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(shorts.subprocess, "run", fake_run)
    res = shorts.build_custom_montage(
        clip_paths=[],
        name="m",
        play_specs=_three_segment_specs(),
        run_vlm_check=False,
        music_path=str(music),
    )
    assert res["ok"] and res["shorts"][0]["ok"]
    (sfx,) = stubbed.sfx
    assert [e["at_seconds"] for e in sfx] == pytest.approx([4.0, 12.0, 16.0, 23.4])


def test_watch_refine_drops_whole_plays_and_rebuilds_by_index(stubbed, monkeypatch):
    monkeypatch.setattr(settings, "shorts_watch_refine_enabled", True)
    seen = {}

    def fake_loop(*, initial_render, re_render_after_drop, clip_durations, clip_names, **_):
        from ai_video_editor.vlm.loops import WatchLoopResult

        seen["names"], seen["durations"] = list(clip_names), list(clip_durations)
        initial_render()
        re_render_after_drop([1])  # drop unit 0: both p1 segments
        return WatchLoopResult(
            ok=True,
            passes=2,
            final_verdict="needs_edits",
            stopped_reason="cap_reached",
            dropped_clips=["p1"],
            final_critique=None,
        )

    monkeypatch.setattr("ai_video_editor.vlm.loops.watch_and_refine_montage", fake_loop)
    res = shorts.build_custom_montage(clip_paths=[], name="m", play_specs=_three_segment_specs())
    assert res["ok"]
    assert seen["names"] == ["p1", "p2"] and seen["durations"] == [18.0, 10.0]
    assert [c.play_id for c in stubbed.plans[-1].clips] == ["p2"]
    assert res["shorts"][0]["clip_count"] == 3  # the requested plan; the render kept p2 only
    (sfx,) = stubbed.sfx
    assert [e["at_seconds"] for e in sfx] == [5.0]  # kaching from the rendered plan
