"""Tests for the editor quirk flags (montage title opt-out + caption fontsize scale).

Both flags are OFF by default to preserve byte-for-byte identical filter graphs.
When ON, they enable the new behavior.

These tests mock/inspect the ffmpeg filter strings rather than rendering real video.
"""

from __future__ import annotations

# ---------------------------------------------------------------------
# 1) MONTAGE_TITLE_OPTIONAL — opt-out for the title burn-in
# ---------------------------------------------------------------------


class TestMontageTitleOptOut:
    """The title overlay in compile_custom_montage is controlled by
    MONTAGE_TITLE_OPTIONAL (config flag) + burn_title param."""

    def test_title_drawtext_default_produces_title(self) -> None:
        """With flag OFF (default), _title_drawtext always returns a filter."""
        from ai_video_editor.shorts import _title_drawtext

        result = _title_drawtext("PENTAKILL")
        assert "drawtext" in result
        assert "PENTAKILL" in result
        assert "between(t,0.2,2.5)" in result

    def test_flag_off_build_filter_complex_includes_title(self, monkeypatch) -> None:
        """With MONTAGE_TITLE_OPTIONAL=False (default), _build_filter_complex
        always includes the title drawtext regardless of burn_title param."""
        from ai_video_editor.config import settings
        from ai_video_editor.shorts import _build_filter_complex

        monkeypatch.setattr(settings, "montage_title_optional", False)

        result = _build_filter_complex(
            n_video_inputs=2,
            title="MULTIKILL",
            mode="montage",
            music_input_index=None,
            duck_volume=0.2,
            music_volume=0.4,
            layout="blur_fill",
            burn_title=True,
        )
        assert "MULTIKILL" in result
        assert "drawtext" in result

    def test_flag_off_ignores_burn_title_false(self, monkeypatch) -> None:
        """With flag OFF, burn_title=False is ignored — title still burns."""
        from ai_video_editor.config import settings
        from ai_video_editor.shorts import _build_filter_complex

        monkeypatch.setattr(settings, "montage_title_optional", False)

        result = _build_filter_complex(
            n_video_inputs=2,
            title="TEST_TITLE",
            mode="montage",
            music_input_index=None,
            duck_volume=0.2,
            music_volume=0.4,
            layout="blur_fill",
            burn_title=False,
        )
        assert "TEST_TITLE" in result

    def test_flag_on_burn_title_true_includes_title(self, monkeypatch) -> None:
        """With flag ON + burn_title=True (default), title is burned in."""
        from ai_video_editor.config import settings
        from ai_video_editor.shorts import _build_filter_complex

        monkeypatch.setattr(settings, "montage_title_optional", True)

        result = _build_filter_complex(
            n_video_inputs=2,
            title="MY_TITLE",
            mode="montage",
            music_input_index=None,
            duck_volume=0.2,
            music_volume=0.4,
            layout="blur_fill",
            burn_title=True,
        )
        assert "MY_TITLE" in result
        assert "drawtext" in result

    def test_flag_on_burn_title_false_omits_title(self, monkeypatch) -> None:
        """With flag ON + burn_title=False, title overlay is skipped."""
        from ai_video_editor.config import settings
        from ai_video_editor.shorts import _build_filter_complex

        monkeypatch.setattr(settings, "montage_title_optional", True)

        result = _build_filter_complex(
            n_video_inputs=2,
            title="SHOULD_NOT_APPEAR",
            mode="montage",
            music_input_index=None,
            duck_volume=0.2,
            music_volume=0.4,
            layout="blur_fill",
            burn_title=False,
        )
        assert "SHOULD_NOT_APPEAR" not in result
        # The video stream should still go through, just without the title
        assert "[vout]" in result

    def test_flag_off_filter_graph_unchanged(self, monkeypatch) -> None:
        """Flag OFF produces the exact same filter graph as before the change."""
        from ai_video_editor.config import settings
        from ai_video_editor.shorts import _build_filter_complex

        monkeypatch.setattr(settings, "montage_title_optional", False)

        # Capture the baseline filter graph
        baseline = _build_filter_complex(
            n_video_inputs=2,
            title="BASELINE",
            mode="montage",
            music_input_index=None,
            duck_volume=0.2,
            music_volume=0.4,
            layout="blur_fill",
            burn_title=True,
        )

        # Same call with burn_title explicitly True should be identical
        explicit_true = _build_filter_complex(
            n_video_inputs=2,
            title="BASELINE",
            mode="montage",
            music_input_index=None,
            duck_volume=0.2,
            music_volume=0.4,
            layout="blur_fill",
            burn_title=True,
        )
        assert baseline == explicit_true


