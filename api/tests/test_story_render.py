"""Story renderer: per-chapter ffmpeg argv, title cards, concat, review budget.

Only `test_real_ffmpeg_renders_two_chapters` runs ffmpeg (skipped without it).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from ai_video_editor.anthropic_surface import SpendCapError, check_budget
from ai_video_editor.config import settings
from ai_video_editor.story import render
from ai_video_editor.story.planner import Chapter, StoryPlan

GAME_LEG = "[0:a:0]volume=0.35[g];[m][g]amix=inputs=2:duration=first:normalize=0[a]"
MIX = "[0:a:2]loudnorm=I=-16:TP=-1.5:LRA=11[m];" + GAME_LEG
MIX_PLAIN = "[0:a:2]volume=1.0[m];" + GAME_LEG


def _plan(mic: int | None = 2, chapters: list[Chapter] | None = None) -> StoryPlan:
    chapters = chapters or [
        Chapter(title="Lane vs Caitlyn: kill", start_s=10.0, end_s=20.0, kind="lane"),
        Chapter(title="Neeko pick", start_s=100.0, end_s=130.0, kind="fight", beats=["Neeko pick"]),
    ]
    return StoryPlan(
        asset_id="a1",
        chapters=chapters,
        total_s=sum(c.end_s - c.start_s for c in chapters),
        mic_stream=mic,
        audio="mic_over_game" if mic is not None else "mixed",
    )


@pytest.fixture
def fake_ffmpeg(monkeypatch, tmp_path):
    """Capture every ffmpeg argv/timeout, every card config and the concat list."""
    calls: dict[str, list] = {"ffmpeg": [], "cards": [], "concat": []}

    def run(cmd, timeout):
        calls["ffmpeg"].append((cmd, timeout))
        Path(cmd[-1]).write_bytes(b"x")
        return True, None

    def intro(config):
        calls["cards"].append(config)
        out = tmp_path / f"card_src_{len(calls['cards'])}.mp4"
        out.write_bytes(b"x")
        return {"ok": True, "output": out.as_posix(), "error": None}

    def concat(parts, out_path):
        calls["concat"].append((list(parts), out_path))
        Path(out_path).write_bytes(b"x")
        return True, None

    monkeypatch.setattr(render, "_run_ffmpeg", run)
    monkeypatch.setattr(render, "render_intro", intro)
    monkeypatch.setattr(render, "_concat", concat)
    return calls


def test_chapter_filter_mixes_mic_over_game(monkeypatch):
    monkeypatch.setattr(settings, "story_mic_loudnorm", True)
    cmd = render.chapter_cmd("src.mp4", 10.0, 20.0, "out.mp4", mic_stream=2)
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert MIX in fc
    assert "scale=1920:1080" in fc
    assert cmd[cmd.index("-ss") + 1] == "10.0"
    assert cmd[cmd.index("-to") + 1] == "20.0"
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("[v]") - 1] == "-map"
    assert "-ar" in cmd and cmd[cmd.index("-ar") + 1] == "48000"
    assert cmd[-1] == "out.mp4"


def test_mic_loudnorm_off_uses_plain_volume_leg(monkeypatch):
    monkeypatch.setattr(settings, "story_mic_loudnorm", False)
    cmd = render.chapter_cmd("src.mp4", 10.0, 20.0, "out.mp4", mic_stream=2)
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert MIX_PLAIN in fc
    assert "loudnorm" not in fc


def test_no_mic_uses_game_audio_only():
    cmd = render.chapter_cmd("src.mp4", 10.0, 20.0, "out.mp4", mic_stream=None)
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "amix" not in fc
    assert "[0:a:0]" in fc
    assert "[a]" in fc


def test_timeout_scales_with_chapter_length(fake_ffmpeg, tmp_path):
    render.render_story("src.mp4", _plan(), tmp_path / "out", cards=False)
    chapter_calls = [t for cmd, t in fake_ffmpeg["ffmpeg"] if "-ss" in cmd]
    assert chapter_calls == [60 + 3 * 10.0, 60 + 3 * 30.0]
    assert render.chapter_timeout(100.0, 130.0) == 150.0


def test_cards_rendered_between_chapters(fake_ffmpeg, tmp_path):
    out_dir = tmp_path / "out"
    result = render.render_story("src.mp4", _plan(), out_dir)
    titles = [c.logo.text for c in fake_ffmpeg["cards"]]
    assert titles == ["Lane vs Caitlyn: kill", "Neeko pick"]
    assert all(c.logo.mode == "text" for c in fake_ffmpeg["cards"])
    assert all(c.duration == settings.story_card_seconds for c in fake_ffmpeg["cards"])
    [(parts, concat_out)] = fake_ffmpeg["concat"]
    names = [Path(p).name for p in parts]
    assert names == ["card_01.mp4", "chapter_01.mp4", "card_02.mp4", "chapter_02.mp4"]
    assert all(Path(p).parent == out_dir / "parts" for p in parts)
    assert result == out_dir / "story.mp4" == Path(concat_out)
    saved = json.loads((out_dir / "story.json").read_text(encoding="utf-8"))
    assert saved["chapters"][1]["title"] == "Neeko pick"


def test_no_cards_concats_chapters_only(fake_ffmpeg, tmp_path):
    render.render_story("src.mp4", _plan(), tmp_path / "out", cards=False)
    assert fake_ffmpeg["cards"] == []
    [(parts, _)] = fake_ffmpeg["concat"]
    assert [Path(p).name for p in parts] == ["chapter_01.mp4", "chapter_02.mp4"]


def test_chapter_failure_raises(fake_ffmpeg, monkeypatch, tmp_path):
    monkeypatch.setattr(render, "_run_ffmpeg", lambda cmd, timeout: (False, "boom"))
    with pytest.raises(render.StoryRenderError, match="boom"):
        render.render_story("src.mp4", _plan(), tmp_path / "out", cards=False)


def test_budget_checked_with_story_cap(fake_ffmpeg, monkeypatch, tmp_path):
    order: list[tuple] = []

    def budget(key, *, cap_usd=None, cap_name=None):
        order.append(("budget", key, cap_usd, cap_name))

    def critic(path, *, fps, spend_key):
        order.append(("critic", fps, spend_key))
        return render.WatchResult(critique=None, panel_count=0, total_seconds=0.0, error="x")

    monkeypatch.setattr(render, "check_budget", budget)
    monkeypatch.setattr(render, "watch_montage", critic)
    render.render_story("src.mp4", _plan(), tmp_path / "out", cards=False, review=True)
    assert order == [
        ("budget", "story:a1", 5.0, "STORY_BUDGET_USD"),
        ("critic", 0.25, "story:a1"),
    ]
    assert (tmp_path / "out" / "critique.md").is_file()


def test_review_skipped_when_over_story_budget(fake_ffmpeg, monkeypatch, tmp_path):
    def budget(key, *, cap_usd=None, cap_name=None):
        raise SpendCapError("over")

    called = []
    monkeypatch.setattr(render, "check_budget", budget)
    monkeypatch.setattr(render, "watch_montage", lambda *a, **k: called.append(1))
    out = render.render_story("src.mp4", _plan(), tmp_path / "out", cards=False, review=True)
    assert out.name == "story.mp4"
    assert called == []


def test_check_budget_cap_override(tmp_path):
    db = tmp_path / "ledger.db"
    check_budget("story:a1", db_path=db, cap_usd=5.0)  # creates the ledger, no spend
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO spend_ledger (asset_id, feature, model, input_tokens, output_tokens, "
            "cost_usd, ts) VALUES ('story:a1', 'watch', 'm', 0, 0, 3.0, 't')"
        )
    conn.close()
    check_budget("story:a1", db_path=db, cap_usd=5.0)  # 3 < 5: allowed
    with pytest.raises(SpendCapError, match="COST_CAP_PER_ASSET_USD"):
        check_budget("story:a1", db_path=db)  # 3 >= default asset cap 2
    # M2: the message names the cap that applied.
    with pytest.raises(SpendCapError, match=r"cap \$2\.50, STORY_BUDGET_USD") as exc:
        check_budget("story:a1", db_path=db, cap_usd=2.5, cap_name="STORY_BUDGET_USD")
    assert "COST_CAP_PER_ASSET_USD" not in str(exc.value)


def _probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(out.stdout.strip())


@pytest.mark.real_ffmpeg
@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg"
)
def test_real_ffmpeg_renders_two_chapters(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", tmp_path / "ws")
    monkeypatch.setattr(settings, "ffmpeg_path", "ffmpeg")
    monkeypatch.setattr(settings, "story_mic_loudnorm", True)
    monkeypatch.setattr(settings, "ffmpeg_video_codec", "libx264")
    monkeypatch.setattr(settings, "ffmpeg_video_codec_opts", "-preset ultrafast -crf 30")
    src = tmp_path / "src.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440",
            "-f", "lavfi", "-i", "sine=frequency=880",
            "-map", "0:v", "-map", "1:a", "-map", "2:a", "-t", "3",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", src,
        ],
        check=True,
    )  # fmt: skip
    plan = _plan(
        mic=1,
        chapters=[
            Chapter(title="One", start_s=0.0, end_s=1.5, kind="fight"),
            Chapter(title="Two", start_s=1.5, end_s=3.0, kind="fight"),
        ],
    )
    out = render.render_story(str(src), plan, tmp_path / "out")
    expected = 3.0 + 2 * settings.story_card_seconds
    assert _probe_duration(out) == pytest.approx(expected, abs=0.35)
    dims = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", out],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    assert dims == "1920,1080"


# --- M8: a source without audio renders video-only (silent track) ---


def test_chapter_cmd_without_audio_uses_a_silent_track():
    cmd = render.chapter_cmd("src.mp4", 10.0, 20.0, "out.mp4", mic_stream=2, has_audio=False)
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "[0:a" not in fc and "[a]" not in fc
    i = cmd.index("anullsrc=r=48000:cl=stereo")
    assert cmd[i - 5 : i] == ["-f", "lavfi", "-t", "10.0", "-i"]
    maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
    assert maps == ["[v]", "1:a"]


def test_render_probes_audio_once_and_goes_video_only(fake_ffmpeg, monkeypatch, tmp_path):
    probes = []
    monkeypatch.setattr(render, "_probe_audio_count", lambda p: probes.append(p) or 0)
    render.render_story("src.mp4", _plan(), tmp_path / "out", cards=False)
    assert probes == ["src.mp4"]
    chapter_cmds = [c for c, _ in fake_ffmpeg["ffmpeg"]]
    assert len(chapter_cmds) == 2
    assert all("anullsrc=r=48000:cl=stereo" in c for c in chapter_cmds)


def test_render_keeps_audio_when_probe_is_unknown(fake_ffmpeg, monkeypatch, tmp_path):
    monkeypatch.setattr(render, "_probe_audio_count", lambda p: None)
    render.render_story("src.mp4", _plan(), tmp_path / "out", cards=False)
    assert all("anullsrc=r=48000:cl=stereo" not in c for c, _ in fake_ffmpeg["ffmpeg"])


def test_mic_stream_missing_from_source_falls_back_to_game_audio(
    fake_ffmpeg, monkeypatch, tmp_path
):
    monkeypatch.setattr(render, "_probe_audio_count", lambda p: 1)
    render.render_story("src.mp4", _plan(mic=2), tmp_path / "out", cards=False)
    for cmd, _ in fake_ffmpeg["ffmpeg"]:
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "[0:a:2]" not in fc and "[0:a:0]anull[a]" in fc
