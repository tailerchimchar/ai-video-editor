"""Intro module — pure config validation, storage paths, ffmpeg-mocked render.

Real ffmpeg never runs in tests. The renderer's filtergraph and command
construction are unit-tested by inspecting the built command list with
`subprocess.run` stubbed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ----- fixtures -----


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Monkeypatch the workspace so intros live under tmp_path/intros."""
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    return tmp_path


# ----- config / storage paths -----


def test_intros_root_uses_workspace(workspace):
    from ai_video_editor.intros.storage import intros_root

    assert intros_root() == (workspace / "intros").resolve()


def test_default_config_validates():
    from ai_video_editor.intros.config import default_intro_config

    cfg = default_intro_config("test")
    assert cfg.name == "test"
    assert cfg.duration == 3.0
    assert cfg.logo.scale == 0.55
    assert cfg.music is None


def test_config_round_trip(workspace):
    """save → load returns identical config (all fields preserved)."""
    from ai_video_editor.intros.config import default_intro_config, load_intro, save_intro

    cfg = default_intro_config("rt-test")
    save_intro(cfg)
    loaded = load_intro("rt-test")
    assert loaded.model_dump() == cfg.model_dump()


def test_config_rejects_bad_values():
    from ai_video_editor.intros.config import IntroConfig

    with pytest.raises(ValueError):
        IntroConfig(name="x", duration=0)  # duration must be >0
    with pytest.raises(ValueError):
        IntroConfig(name="x", logo={"scale": 1.5})  # scale > 1
    with pytest.raises(ValueError):
        IntroConfig(name="x", logo={"pulse_factor": 0.5})  # pulse < 1


def test_list_intros_skips_folders_without_config(workspace):
    """A folder under intros/ without intro.json shouldn't show up
    (in-progress / hand-deleted intros are noise)."""
    from ai_video_editor.intros.config import default_intro_config, save_intro
    from ai_video_editor.intros.storage import intros_root, list_intros

    save_intro(default_intro_config("real-one"))
    # Make a junk folder
    (intros_root() / "junk").mkdir()
    assert list_intros() == ["real-one"]


# ----- render: command construction with ffmpeg stubbed -----


def _make_logo(workspace, name: str = "test") -> Path:
    """Create the intro folder + a fake logo file so render_intro can
    pass its existence check."""
    from ai_video_editor.intros.config import default_intro_config, save_intro
    from ai_video_editor.intros.storage import intro_logo_path

    save_intro(default_intro_config(name))
    logo = intro_logo_path(name)
    logo.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    return logo


def test_render_returns_error_when_logo_missing(workspace):
    """No logo PNG on disk → fail fast with a useful message, don't
    invoke ffmpeg with a bogus path."""
    from ai_video_editor.intros.config import default_intro_config, save_intro
    from ai_video_editor.intros.render import render_intro

    cfg = default_intro_config("no-logo")
    save_intro(cfg)
    result = render_intro(cfg)
    assert result["ok"] is False
    assert result["output"] is None
    assert "logo not found" in result["error"]


def test_render_invokes_ffmpeg_with_expected_args(workspace, monkeypatch):
    """Inspect the ffmpeg command produced for a silent intro. We're
    not running it — just verifying we'd pass sane args."""
    from ai_video_editor.intros import render as render_mod
    from ai_video_editor.intros.config import default_intro_config
    from ai_video_editor.intros.render import render_intro

    _make_logo(workspace, "captured")
    captured: dict = {}

    def fake_run(cmd, capture_output=True, text=True):
        captured["cmd"] = cmd

        class R:
            returncode = 0
            stderr = ""

        return R()

    monkeypatch.setattr(render_mod.subprocess, "run", fake_run)
    cfg = default_intro_config("captured")
    result = render_intro(cfg)
    assert result["ok"] is True
    assert result["output"].endswith("intro.mp4")
    cmd = captured["cmd"]
    # Logo is input 0; silent audio (anullsrc) is input 1. Even when
    # no music is configured the intro carries a silent audio track
    # so downstream concat doesn't drop audio.
    assert "-filter_complex" in cmd
    assert "-y" in cmd
    assert "anullsrc=channel_layout=stereo:sample_rate=44100" in cmd
    # Map silent audio to output (so the intro mp4 has a 2-stream pair).
    assert "1:a" in cmd
    # Two inputs now: logo + silent.
    i_count = sum(1 for a in cmd if a == "-i")
    assert i_count == 2


