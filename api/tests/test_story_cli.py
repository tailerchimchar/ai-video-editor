"""story-render CLI: reads the panel round, Riot cache, lane and mic files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_video_editor.config import settings
from ai_video_editor.league.riot_timeline import RiotTimeline, TimelineEvent
from ai_video_editor.story import cli


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", tmp_path / "ws")
    return tmp_path / "ws"


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _one_kill_timeline() -> RiotTimeline:
    kill = TimelineEvent("kill", 1400.0, 1400.0, [], {})
    return RiotTimeline("NA1_1", "high", 0.0, None, [kill], [])


def test_load_beats_recording_seconds_and_refs(tmp_path):
    _write(
        tmp_path / "gaming-expert.json",
        [
            {
                "clip_ref": "06_kill_23m05s",
                "story_beats": [{"t_start": 1395.0, "t_end": 1410.0, "beat": "Neeko pick"}],
            },
            {"clip_ref": "30m00s", "story_beats": ["The close"]},
        ],
    )
    beats = cli.load_beats(tmp_path)
    assert [(b.t_start, b.t_end, b.beat) for b in beats] == [
        (1395.0, 1410.0, "Neeko pick"),
        (1800.0, 1800.0, "The close"),
    ]


def test_load_beats_clip_relative_offsets_by_clips_json(tmp_path):
    _write(tmp_path / "clips.json", [{"file": "01_kill_14m32s.mp4", "start_seconds": 870.0,
                                      "end_seconds": 892.5}])  # fmt: skip
    _write(
        tmp_path / "gaming-expert.json",
        [
            {
                "clip_ref": "14m32s",
                "story_beats": [{"t_start": 2.0, "t_end": 6.0, "beat": "Dive"}, "Cleanup"],
            }
        ],
    )
    beats = cli.load_beats(tmp_path)
    assert [(b.t_start, b.t_end) for b in beats] == [(872.0, 876.0), (870.0, 892.5)]


def test_mic_stream_read_from_workspace(ws):
    assert cli.load_mic_stream("a1") is None
    _write(ws / "mic" / "a1.json", {"stream_index": 2, "method": "title", "activity": {}})
    assert cli.load_mic_stream("a1") == 2


def test_mic_stream_none_when_meta_has_error(ws):
    _write(ws / "mic" / "a1.json", {"stream_index": 2, "method": "override", "error": "x"})
    assert cli.load_mic_stream("a1") is None


def test_round_dir_accepts_name_or_path(ws, tmp_path):
    assert cli.round_dir("2026-11-22_jhin") == ws / "panel" / "2026-11-22_jhin"
    assert cli.round_dir(str(tmp_path)) == tmp_path


def test_main_wires_plan_and_render(ws, tmp_path, monkeypatch):
    rnd = ws / "panel" / "r1"
    _write(rnd / "gaming-expert.json", [{"clip_ref": "23m15s", "story_beats": [
        {"t_start": 1395.0, "t_end": 1410.0, "beat": "Neeko pick"}]}])  # fmt: skip
    _write(ws / "mic" / "a1.json", {"stream_index": 1})
    tl = _one_kill_timeline()
    monkeypatch.setattr(cli, "load_timeline", lambda asset_id: tl)
    monkeypatch.setattr(cli, "load_lane", lambda asset_id: None)
    monkeypatch.setattr(cli, "asset_path", lambda asset_id: "C:/src.mp4")
    seen = {}

    def fake_render(src, plan, out_dir, *, cards, review):
        seen.update(src=src, plan=plan, out_dir=out_dir, cards=cards, review=review)
        return Path(out_dir) / "story.mp4"

    monkeypatch.setattr(cli, "render_story", fake_render)
    assert cli.main(["--asset", "a1", "--round", "r1", "--no-cards"]) == 0
    assert seen["src"] == "C:/src.mp4"
    assert seen["out_dir"] == ws / "story" / "r1"
    assert seen["cards"] is False
    assert seen["plan"].asset_id == "a1"
    assert seen["plan"].mic_stream == 1
    assert [c.title for c in seen["plan"].chapters] == ["Neeko pick"]


def test_main_fails_without_timeline(ws, monkeypatch):
    _write(ws / "panel" / "r1" / "gaming-expert.json", [])
    monkeypatch.setattr(cli, "load_timeline", lambda asset_id: None)
    assert cli.main(["--asset", "a1", "--round", "r1"]) == 2


def test_main_reports_render_failure(ws, monkeypatch):
    from ai_video_editor.story.render import StoryRenderError

    _write(ws / "panel" / "r1" / "gaming-expert.json", [])
    tl = _one_kill_timeline()
    monkeypatch.setattr(cli, "load_timeline", lambda asset_id: tl)
    monkeypatch.setattr(cli, "load_lane", lambda asset_id: None)

    def boom(*a, **k):
        raise StoryRenderError("chapter 1 failed")

    monkeypatch.setattr(cli, "render_story", boom)
    assert cli.main(["--asset", "a1", "--round", "r1", "--source", "C:/x.mp4"]) == 1


# --- review round 1 fixes ---


def test_load_beats_unknown_clip_uses_ref_time(tmp_path, caplog):
    # Real Jhin round: clip 01 has "file": null, so clips.json cannot place it.
    _write(tmp_path / "clips.json", [{"file": None, "start_seconds": 137.0, "end_seconds": 165.08}])
    _write(
        tmp_path / "gaming-expert.json",
        [
            {
                "clip_ref": "01_kill_2m17s (REJECTED by VLM, window 137.0-165.1)",
                "story_beats": [
                    {"t_start": 0.0, "t_end": 8.8, "beat": "setup: early skirmish"},
                    {"t_start": 8.8, "t_end": 10.8, "beat": "payoff: first kill"},
                ],
            }
        ],
    )
    with caplog.at_level("WARNING"):
        beats = cli.load_beats(tmp_path)
    assert [(b.t_start, round(b.t_end, 2)) for b in beats] == [(137.0, 145.8), (145.8, 147.8)]
    assert "unknown" in caplog.text


def test_clip_windows_keyed_by_clip_ref_and_file(tmp_path):
    _write(
        tmp_path / "clips.json",
        [
            {"file": "03_kill_11m22s.mp4", "start_seconds": 682.5, "end_seconds": 697.5},
            {"clip_ref": "20m00s", "file": None, "start_seconds": 1190.0, "end_seconds": 1210.0},
        ],
    )
    windows = cli._clip_windows(tmp_path)
    assert windows["11m22s"] == (682.5, 697.5)
    assert windows["20m00s"] == (1190.0, 1210.0)
    assert "None" not in windows


def test_load_beats_skips_game_story_and_long_beats(tmp_path):
    _write(
        tmp_path / "gaming-expert.json",
        [
            {"clip_ref": "GAME_STORY", "story_beats": [{"t_start": 0.0, "t_end": 2037.6,
                                                         "beat": "game: summary"}]},
            {"clip_ref": "10m00s", "story_beats": [
                {"t_start": 600.0, "t_end": 700.0, "beat": "too long"},
                {"t_start": 600.0, "t_end": 610.0, "beat": "kept"}]},
        ],
    )  # fmt: skip
    assert [b.beat for b in cli.load_beats(tmp_path)] == ["kept"]


def test_load_lane_malformed_json_returns_none(ws):
    _write(ws / "riot" / "a1" / "lane.json", {"opponent_champion": "Cait", "minutes": [{"x": 1}]})
    assert cli.load_lane("a1") is None


# --- final fix wave ---


def test_absolute_beat_just_before_its_clip_is_not_double_shifted(tmp_path):
    # 02_hype_callout_9m45s spans 585-620; a recording-second beat at 580 s
    # would land at 1165 s if treated as clip-relative: past the clip end.
    _write(tmp_path / "clips.json", [{"file": "02_hype_callout_9m45s.mp4",
                                      "start_seconds": 585.0, "end_seconds": 620.0}])  # fmt: skip
    _write(
        tmp_path / "gaming-expert.json",
        [
            {
                "clip_ref": "02_hype_callout_9m45s",
                "story_beats": [
                    {"t_start": 580.0, "t_end": 590.0, "beat": "absolute"},
                    {"t_start": 0.0, "t_end": 35.0, "beat": "relative"},
                ],
            }
        ],
    )
    beats = cli.load_beats(tmp_path)
    assert [(b.t_start, b.t_end, b.beat) for b in beats] == [
        (580.0, 590.0, "absolute"),
        (585.0, 620.0, "relative"),
    ]


def _main_with_plan(ws, monkeypatch, *, cards: bool, over_budget: bool = False):
    from ai_video_editor.story.planner import StoryPlan

    _write(ws / "panel" / "r1" / "gaming-expert.json", [])
    monkeypatch.setattr(cli, "load_timeline", lambda asset_id: _one_kill_timeline())
    monkeypatch.setattr(cli, "load_lane", lambda asset_id: None)
    seen = {}

    def fake_plan(*a, **k):
        seen.update(k)
        return StoryPlan(
            chapters=[], total_s=0.0, mic_stream=None, audio="mixed", over_budget=over_budget
        )

    monkeypatch.setattr(cli, "plan_story", fake_plan)
    monkeypatch.setattr(cli, "render_story", lambda *a, **k: Path("story.mp4"))
    argv = ["--asset", "a1", "--round", "r1", "--source", "C:/x.mp4"]
    assert cli.main(argv + ([] if cards else ["--no-cards"])) == 0
    return seen


def test_main_passes_card_seconds_only_when_cards_on(ws, monkeypatch):
    monkeypatch.setattr(settings, "story_card_seconds", 2.5)
    assert _main_with_plan(ws, monkeypatch, cards=True)["card_s"] == 2.5
    assert _main_with_plan(ws, monkeypatch, cards=False)["card_s"] == 0.0


def test_main_prints_over_budget(ws, monkeypatch, capsys):
    _main_with_plan(ws, monkeypatch, cards=False, over_budget=True)
    assert "over_budget" in capsys.readouterr().out
    _main_with_plan(ws, monkeypatch, cards=False, over_budget=False)
    assert "over_budget" not in capsys.readouterr().out


# --- plays drive cuts (#10) ---


def _plays_main(ws, monkeypatch, argv, *, drive: bool, frozen: bool):
    from ai_video_editor.league.play_models import GameModel

    _write(ws / "panel" / "r1" / "gaming-expert.json", [])
    if frozen:
        path = ws / "plays" / "a1.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(GameModel().model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(settings, "plays_drive_cuts", drive)
    monkeypatch.setattr(cli, "load_timeline", lambda a: _one_kill_timeline())
    monkeypatch.setattr(cli, "load_lane", lambda a: None)
    seen = {"path": None}
    monkeypatch.setattr(
        cli, "plan_story", lambda *a, **k: seen.update(path="beats") or _empty_plan()
    )
    monkeypatch.setattr(
        cli, "plan_story_from_plays", lambda *a, **k: seen.update(path="plays") or _empty_plan()
    )

    def fake_render(src, plan, out_dir, *, cards, review):
        seen["cards"] = cards
        return Path("story.mp4")

    monkeypatch.setattr(cli, "render_story", fake_render)
    seen["rc"] = cli.main(["--asset", "a1", "--round", "r1", "--source", "C:/x.mp4", *argv])
    return seen


def _empty_plan():
    from ai_video_editor.story.planner import StoryPlan

    return StoryPlan(asset_id="a1", chapters=[], total_s=0.0, mic_stream=None, audio="mixed")


def test_plays_drive_cuts_uses_frozen_file_by_default(ws, monkeypatch):
    seen = _plays_main(ws, monkeypatch, [], drive=True, frozen=True)
    assert seen["rc"] == 0 and seen["path"] == "plays" and seen["cards"] is False


def test_plays_drive_cuts_without_file_uses_beats(ws, monkeypatch):
    assert _plays_main(ws, monkeypatch, [], drive=True, frozen=False)["path"] == "beats"


def test_setting_off_uses_beats_unless_plays_flag(ws, monkeypatch):
    assert _plays_main(ws, monkeypatch, [], drive=False, frozen=True)["path"] == "beats"
    assert _plays_main(ws, monkeypatch, ["--plays"], drive=False, frozen=True)["path"] == "plays"


def test_no_plays_flag_forces_beats(ws, monkeypatch):
    assert _plays_main(ws, monkeypatch, ["--no-plays"], drive=True, frozen=True)["path"] == "beats"


def test_plays_flag_without_file_fails(ws, monkeypatch):
    seen = _plays_main(ws, monkeypatch, ["--plays"], drive=False, frozen=False)
    assert seen["rc"] == 2 and seen["path"] is None


# --- early game (AVE-57) ---


def _early_main(ws, monkeypatch, argv=(), *, setting=True, cache=True, confirm=None, want=None):
    import sys
    import types

    from ai_video_editor.league.early_game import EarlySegment
    from ai_video_editor.league.play_models import GameModel

    seg = EarlySegment(kind="opening", start_s=30, end_s=100, reason="r")
    monkeypatch.setattr(settings, "story_early_game", setting)
    wants = want if want is not None else confirm is not None
    monkeypatch.setattr(settings, "story_early_confirm", wants)
    if cache:
        _write(ws / "riot" / "a1" / "timeline.json", {"info": {"frames": []}})
    monkeypatch.setattr(cli, "early_segments", lambda tl, raw: [seg])
    monkeypatch.setattr(cli, "first_back_s", lambda raw, pid, off: 200.0)
    seen: dict = {}
    if confirm is not None:
        mod = types.ModuleType("ai_video_editor.vlm.early_confirm")

        def fake_confirm(video, segs, *, first_back_s, backend, fps=0.5):
            seen["confirm"] = (video, first_back_s)
            return confirm(segs)

        mod.confirm_early = fake_confirm
        monkeypatch.setitem(sys.modules, "ai_video_editor.vlm.early_confirm", mod)
        monkeypatch.setattr(cli, "_early_backend", lambda asset_id: object())
    else:
        monkeypatch.setitem(sys.modules, "ai_video_editor.vlm.early_confirm", None)
    _write(ws / "panel" / "r1" / "gaming-expert.json", [])
    path = ws / "plays" / "a1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(GameModel().model_dump_json(), encoding="utf-8")
    tl = _one_kill_timeline()
    tl.owner_pid = 3
    monkeypatch.setattr(cli, "load_timeline", lambda a: tl)

    def fake_plan(*a, **k):
        seen["early"] = k.get("early")
        return _empty_plan()

    monkeypatch.setattr(cli, "plan_story_from_plays", fake_plan)
    monkeypatch.setattr(cli, "render_story", lambda *a, **k: Path("story.mp4"))
    argv = ["--asset", "a1", "--round", "r1", "--source", "C:/x.mp4", "--plays", *argv]
    seen["rc"] = cli.main(argv)
    return seen


def test_early_segments_passed_to_planner(ws, monkeypatch):
    seen = _early_main(ws, monkeypatch)
    assert seen["rc"] == 0 and [s.kind for s in seen["early"]] == ["opening"]


def test_no_early_flag_and_setting_disable(ws, monkeypatch):
    assert _early_main(ws, monkeypatch, ["--no-early"])["early"] is None
    assert _early_main(ws, monkeypatch, setting=False)["early"] is None


def test_early_skipped_without_raw_timeline(ws, monkeypatch):
    assert _early_main(ws, monkeypatch, cache=False)["early"] is None


def test_early_confirm_refines_when_available(ws, monkeypatch):
    from ai_video_editor.league.early_game import EarlySegment

    def refine(segs):
        return [EarlySegment(kind="opening", start_s=40, end_s=90, reason="r", confirmed=True)]

    seen = _early_main(ws, monkeypatch, confirm=refine)
    assert seen["confirm"] == ("C:/x.mp4", 200.0)
    assert seen["early"][0].confirmed and seen["early"][0].start_s == 40


def test_early_confirm_missing_module_is_skipped(ws, monkeypatch):
    seen = _early_main(ws, monkeypatch, want=True)
    assert seen["rc"] == 0 and seen["early"][0].start_s == 30
