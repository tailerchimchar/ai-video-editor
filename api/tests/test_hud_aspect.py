"""9:16_hud aspect — HUD-preserving vertical layout for per-clip edits.

Flag-gated (HUD_ASPECT_ENABLED, default off). Pure filter builders are
tested without ffmpeg; one lavfi integration check verifies 720x1280.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_video_editor.config import settings
from ai_video_editor.edits import (
    _DEFAULT_STYLE,
    HUD_ASPECT,
    HUD_CAPTION_Y,
    apply_caption,
    apply_zoom,
    aspect_filter,
    build_hud_aspect_filter_complex,
    caption_filters,
    hud_caption_segments,
    lol_scoreline_overlay_rect,
    validate_edit_aspect,
    zoom_upscale_filter,
)

# ----- pure unit tests -------------------------------------------------


def test_flag_off_rejects_9_16_hud() -> None:
    with pytest.raises(ValueError, match="HUD_ASPECT_ENABLED"):
        validate_edit_aspect(HUD_ASPECT, hud_enabled=False)


def test_flag_on_allows_9_16_hud() -> None:
    validate_edit_aspect(HUD_ASPECT, hud_enabled=True)  # no raise


def test_existing_aspects_always_allowed() -> None:
    for aspect in ("16:9", "9:16", "source"):
        validate_edit_aspect(aspect, hud_enabled=False)
        validate_edit_aspect(aspect, hud_enabled=True)


def test_aspect_filter_unchanged_for_legacy() -> None:
    """Byte-for-byte: legacy aspects must not grow a HUD branch."""
    assert aspect_filter("16:9") == ""
    assert aspect_filter("9:16") == "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=720:1280"


def test_zoom_upscale_skips_for_9_16_hud_when_1080p_flag_on() -> None:
    """Same skip as plain 9:16 — HUD layout already targets 720x1280."""
    assert zoom_upscale_filter(scale_to_1080p=True, aspect=HUD_ASPECT) == ""
    assert zoom_upscale_filter(scale_to_1080p=True, aspect="9:16") == ""
    # Flag off still keeps legacy 2x even for HUD (matches 9:16 behavior).
    assert zoom_upscale_filter(scale_to_1080p=False, aspect=HUD_ASPECT) == "scale=iw*2:ih*2"


def test_hud_graph_contains_lol_layout_and_effect() -> None:
    effect = "crop=iw/2:ih/2:(iw-iw/2)/2:(ih-ih/2)/2,scale=iw*2:ih*2"
    graph = build_hud_aspect_filter_complex(effect, game="League of Legends")
    assert effect in graph
    assert "split=4" in graph
    # LoL scoreline + minimap ROIs (must reuse cropped_hud, not reinvent)
    assert "iw*0.22:ih*0.10:iw*0.78:ih*0.005" in graph
    assert "ih*0.22:ih*0.22:iw-ih*0.23:ih*0.77" in graph
    assert "[out]" in graph


def test_hud_valorant_variant_chosen() -> None:
    graph = build_hud_aspect_filter_complex("null", game="Valorant")
    # Val killfeed crop + top-left minimap (not LoL scoreline/minimap)
    assert "iw*0.22:ih*0.12:iw*0.78:ih*0.08" in graph
    assert "iw*0.20:ih*0.30:iw*0.01:ih*0.02" in graph
    assert "iw*0.22:ih*0.10:iw*0.78:ih*0.005" not in graph


def test_hud_caption_default_y_is_top_band() -> None:
    assert HUD_CAPTION_Y == "40"
    segs = hud_caption_segments([{"text": "clutch", "start_seconds": 0.0, "end_seconds": 1.0}])
    filt = caption_filters(segs, clip_start_offset=0.0)
    assert f"y={HUD_CAPTION_Y}" in filt
    assert "y=h/8" not in filt
    assert "y=h-th-60" not in filt


def test_default_hud_caption_clears_scoreline_overlay() -> None:
    """Default caption band must not intersect the LoL score HUD rect.

    Uses `lol_scoreline_overlay_rect` (layout's own letterbox math) and
    the default caption fontsize — no hard-coded scoreline y.
    """
    caption_y = int(HUD_CAPTION_Y)
    caption_h = int(_DEFAULT_STYLE["fontsize"])
    caption_bottom = caption_y + caption_h
    _sx, score_y, _sw, score_h = lol_scoreline_overlay_rect()
    # Vertical clearance: caption fully above the scoreline strip.
    assert caption_bottom <= score_y, (
        f"default caption [{caption_y},{caption_bottom}) overlaps "
        f"scoreline [{score_y},{score_y + score_h})"
    )


def test_hud_caption_explicit_y_wins() -> None:
    segs = hud_caption_segments(
        [
            {
                "text": "clutch",
                "start_seconds": 0.0,
                "end_seconds": 1.0,
                "style": {"y_position": "h-th-120"},
            }
        ]
    )
    filt = caption_filters(segs, clip_start_offset=0.0)
    assert "y=h-th-120" in filt
    assert f"y={HUD_CAPTION_Y}" not in filt


def test_hud_caption_drawtext_comes_after_layout_output() -> None:
    """Captions must burn onto the composed 720x1280 frame, not the source."""
    segs = hud_caption_segments(
        [{"text": "placeholder_callout", "start_seconds": 0.0, "end_seconds": 1.0}]
    )
    caps = caption_filters(segs, clip_start_offset=0.0)
    graph = build_hud_aspect_filter_complex("null", game="League of Legends", caption_vf=caps)
    assert "drawtext=" in graph
    # Layout's final overlay (minimap) finishes before drawtext.
    assert graph.index("drawtext=") > graph.rfind("overlay=")
    # No pre-layout [fx] stage when effect is null.
    assert "[fx]" not in graph
    assert graph.endswith("[out]")


def test_hud_zoom_effect_stays_before_layout() -> None:
    """Zoom/focus still run on the source stream before cropped_hud."""
    effect = "crop=iw/2:ih/2:(iw-iw/2)/2:(ih-ih/2)/2,scale=iw*2:ih*2"
    graph = build_hud_aspect_filter_complex(effect, game="League of Legends")
    assert graph.index(effect) < graph.index("split=")
    assert "drawtext=" not in graph


def test_hud_zoom_then_caption_order() -> None:
    effect = "crop=iw/2:ih/2:(iw-iw/2)/2:(ih-ih/2)/2"
    caps = caption_filters(
        hud_caption_segments(
            [{"text": "placeholder_callout", "start_seconds": 0.0, "end_seconds": 0.5}]
        ),
        clip_start_offset=0.0,
    )
    graph = build_hud_aspect_filter_complex(effect, game="League of Legends", caption_vf=caps)
    assert graph.index(effect) < graph.index("split=")
    assert graph.index("drawtext=") > graph.rfind("overlay=")


def test_non_hud_aspect_filter_builders_unchanged() -> None:
    """Plain 9:16 / 16:9 / source stay byte-for-byte outside the HUD path."""
    assert aspect_filter("16:9") == ""
    assert aspect_filter("9:16") == "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=720:1280"
    assert aspect_filter("source") == ""
    # caption_filters itself is aspect-agnostic — no HUD branch leaked in.
    plain = caption_filters(
        [{"text": "hi", "start_seconds": 0.0, "end_seconds": 1.0}],
        clip_start_offset=0.0,
    )
    assert "y=h-th-60" in plain
    assert f"y={HUD_CAPTION_Y}" not in plain or HUD_CAPTION_Y == "h-th-60"


# ----- router ----------------------------------------------------------


def test_zoom_rejects_9_16_hud_when_flag_off(client, asset, monkeypatch) -> None:
    monkeypatch.setattr(settings, "hud_aspect_enabled", False)
    r = client.post(
        "/api/v1/edit/zoom",
        json={
            "asset_id": asset["id"],
            "start_seconds": 0.0,
            "end_seconds": 5.0,
            "aspect": "9:16_hud",
        },
    )
    assert r.status_code == 400
    assert "HUD_ASPECT_ENABLED" in r.json()["detail"]


def test_caption_video_rejects_9_16_hud_when_flag_off(client, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "hud_aspect_enabled", False)
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    # Aspect is validated before the source-file check.
    r = client.post(
        "/api/v1/clips/caption_video",
        json={
            "source_path": str(tmp_path / "missing.mp4"),
            "segments": [{"text": "hi", "start_seconds": 0.0, "end_seconds": 0.5}],
            "aspect": "9:16_hud",
        },
    )
    assert r.status_code == 400
    assert "HUD_ASPECT_ENABLED" in r.json()["detail"]


# ----- integration (real ffmpeg) ---------------------------------------


def _make_lavfi(path: Path, *, w: int = 1920, h: int = 1080, seconds: int = 1) -> None:
    subprocess.run(
        [
            settings.ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={w}x{h}:r=10:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r=8000:cl=mono:d={seconds}",
            "-shortest",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _probe_wh(path: Path) -> tuple[int, int]:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            str(path),
        ],
        text=True,
    ).strip()
    w, h = out.split(",")
    return int(w), int(h)


def test_apply_zoom_9_16_hud_renders_720x1280(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "hud_aspect_enabled", True)
    src = tmp_path / "src_1080p.mp4"
    _make_lavfi(src, w=1920, h=1080, seconds=1)
    out = tmp_path / "out.mp4"
    ok, err = apply_zoom(
        str(src),
        str(out),
        start=0.0,
        end=1.0,
        factor=2.0,
        roi="center",
        aspect=HUD_ASPECT,
        game="League of Legends",
    )
    assert ok, err
    assert _probe_wh(out) == (720, 1280)


def _gray_crop_luma_sum(path: Path, *, y: int, h: int, w: int = 720) -> int:
    """Sum of gray pixel values for one frame crop — cheap caption probe."""
    raw = subprocess.check_output(
        [
            settings.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vf",
            f"crop={w}:{h}:0:{y},format=gray",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-",
        ]
    )
    return sum(raw)


def test_apply_caption_9_16_hud_renders_720x1280_and_top_band(tmp_path, monkeypatch) -> None:
    """Caption at default HUD_CAPTION_Y lands in the top band of 720x1280."""
    monkeypatch.setattr(settings, "hud_aspect_enabled", True)
    src = tmp_path / "src_1080p.mp4"
    _make_lavfi(src, w=1920, h=1080, seconds=1)

    with_cap = tmp_path / "with_cap.mp4"
    ok, err = apply_caption(
        str(src),
        str(with_cap),
        start=0.0,
        end=1.0,
        segments=[
            {
                "text": "PLACEHOLDER",
                "start_seconds": 0.0,
                "end_seconds": 1.0,
                "style": {
                    "fontsize": 48,
                    "color": "white",
                    "border_width": 0,
                    "border_color": "black",
                },
            }
        ],
        aspect=HUD_ASPECT,
        game="League of Legends",
    )
    assert ok, err
    assert _probe_wh(with_cap) == (720, 1280)

    # Same HUD layout, no drawtext (whitespace-only segment is skipped).
    no_cap = tmp_path / "no_cap.mp4"
    ok, err = apply_caption(
        str(src),
        str(no_cap),
        start=0.0,
        end=1.0,
        segments=[{"text": "   ", "start_seconds": 0.0, "end_seconds": 1.0}],
        aspect=HUD_ASPECT,
        game="League of Legends",
    )
    assert ok, err

    # Default y=40; fontsize 48 → glyph spans ~40..88. Crop that strip.
    top_with = _gray_crop_luma_sum(with_cap, y=30, h=70)
    top_without = _gray_crop_luma_sum(no_cap, y=30, h=70)
    assert top_with > top_without + 500, (
        f"expected caption luma in top band; with={top_with} without={top_without}"
    )

    # Play-area mid strip must NOT pick up the caption (the pre-layout bug).
    mid_with = _gray_crop_luma_sum(with_cap, y=500, h=90)
    mid_without = _gray_crop_luma_sum(no_cap, y=500, h=90)
    assert abs(mid_with - mid_without) < 500, (
        f"caption leaked into play area; with={mid_with} without={mid_without}"
    )