def test_render_adds_music_input_when_music_present(workspace, monkeypatch):
    from ai_video_editor.intros import render as render_mod
    from ai_video_editor.intros.config import IntroConfig
    from ai_video_editor.intros.render import render_intro
    from ai_video_editor.intros.storage import intro_music_path

    _make_logo(workspace, "with-music")
    music = intro_music_path("with-music")
    music.write_bytes(b"\x00" * 32)

    captured: dict = {}

    def fake_run(cmd, capture_output=True, text=True):
        captured["cmd"] = cmd

        class R:
            returncode = 0
            stderr = ""

        return R()

    monkeypatch.setattr(render_mod.subprocess, "run", fake_run)
    cfg = IntroConfig(name="with-music", music={"filename": "music.mp3"})
    result = render_intro(cfg)
    assert result["ok"] is True
    assert result["has_music"] is True
    cmd = captured["cmd"]
    i_count = sum(1 for a in cmd if a == "-i")
    assert i_count == 2  # logo + music


def test_silent_intro_still_emits_audio_stream(workspace, monkeypatch):
    """Regression test for the concat audio-drop bug.

    Before the silent-track fix, a music-less intro produced a
    video-only mp4. When that intro was concat'd with gameplay clips
    that HAD audio, ffmpeg's concat demuxer dropped audio from the
    final compilation entirely. The fix: always emit an audio stream
    (silent if no music), so every input to the concat has matching
    streams. This test guards against the regression by asserting
    the silent-audio inputs + mapping are present in the command.
    """
    from ai_video_editor.intros import render as render_mod
    from ai_video_editor.intros.config import default_intro_config
    from ai_video_editor.intros.render import render_intro

    _make_logo(workspace, "silent-intro")
    captured: dict = {}

    def fake_run(cmd, capture_output=True, text=True):
        captured["cmd"] = cmd

        class R:
            returncode = 0
            stderr = ""

        return R()

    monkeypatch.setattr(render_mod.subprocess, "run", fake_run)
    cfg = default_intro_config("silent-intro")
    assert cfg.music is None  # confirms we're testing the no-music path
    render_intro(cfg)
    cmd = captured["cmd"]
    # Silent track input is generated via lavfi anullsrc...
    assert "anullsrc=channel_layout=stereo:sample_rate=44100" in cmd
    # ...mapped to output...
    assert "1:a" in cmd
    # ...and encoded as AAC matching what per-clip parts produce.
    aac_pos = [i for i, a in enumerate(cmd) if a == "aac"]
    assert aac_pos, "expected AAC encoding for the silent audio track"


# ----- text-mode logo -----


def test_text_logo_config_validates():
    from ai_video_editor.intros.config import IntroConfig, _TextLogoConfig

    cfg = IntroConfig(name="t", logo=_TextLogoConfig(text="NOODLZ"))
    assert cfg.logo.mode == "text"
    assert cfg.logo.text == "NOODLZ"


def test_text_logo_requires_non_empty_text():
    from ai_video_editor.intros.config import _TextLogoConfig

    with pytest.raises(ValueError):
        _TextLogoConfig(text="")


def test_image_vs_text_mode_load_via_discriminator(workspace):
    """A saved + reloaded text-mode config should come back as the
    correct branch of the union, not as the default image config."""
    from ai_video_editor.intros.config import (
        IntroConfig,
        _TextLogoConfig,
        load_intro,
        save_intro,
    )

    cfg = IntroConfig(name="tx", logo=_TextLogoConfig(text="HELLO", font_size=120))
    save_intro(cfg)
    reloaded = load_intro("tx")
    assert reloaded.logo.mode == "text"
    assert reloaded.logo.text == "HELLO"
    assert reloaded.logo.font_size == 120


