"""TikTok-style word-by-word caption renderer + per-clip mutator."""

from __future__ import annotations

import pytest

# ----- caption_filters_tiktok pure tests -----


def test_tiktok_emits_one_drawtext_per_word():
    from ai_video_editor.edits import caption_filters_tiktok

    seg = {
        "start_seconds": 10.0,
        "end_seconds": 11.5,
        "text": "got him",
        "words": [
            {"word": "got", "start": 10.0, "end": 10.5},
            {"word": "him", "start": 10.6, "end": 11.2},
        ],
    }
    out = caption_filters_tiktok([seg], clip_start_offset=0.0)
    # Two drawtext filters (one per word), comma-joined.
    assert out.count("drawtext=") == 2
    assert "text='got'" in out
    assert "text='him'" in out


def test_tiktok_uses_clip_relative_timing():
    """Word timestamps are absolute (source-VOD time). The renderer must
    subtract clip_start_offset so the `enable=between(t,…)` ticks count
    from the clip's t=0."""
    from ai_video_editor.edits import caption_filters_tiktok

    seg = {
        "start_seconds": 100.0,
        "end_seconds": 101.0,
        "text": "wow",
        "words": [{"word": "wow", "start": 100.2, "end": 100.7}],
    }
    out = caption_filters_tiktok([seg], clip_start_offset=100.0)
    # 100.2 - 100.0 = 0.20s into the clip, 100.7 - 100.0 = 0.70s.
    assert "between(t,0.20,0.70)" in out


def test_tiktok_falls_back_to_even_split_when_no_words():
    """Legacy transcripts (pre-word-timestamps) have text but no `words`.
    The renderer must split the text evenly across the segment span so
    TikTok mode still produces SOMETHING usable."""
    from ai_video_editor.edits import caption_filters_tiktok

    seg = {
        "start_seconds": 0.0,
        "end_seconds": 3.0,
        "text": "one two three",
        # words deliberately absent
    }
    out = caption_filters_tiktok([seg], clip_start_offset=0.0)
    # 3 words / 3s span => 1s per word.
    assert "text='one'" in out
    assert "text='two'" in out
    assert "text='three'" in out
    # First word: enable=between(t,0.00,1.00).
    assert "between(t,0.00,1.00)" in out


def test_tiktok_empty_segments_returns_empty_string():
    from ai_video_editor.edits import caption_filters_tiktok

    assert caption_filters_tiktok([], clip_start_offset=0.0) == ""
    assert caption_filters_tiktok([{"start_seconds": 0, "end_seconds": 1, "text": "  "}], 0.0) == ""


def test_tiktok_top_center_position_and_big_font():
    """Default styling: top-center (y=h/8), fontsize 80. These are the
    'TikTok look' opinionated defaults; if either drifts, captions stop
    looking right."""
    from ai_video_editor.edits import caption_filters_tiktok

    seg = {
        "start_seconds": 0,
        "end_seconds": 1,
        "text": "hi",
        "words": [{"word": "hi", "start": 0, "end": 0.5}],
    }
    out = caption_filters_tiktok([seg], clip_start_offset=0.0)
    assert "fontsize=80" in out
    assert "y=h/8" in out
    assert "x=(w-text_w)/2" in out


# ----- set_caption_mode mutator -----


def _mini_spec(*clip_ids: str) -> dict:
    return {
        "id": "spec-1",
        "aspect": "16:9",
        "fade_seconds": 0.3,
        "clips": [
            {
                "id": cid,
                "asset_id": "a1",
                "asset_path": "/fake.mp4",
                "start_seconds": 0.0,
                "end_seconds": 10.0,
                "event_type": "clip",
                "caption_segments": [],
                "effects": [],
                "caption_mode": "segment",
            }
            for cid in clip_ids
        ],
    }


def test_set_caption_mode_flips_target_clip_only():
    from ai_video_editor.compile import set_caption_mode

    spec = _mini_spec("a", "b", "c")
    new_spec, dirty = set_caption_mode(spec, 1, "tiktok")
    assert new_spec["clips"][0]["caption_mode"] == "segment"
    assert new_spec["clips"][1]["caption_mode"] == "tiktok"
    assert new_spec["clips"][2]["caption_mode"] == "segment"
    assert dirty == {"b"}