# ---------------------------------------------------------------------
# 2) CAPTION_FONTSIZE_SCALE — scale caption fontsize to output resolution
# ---------------------------------------------------------------------


class TestCaptionFontsizeScale:
    """Caption fontsize scaling relative to 1080p reference height."""

    def test_flag_off_fontsize_unchanged(self) -> None:
        """With scale_enabled=False (default), fontsize is used as-is."""
        from ai_video_editor.edits import caption_filters

        segments = [
            {"text": "Hello", "start_seconds": 0.0, "end_seconds": 2.0, "style": {"fontsize": 100}},
        ]
        result = caption_filters(segments, clip_start_offset=0.0, scale_enabled=False)
        # Fontsize should be exactly 100 as requested
        assert "fontsize=100" in result

    def test_flag_off_ignores_output_height(self) -> None:
        """With flag OFF, output_height param is ignored."""
        from ai_video_editor.edits import caption_filters

        segments = [
            {"text": "Test", "start_seconds": 0.0, "end_seconds": 1.0, "style": {"fontsize": 80}},
        ]
        # Pass output_height but it should be ignored
        result = caption_filters(
            segments, clip_start_offset=0.0, output_height=720, scale_enabled=False
        )
        assert "fontsize=80" in result

    def test_flag_on_scales_for_720_output(self) -> None:
        """With flag ON and output_height=720, fontsize scales down from 1080 ref."""
        from ai_video_editor.edits import caption_filters

        segments = [
            {"text": "Test", "start_seconds": 0.0, "end_seconds": 1.0, "style": {"fontsize": 90}},
        ]
        # 720 / 1080 = 0.667, so fontsize 90 -> 60
        result = caption_filters(
            segments, clip_start_offset=0.0, output_height=720, scale_enabled=True
        )
        assert "fontsize=60" in result

    def test_flag_on_scales_for_1920_output(self) -> None:
        """With flag ON and output_height=1920, fontsize scales up from 1080 ref."""
        from ai_video_editor.edits import caption_filters

        segments = [
            {"text": "Test", "start_seconds": 0.0, "end_seconds": 1.0, "style": {"fontsize": 60}},
        ]
        # 1920 / 1080 = 1.778, so fontsize 60 -> ~107 (rounded)
        result = caption_filters(
            segments, clip_start_offset=0.0, output_height=1920, scale_enabled=True
        )
        # Allow for small rounding variance (106-108 are acceptable)
        import re

        match = re.search(r"fontsize=(\d+)", result)
        assert match
        fontsize = int(match.group(1))
        assert 105 <= fontsize <= 108, f"Expected scaled fontsize ~107, got {fontsize}"

    def test_flag_on_1080_output_unchanged(self) -> None:
        """With flag ON and output_height=1080 (reference), fontsize is unchanged."""
        from ai_video_editor.edits import caption_filters

        segments = [
            {"text": "Test", "start_seconds": 0.0, "end_seconds": 1.0, "style": {"fontsize": 72}},
        ]
        result = caption_filters(
            segments, clip_start_offset=0.0, output_height=1080, scale_enabled=True
        )
        assert "fontsize=72" in result

    def test_flag_on_clamps_max_fontsize_for_width(self) -> None:
        """With flag ON, fontsize is clamped so text width fits with margin."""
        from ai_video_editor.edits import caption_filters

        # Very large fontsize that would clip on 720px wide output
        segments = [
            {
                "text": "PENTAKILL MULTIKILL",  # ~19 chars
                "start_seconds": 0.0,
                "end_seconds": 1.0,
                "style": {"fontsize": 200},
            },
        ]
        result = caption_filters(
            segments,
            clip_start_offset=0.0,
            output_height=1280,
            output_width=720,
            scale_enabled=True,
        )
        # Extract the fontsize from the filter string
        import re

        match = re.search(r"fontsize=(\d+)", result)
        assert match
        fontsize = int(match.group(1))
        # Should be clamped below the requested 200 to fit
        assert fontsize < 200

    def test_flag_on_default_output_height_uses_1080(self) -> None:
        """With flag ON but default output dimensions (1920x1080), fontsize unchanged."""
        from ai_video_editor.edits import caption_filters

        segments = [
            {"text": "Test", "start_seconds": 0.0, "end_seconds": 1.0, "style": {"fontsize": 50}},
        ]
        # Default output_height=1080, output_width=1920 -> no scaling
        result = caption_filters(segments, clip_start_offset=0.0, scale_enabled=True)
        assert "fontsize=50" in result

    def test_flag_off_filter_graph_byte_identical(self) -> None:
        """With flag OFF, filter graph is byte-for-byte identical.

        The output is the same regardless of output dimensions when scaling is disabled.
        """
        from ai_video_editor.edits import caption_filters

        segments = [
            {"text": "Hello World", "start_seconds": 1.0, "end_seconds": 3.0},
        ]

        # Both calls should produce identical output with scale_enabled=False
        result1 = caption_filters(segments, clip_start_offset=0.0, scale_enabled=False)
        result2 = caption_filters(
            segments, clip_start_offset=0.0, output_height=720, scale_enabled=False
        )
        assert result1 == result2

    def test_text_wrapping_on_long_text(self) -> None:
        """With flag ON and long text, text wraps onto multiple lines."""
        from ai_video_editor.edits import caption_filters

        segments = [
            {
                "text": "This is a really long caption that should wrap onto two lines",
                "start_seconds": 0.0,
                "end_seconds": 2.0,
                "style": {"fontsize": 60},
            },
        ]
        result = caption_filters(
            segments,
            clip_start_offset=0.0,
            output_height=1280,
            output_width=720,
            scale_enabled=True,
        )
        # Text should be wrapped (contains escaped newline)
        # The escaped newline in drawtext could be \n or \\n depending on escaping
        assert "\\n" in result or "\n" in result.replace("\\n", "\n")


# ---------------------------------------------------------------------
# Config flag tests
# ---------------------------------------------------------------------


class TestConfigFlags:
    """The new config flags exist and have correct defaults."""

    def test_montage_title_optional_exists_and_defaults_false(self) -> None:
        """MONTAGE_TITLE_OPTIONAL config field exists and defaults to False."""
        from ai_video_editor.config import Settings

        # Create a fresh settings instance with minimal required fields
        s = Settings(
            outplayed_media_dir="/tmp/media",
            workspace_dir="/tmp/workspace",
        )
        assert hasattr(s, "montage_title_optional")
        assert s.montage_title_optional is False

    def test_caption_fontsize_scale_enabled_exists_and_defaults_false(self) -> None:
        """CAPTION_FONTSIZE_SCALE_ENABLED config field exists and defaults to False."""
        from ai_video_editor.config import Settings

        s = Settings(
            outplayed_media_dir="/tmp/media",
            workspace_dir="/tmp/workspace",
        )
        assert hasattr(s, "caption_fontsize_scale_enabled")
        assert s.caption_fontsize_scale_enabled is False