def test_text_intro_render_skips_logo_existence_check(workspace, monkeypatch):
    """Text intros have no source PNG — `render_intro` must not bail
    on the logo-not-found path that image-mode uses."""
    from ai_video_editor.intros import render as render_mod
    from ai_video_editor.intros.config import IntroConfig, _TextLogoConfig
    from ai_video_editor.intros.render import render_intro
    from ai_video_editor.intros.storage import ensure_intro_folder

    ensure_intro_folder("text-render")
    captured: dict = {}

    def fake_run(cmd, capture_output=True, text=True):
        captured["cmd"] = cmd

        class R:
            returncode = 0
            stderr = ""

        return R()

    monkeypatch.setattr(render_mod.subprocess, "run", fake_run)
    cfg = IntroConfig(name="text-render", logo=_TextLogoConfig(text="HI"))
    result = render_intro(cfg)
    assert result["ok"] is True
    cmd = captured["cmd"]
    # Text mode: no logo input. Just silent audio.
    i_count = sum(1 for a in cmd if a == "-i")
    assert i_count == 1
    # And the audio is mapped from input 0 (no logo input ahead of it).
    assert "0:a" in cmd


def test_text_intro_filtergraph_uses_drawtext_per_letter(workspace, monkeypatch):
    """Letter-spacing only works if we emit one `drawtext` per letter,
    so this asserts the filtergraph has N drawtext nodes for an N-letter
    text. Anything else means the letter_spacing feature is broken."""
    from ai_video_editor.intros import render as render_mod
    from ai_video_editor.intros.config import IntroConfig, _TextLogoConfig
    from ai_video_editor.intros.render import render_intro
    from ai_video_editor.intros.storage import ensure_intro_folder

    ensure_intro_folder("letterspace")
    captured: dict = {}

    def fake_run(cmd, capture_output=True, text=True):
        captured["cmd"] = cmd

        class R:
            returncode = 0
            stderr = ""

        return R()

    monkeypatch.setattr(render_mod.subprocess, "run", fake_run)
    cfg = IntroConfig(
        name="letterspace",
        logo=_TextLogoConfig(text="HELLO", letter_spacing=10),
    )
    render_intro(cfg)
    filtergraph = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
    # 5 letters → 5 drawtext calls
    assert filtergraph.count("drawtext=") == 5
    # Each letter rendered separately
    for ch in "HELLO":
        assert f"text='{ch}'" in filtergraph


def test_text_intro_filtergraph_includes_stroke_and_shadow(workspace, monkeypatch):
    """When stroke_width > 0 OR shadow_offset is non-zero, those
    drawtext options must appear in the filtergraph."""
    from ai_video_editor.intros import render as render_mod
    from ai_video_editor.intros.config import IntroConfig, _TextLogoConfig
    from ai_video_editor.intros.render import render_intro
    from ai_video_editor.intros.storage import ensure_intro_folder

    ensure_intro_folder("fx")
    captured: dict = {}

    def fake_run(cmd, capture_output=True, text=True):
        captured["cmd"] = cmd

        class R:
            returncode = 0
            stderr = ""

        return R()

    monkeypatch.setattr(render_mod.subprocess, "run", fake_run)
    cfg = IntroConfig(
        name="fx",
        logo=_TextLogoConfig(
            text="X",
            stroke_width=4,
            stroke_color="cyan",
            shadow_offset_x=3,
            shadow_offset_y=3,
            shadow_color="black",
            shadow_alpha=0.6,
        ),
    )
    render_intro(cfg)
    fg = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
    assert "borderw=4" in fg
    assert "bordercolor=cyan" in fg
    assert "shadowx=3" in fg
    assert "shadowy=3" in fg
    assert "shadowcolor=black@0.60" in fg


def test_text_intro_alignment_changes_x_expression(workspace, monkeypatch):
    """Center alignment uses `(W-total_w)/2`; left uses a fixed margin;
    right anchors to the right edge."""
    from ai_video_editor.intros import render as render_mod
    from ai_video_editor.intros.config import IntroConfig, _TextLogoConfig
    from ai_video_editor.intros.render import render_intro
    from ai_video_editor.intros.storage import ensure_intro_folder

    def _filtergraph_for(alignment: str) -> str:
        name = f"align-{alignment}"
        ensure_intro_folder(name)
        captured: dict = {}

        def fake_run(cmd, capture_output=True, text=True):
            captured["cmd"] = cmd

            class R:
                returncode = 0
                stderr = ""

            return R()

        monkeypatch.setattr(render_mod.subprocess, "run", fake_run)
        cfg = IntroConfig(name=name, logo=_TextLogoConfig(text="X", alignment=alignment))
        render_intro(cfg)
        return captured["cmd"][captured["cmd"].index("-filter_complex") + 1]

    center_fg = _filtergraph_for("center")
    left_fg = _filtergraph_for("left")
    right_fg = _filtergraph_for("right")
    # Centered: divides by 2
    assert "(W-" in center_fg and ")/2" in center_fg
    # Left: fixed pixel margin from the left edge (60 in our impl)
    assert "x=60" in left_fg
    # Right: subtracts total_w from W
    assert "W-" in right_fg