def test_set_caption_mode_revert_to_segment():
    from ai_video_editor.compile import set_caption_mode

    spec = _mini_spec("a")
    spec["clips"][0]["caption_mode"] = "tiktok"
    new_spec, _ = set_caption_mode(spec, 0, "segment")
    assert new_spec["clips"][0]["caption_mode"] == "segment"


def test_set_caption_mode_rejects_invalid_mode():
    from ai_video_editor.compile import set_caption_mode

    spec = _mini_spec("a")
    with pytest.raises(ValueError, match="caption_mode"):
        set_caption_mode(spec, 0, "karaoke")


def test_set_caption_mode_does_not_mutate_caller_spec():
    from ai_video_editor.compile import set_caption_mode

    spec = _mini_spec("a")
    set_caption_mode(spec, 0, "tiktok")
    assert spec["clips"][0]["caption_mode"] == "segment"


# ----- _build_clip_filterchain renderer selection -----


def test_build_filterchain_uses_tiktok_renderer_when_mode_tiktok():
    """When clip.caption_mode == 'tiktok', the chain should contain the
    big-font top-center drawtext calls, NOT the bottom-center segment ones."""
    from ai_video_editor.compile import _build_clip_filterchain

    clip = {
        "id": "x",
        "start_seconds": 0.0,
        "end_seconds": 2.0,
        "effects": [],
        "caption_segments": [
            {
                "start_seconds": 0.0,
                "end_seconds": 2.0,
                "text": "yo",
                "words": [{"word": "yo", "start": 0.0, "end": 1.0}],
            }
        ],
        "caption_mode": "tiktok",
    }
    chain = _build_clip_filterchain(clip, aspect="16:9", fade_seconds=0.0)
    assert "fontsize=80" in chain  # tiktok renderer's big font
    assert "y=h/8" in chain  # top-center


def test_build_filterchain_segment_mode_uses_classic_renderer():
    from ai_video_editor.compile import _build_clip_filterchain

    clip = {
        "id": "x",
        "start_seconds": 0.0,
        "end_seconds": 2.0,
        "effects": [],
        "caption_segments": [
            {
                "start_seconds": 0.0,
                "end_seconds": 2.0,
                "text": "yo",
                "words": [{"word": "yo", "start": 0.0, "end": 1.0}],
            }
        ],
        "caption_mode": "segment",
    }
    chain = _build_clip_filterchain(clip, aspect="16:9", fade_seconds=0.0)
    assert "fontsize=36" in chain  # segment renderer's smaller font
    assert "y=h-th-60" in chain  # bottom-center


def test_build_filterchain_defaults_to_segment_when_mode_missing():
    """Specs from before the caption_mode field existed should render
    in classic segment mode rather than crashing."""
    from ai_video_editor.compile import _build_clip_filterchain

    clip = {
        "id": "x",
        "start_seconds": 0.0,
        "end_seconds": 2.0,
        "effects": [],
        "caption_segments": [{"start_seconds": 0.0, "end_seconds": 2.0, "text": "yo"}],
        # caption_mode field omitted
    }
    chain = _build_clip_filterchain(clip, aspect="16:9", fade_seconds=0.0)
    assert "fontsize=36" in chain


def test_overlay_caption_effect_renders_segment_style_in_tiktok_mode():
    """add_caption overlay text (e.g. 'PENTAKILL') is hand-written
    branding, not transcript content — must always render bottom-center
    segment-style even when the clip is in tiktok mode."""
    from ai_video_editor.compile import _build_clip_filterchain

    clip = {
        "id": "x",
        "start_seconds": 0.0,
        "end_seconds": 5.0,
        "effects": [{"kind": "caption", "text": "PENTAKILL"}],
        "caption_segments": [],
        "caption_mode": "tiktok",
    }
    chain = _build_clip_filterchain(clip, aspect="16:9", fade_seconds=0.0)
    assert "text='PENTAKILL'" in chain
    assert "fontsize=36" in chain  # rendered with the classic renderer