# ----- PNG preset library -----


def test_presets_root_under_intros(workspace):
    from ai_video_editor.intros.storage import intros_root, presets_root

    assert presets_root() == intros_root() / "_presets"


def test_list_presets_empty_when_no_folder(workspace):
    from ai_video_editor.intros.storage import list_presets

    assert list_presets() == []


def test_list_presets_finds_pngs(workspace):
    """Drop two PNGs into _presets and confirm they list (sans extension)."""
    from ai_video_editor.intros.storage import (
        ensure_presets_root,
        list_presets,
        preset_path,
    )

    ensure_presets_root()
    preset_path("noodlz-bracketed").write_bytes(b"\x89PNG\r\n\x1a\n")
    preset_path("noodlz-glitch").write_bytes(b"\x89PNG\r\n\x1a\n")
    # A non-PNG sibling shouldn't show up
    (preset_path("noodlz-bracketed").parent / "notes.txt").write_text("hi")
    assert list_presets() == ["noodlz-bracketed", "noodlz-glitch"]


def test_presets_folder_skipped_by_list_intros(workspace):
    """`_presets/` is not an intro — it must NOT appear in `list_intros()`.

    The filter is `(d / 'intro.json').is_file()` so _presets is naturally
    excluded (it has no intro.json), but this is the regression test."""
    from ai_video_editor.intros.config import default_intro_config, save_intro
    from ai_video_editor.intros.storage import (
        ensure_presets_root,
        list_intros,
        preset_path,
    )

    save_intro(default_intro_config("real-intro"))
    ensure_presets_root()
    preset_path("a-preset").write_bytes(b"\x89PNG\r\n\x1a\n")
    assert list_intros() == ["real-intro"]


def test_render_propagates_ffmpeg_error(workspace, monkeypatch):
    """When ffmpeg returns non-zero, the failure is surfaced in
    `error` and `output` is None."""
    from ai_video_editor.intros import render as render_mod
    from ai_video_editor.intros.config import default_intro_config
    from ai_video_editor.intros.render import render_intro

    _make_logo(workspace, "fail")

    def fake_run(cmd, capture_output=True, text=True):
        class R:
            returncode = 1
            stderr = "x" * 2000 + " — at end: bad filtergraph"

        return R()

    monkeypatch.setattr(render_mod.subprocess, "run", fake_run)
    result = render_intro(default_intro_config("fail"))
    assert result["ok"] is False
    assert result["output"] is None
    assert "bad filtergraph" in result["error"]  # tail of stderr preserved


# ----- filtergraph expressions: damped bounce math -----


def test_bounce_expressions_collapse_to_static_when_disabled():
    """Setting bounce_pixels=0 + pulse_factor=1 should produce
    no-op filter expressions so the logo is just statically centered."""
    from ai_video_editor.intros.config import IntroConfig
    from ai_video_editor.intros.render import _bounce_filter_expressions

    cfg = IntroConfig(
        name="static",
        logo={"mode": "image", "bounce_pixels": 0, "pulse_factor": 1.0},
    )
    y_off, scale = _bounce_filter_expressions(cfg)
    # y_off uses amplitude (0) — the expression still evaluates to 0
    assert y_off.startswith("(0)*")
    # scale collapses to the literal value (no time-varying envelope)
    assert "0.5500" in scale  # default scale 0.55 formatted to .4f


def test_bounce_frequency_scales_with_count():
    from ai_video_editor.intros.config import IntroConfig
    from ai_video_editor.intros.render import _bounce_filter_expressions

    cfg_1 = IntroConfig(name="b1", duration=3.0, logo={"mode": "image", "bounce_count": 1})
    cfg_4 = IntroConfig(name="b4", duration=3.0, logo={"mode": "image", "bounce_count": 4})
    y1, _ = _bounce_filter_expressions(cfg_1)
    y4, _ = _bounce_filter_expressions(cfg_4)
    # 4-bounce expression should have a higher frequency than 1-bounce
    # (frequency = count / duration). Extract the numeric arg.
    import re

    f1 = float(re.search(r"2\*PI\*([\d.]+)\*t", y1).group(1))
    f4 = float(re.search(r"2\*PI\*([\d.]+)\*t", y4).group(1))
    assert f4 == pytest.approx(f1 * 4, rel=1e-3)


# ----- default intro -----


def test_default_intro_unset_returns_none(workspace):
    from ai_video_editor.intros.storage import get_default_intro_name

    assert get_default_intro_name() is None


def test_set_and_get_default_intro(workspace):
    from ai_video_editor.intros.storage import (
        get_default_intro_name,
        set_default_intro_name,
    )

    set_default_intro_name("noodlz-text-v1")
    assert get_default_intro_name() == "noodlz-text-v1"


def test_clear_default_intro(workspace):
    from ai_video_editor.intros.storage import (
        get_default_intro_name,
        set_default_intro_name,
    )

    set_default_intro_name("a")
    set_default_intro_name(None)
    assert get_default_intro_name() is None


def test_default_intro_settings_survives_other_keys(workspace):
    """The settings file might grow over time (other workspace knobs).
    Setting / clearing the default must not nuke unrelated keys."""
    from ai_video_editor.intros.storage import (
        load_intro_settings,
        save_intro_settings,
        set_default_intro_name,
    )

    save_intro_settings({"unrelated_setting": 42})
    set_default_intro_name("a")
    data = load_intro_settings()
    assert data["unrelated_setting"] == 42
    assert data["default_intro"] == "a"


def test_load_intro_settings_handles_missing_file(workspace):
    from ai_video_editor.intros.storage import load_intro_settings

    assert load_intro_settings() == {}


def test_load_intro_settings_handles_corrupt_file(workspace):
    """A hand-edited / partially-written settings file shouldn't take
    down compilation rendering. Falls back to empty dict."""
    from ai_video_editor.intros.storage import load_intro_settings, settings_path

    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text("not valid json {{{", encoding="utf-8")
    assert load_intro_settings() == {}


# ----- insert_intro_at_position mutator -----


def test_insert_intro_at_position_1_prepends():
    from ai_video_editor.compile import insert_intro_at_position

    spec = {"clips": [{"id": "c1", "event_type": "clip"}]}
    new_spec, dirty = insert_intro_at_position(
        spec,
        intro_name="noodlz",
        intro_path="/i.mp4",
        duration=3.0,
        position=1,
    )
    assert len(new_spec["clips"]) == 2
    assert new_spec["clips"][0]["event_type"] == "intro"
    assert new_spec["clips"][1]["id"] == "c1"
    assert dirty == {new_spec["clips"][0]["id"]}


def test_insert_intro_at_position_middle():
    """Inserting AFTER clip #2 → position=3 (1-based). Clip #2 stays
    where it is; the original clip #3 shifts to slot #4."""
    from ai_video_editor.compile import insert_intro_at_position

    spec = {
        "clips": [
            {"id": "a", "event_type": "clip"},
            {"id": "b", "event_type": "clip"},
            {"id": "c", "event_type": "clip"},
        ]
    }
    new_spec, _ = insert_intro_at_position(
        spec,
        intro_name="noodlz",
        intro_path="/i.mp4",
        duration=3.0,
        position=3,
    )
    assert len(new_spec["clips"]) == 4
    assert [c["id"] if c["event_type"] == "clip" else "INTRO" for c in new_spec["clips"]] == [
        "a",
        "b",
        "INTRO",
        "c",
    ]


def test_insert_intro_at_position_appends_when_out_of_range():
    """Position past the end should clamp to "append at the end" rather
    than raise — defensive UX, no IndexError surfacing to the user."""
    from ai_video_editor.compile import insert_intro_at_position

    spec = {"clips": [{"id": "a", "event_type": "clip"}]}
    new_spec, _ = insert_intro_at_position(
        spec, intro_name="x", intro_path="/x.mp4", duration=2.0, position=99
    )
    assert len(new_spec["clips"]) == 2
    assert new_spec["clips"][1]["event_type"] == "intro"


def test_insert_intro_at_position_does_not_replace_existing_intro():
    """Unlike `set_intro_clip` which replaces, this mutator stacks.
    Useful for "intro between every clip" patterns."""
    from ai_video_editor.compile import insert_intro_at_position, set_intro_clip

    spec = {"clips": [{"id": "c", "event_type": "clip"}]}
    spec, _ = set_intro_clip(spec, intro_name="noodlz", intro_path="/i.mp4", duration=3.0)
    # Now spec has [intro, clip]. Insert ANOTHER intro at position 3.
    spec, _ = insert_intro_at_position(
        spec, intro_name="other", intro_path="/o.mp4", duration=2.0, position=3
    )
    intros = [c for c in spec["clips"] if c["event_type"] == "intro"]
    assert len(intros) == 2  # both intros present, not replaced


def test_insert_intro_at_position_does_not_mutate_input():
    from ai_video_editor.compile import insert_intro_at_position

    spec = {"clips": [{"id": "c", "event_type": "clip"}]}
    insert_intro_at_position(spec, intro_name="x", intro_path="/x.mp4", duration=1.0, position=1)
    assert len(spec["clips"]) == 1  # input untouched


# ----- compile mutator integration -----


def test_set_intro_clip_prepends_when_no_intro_present():
    from ai_video_editor.compile import set_intro_clip

    spec = {"clips": [{"id": "abc12345", "event_type": "clip"}]}
    new_spec, dirty = set_intro_clip(
        spec, intro_name="noodlz", intro_path="/fake/intro.mp4", duration=3.0
    )
    assert len(new_spec["clips"]) == 2
    intro = new_spec["clips"][0]
    assert intro["event_type"] == "intro"
    assert intro["asset_id"] is None
    assert intro["asset_path"] == "/fake/intro.mp4"
    assert intro["intro_name"] == "noodlz"
    assert intro["end_seconds"] == 3.0
    assert dirty == {intro["id"]}


def test_set_intro_clip_replaces_existing_intro():
    """Applying a different intro to a reel that already has one
    should REPLACE (not double-stack) so reels can't accumulate intros."""
    from ai_video_editor.compile import set_intro_clip

    spec = {
        "clips": [
            {"id": "old-intro-id", "event_type": "intro", "intro_name": "noodlz"},
            {"id": "clip1", "event_type": "clip"},
        ]
    }
    new_spec, dirty = set_intro_clip(
        spec, intro_name="other-intro", intro_path="/other.mp4", duration=2.5
    )
    assert len(new_spec["clips"]) == 2  # same total, intro replaced
    assert new_spec["clips"][0]["intro_name"] == "other-intro"
    assert new_spec["clips"][0]["id"] != "old-intro-id"  # new id assigned
    # dirty is the new intro's id (so it re-renders fresh)
    assert dirty == {new_spec["clips"][0]["id"]}


def test_clear_intro_removes_intro_when_present():
    from ai_video_editor.compile import clear_intro_clip

    spec = {
        "clips": [
            {"id": "i", "event_type": "intro"},
            {"id": "c1", "event_type": "clip"},
        ]
    }
    new_spec, dirty = clear_intro_clip(spec)
    assert len(new_spec["clips"]) == 1
    assert new_spec["clips"][0]["id"] == "c1"
    assert dirty == set()


def test_clear_intro_is_noop_when_no_intro():
    from ai_video_editor.compile import clear_intro_clip

    spec = {"clips": [{"id": "c1", "event_type": "clip"}]}
    new_spec, dirty = clear_intro_clip(spec)
    assert new_spec["clips"] == spec["clips"]
    assert dirty == set()


def test_set_intro_does_not_mutate_input_spec():
    """Mutators must deep-copy — the caller's spec stays unchanged.
    Important so failed renders don't half-corrupt the spec on disk."""
    from ai_video_editor.compile import set_intro_clip

    spec = {"clips": [{"id": "c1", "event_type": "clip"}]}
    set_intro_clip(spec, intro_name="x", intro_path="/x.mp4", duration=1.0)
    assert len(spec["clips"]) == 1  # original untouched
    assert spec["clips"][0]["event_type"] == "clip"
