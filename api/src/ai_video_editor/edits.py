"""Per-clip editing primitives — Phase 3, Milestone A.

Three ops: zoom (crop+scale on an ROI), caption (burn transcript text),
focus (spotlight: dim everything outside a circle). Each is one
ffmpeg invocation built from validated inputs (no shell, no user-raw
args). Pure parts (aspect/ROI/caption filter builders) are unit-tested
without ffmpeg.

Aspect is configurable per render — `"16:9"` is a passthrough,
`"9:16"` does a center crop + scale to 720x1280 for TikTok/Reels.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import settings

# ROI presets are expressed as ffmpeg crop-friendly fractional
# expressions (iw/ih) so they auto-scale to any source resolution.
# Each maps to "(out_w, out_h, x, y)" expression fragments.
_ROI_PRESETS: dict[str, tuple[str, str, str, str]] = {
    # Whole frame (no crop) — useful as a sentinel when only zoom_factor matters
    "full": ("iw", "ih", "0", "0"),
    # Center: width/factor x height/factor at the middle (factor injected later)
    "center": ("iw/{f}", "ih/{f}", "(iw-iw/{f})/2", "(ih-ih/{f})/2"),
    # LoL HUD scoreline (top-right — team kills + personal KDA + clock).
    # Updated 2026-05-27 from real screenshot — modern League HUD has the
    # scoreline in the top-right, not top-center (which is reserved for
    # objective notifications).
    "scoreline_lol": ("iw*0.20", "ih*0.05", "iw*0.78", "0"),
    # LoL minimap (bottom-right square)
    "minimap_lol": ("ih*0.22", "ih*0.22", "iw-ih*0.23", "ih*0.77"),
    # LoL champion portrait + summoner spells (bottom-left).
    # Coords mirror profiles/league.toml [regions.champion_portrait].
    "champion_portrait_lol": ("iw*0.07", "ih*0.14", "iw*0.16", "ih*0.86"),
    # LoL killfeed (top-right). Mirrors profiles/league.toml [regions.killfeed].
    "killfeed_lol": ("iw*0.22", "ih*0.28", "iw*0.78", "ih*0.08"),
    # LoL item bar (bottom-center six slots + trinket).
    # Mirrors profiles/league.toml [regions.item_bar].
    "item_bar_lol": ("iw*0.10", "ih*0.08", "iw*0.55", "ih*0.88"),
    # Twitch stream-overlay cam region for League streamers — cam sits
    # to the LEFT of the in-game minimap (which occupies the very bottom-
    # right corner). Right edge stops at x=0.85 (minimap's left edge).
    # Tune the values for your specific OBS layout if the cam doesn't fill
    # the zoom cleanly — change `_ROI_PRESETS["streamcam_lol"]` here.
    "streamcam_lol": ("iw*0.19", "ih*0.22", "iw*0.66", "ih*0.78"),
}


def aspect_filter(aspect: str) -> str:
    """Filter tail to convert the working stream to the target aspect.

    `"16:9"` (default) is a no-op. `"9:16"` does a centered crop to 9:16
    and scales to a 720x1280 mobile frame (TikTok/Reels canonical).
    """
    if aspect == "9:16":
        return "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=720:1280"
    return ""  # 16:9 / passthrough


def resolve_roi(roi: str | dict, factor: float) -> tuple[str, str, str, str]:
    """Return ffmpeg (w, h, x, y) expressions for the requested ROI.

    `roi` may be a preset name (`"center"`, `"scoreline_lol"`,
    `"minimap_lol"`, `"full"`) or an explicit dict
    `{"x": frac, "y": frac, "w": frac, "h": frac}` where each fraction
    is 0..1 of the source dimensions.
    """
    if isinstance(roi, dict):
        x, y, w, h = (float(roi[k]) for k in ("x", "y", "w", "h"))
        return (f"iw*{w}", f"ih*{h}", f"iw*{x}", f"ih*{y}")
    preset = _ROI_PRESETS.get(roi)
    if preset is None:
        raise ValueError(f"unknown ROI preset {roi!r}")
    return tuple(p.format(f=factor) for p in preset)  # type: ignore[return-value]


def _escape_drawtext(text: str) -> str:
    """Sanitise text for ffmpeg's drawtext inside ``text='...'``.

    ffmpeg's filter-graph quoting is famously hard to escape correctly
    (especially for apostrophes — `\\'` doesn't work inside a single-
    quoted value). We sidestep the trickiest cases instead of trying to
    out-escape the parser:

    - **Apostrophes** are replaced with the typographically-identical
      curly apostrophe (U+2019), which needs no escaping at any level.
    - **Colons** and **commas** are filter-graph separators and must be
      backslash-escaped.
    - **Backslashes** in transcripts almost certainly came from a bad
      OCR-ish artefact; drop them.
    """
    return (
        text.replace("\\", "")
        .replace("'", "’")  # noqa: RUF001 (intentional curly quote)
        .replace(":", r"\:")
        .replace(",", r"\,")
    )


def _escape_fontfile_path(path: str) -> str:
    """Normalise a font path for drawtext's `fontfile=` argument.

    Empirically required for Windows: BOTH single-quote the value
    (`caption_filters` does that) AND backslash-escape the drive-letter
    colon. Either alone isn't enough — ffmpeg's parser strips the
    quotes before applying its colon-as-separator rule, so without the
    escape it bails at `fontfile=C` and treats the rest as a new option.
    """
    return path.replace("\\", "/").replace(":", r"\:")


# ----- unified caption renderer ----------------------------------------
#
# One renderer for ALL captions. Each segment carries an optional
# `style` field. The renderer reads the style, merges preset + overrides,
# and emits a single `drawtext` filter per segment.
#
# Style presets are visual recipes — a named bundle of (fontsize,
# y-position, colors, border). Adding a new look (title cards,
# subtitles, per-speaker color) means adding a preset to STYLE_PRESETS
# below; nothing else changes. Individual `style` fields override the
# preset's defaults so power users can tweak without forking a preset.
#
# "TikTok-mode" is now a DATA TRANSFORMATION (explode segments into
# word-segments + tag each with style.preset="tiktok"), not a separate
# code path. Same renderer; just different shape of input.


# Default visual recipe — bottom-center "Netflix-like" captions.
_DEFAULT_STYLE: dict = {
    "fontsize": 36,
    "y_position": "h-th-60",  # ffmpeg expr — bottom with 60px margin
    "color": "white",
    "border_width": 4,
    "border_color": "black",
    "min_duration_seconds": 0.10,  # never let a segment vanish faster than this
}

# TikTok-style hero text — top-center, big bold white-on-black border.
_TIKTOK_STYLE: dict = {
    "fontsize": 80,
    "y_position": "h/8",
    "color": "white",
    "border_width": 6,
    "border_color": "black",
    "min_duration_seconds": 0.05,
}

STYLE_PRESETS: dict[str, dict] = {
    "default": _DEFAULT_STYLE,
    "tiktok": _TIKTOK_STYLE,
}


def resolve_caption_style(style: dict | None) -> dict:
    """Merge a per-segment `style` dict on top of its preset.

    `style` may be None (use default preset), or a dict containing
    `preset` plus any individual overrides. Unknown presets fall
    back to default. Returns a flat dict the renderer reads from.
    """
    if not style:
        return dict(_DEFAULT_STYLE)
    preset_name = style.get("preset") or "default"
    base = dict(STYLE_PRESETS.get(preset_name, _DEFAULT_STYLE))
    for key, value in style.items():
        if key == "preset" or value is None:
            continue
        base[key] = value
    return base


def caption_filters(
    segments: list[dict],
    clip_start_offset: float,
    *,
    fontfile: str | None = None,
) -> str:
    """Build a `drawtext` chain — one filter per segment.

    `clip_start_offset` is the source-VOD time at which the OUTPUT clip
    begins; we subtract it so the `enable=between(t,a,b)` times are
    relative to the clip's own timeline (which starts at 0).

    Each segment's `style` field (optional) picks the preset and any
    per-segment overrides. Segments with no style use `default`.

    This function REPLACES the old `caption_filters` + `caption_filters_tiktok`
    pair — both modes are now driven by the same code with different
    `style` values on the segments.
    """
    if fontfile is None:
        fontfile = settings.caption_font_path
    ff = _escape_fontfile_path(fontfile)
    parts: list[str] = []
    for s in segments or []:
        text = _escape_drawtext((s.get("text") or "").strip())
        if not text:
            continue
        style = resolve_caption_style(s.get("style"))
        min_dur = float(style.get("min_duration_seconds") or 0.1)
        a = max(0.0, float(s["start_seconds"]) - clip_start_offset)
        b = max(a + min_dur, float(s["end_seconds"]) - clip_start_offset)
        parts.append(
            "drawtext="
            f"fontfile='{ff}'"  # single-quote so Windows 'C:/...' stays literal
            f":text='{text}'"
            f":fontsize={int(style['fontsize'])}:fontcolor={style['color']}"
            f":borderw={int(style['border_width'])}:bordercolor={style['border_color']}"
            f":x=(w-text_w)/2:y={style['y_position']}"
            f":enable='between(t,{a:.2f},{b:.2f})'"
        )
    return ",".join(parts)


def _segment_words_with_fallback(seg: dict) -> list[dict]:
    """Per-word timings for a segment, falling back to even-split when
    the segment doesn't carry `words` (pre-word-timestamps transcripts).

    Used by `explode_segments_to_words` and the legacy migration path
    in `_build_clip_filterchain`. Returns `[{word, start, end}, ...]`
    in clip-source seconds. Empty when no text.
    """
    words = seg.get("words")
    if words:
        return [
            {"word": w["word"], "start": float(w["start"]), "end": float(w["end"])}
            for w in words
            if (w.get("word") or "").strip()
        ]
    text = (seg.get("text") or "").strip()
    if not text:
        return []
    tokens = text.split()
    if not tokens:
        return []
    seg_a = float(seg["start_seconds"])
    seg_b = float(seg["end_seconds"])
    span = max(0.001, seg_b - seg_a)
    step = span / len(tokens)
    return [
        {"word": tok, "start": seg_a + i * step, "end": seg_a + (i + 1) * step}
        for i, tok in enumerate(tokens)
    ]


def explode_segments_to_words(
    segments: list[dict], *, style_preset: str = "tiktok"
) -> list[dict]:
    """Transform a segment list into per-WORD segments with a style preset.

    Uses each segment's `words[]` when available (Whisper word timings),
    falls back to even-split timing across the segment span otherwise.
    The output is a flat list of segments — input shape PLUS a per-word
    granularity AND a style tag. Renderer treats them like any other
    segments.

    This IS the implementation of "switch to TikTok mode" — call it
    on a clip's caption_segments to get the explode + style effect.
    """
    out: list[dict] = []
    for seg in segments or []:
        for w in _segment_words_with_fallback(seg):
            text = (w.get("word") or "").strip()
            if not text:
                continue
            out.append(
                {
                    "start_seconds": float(w["start"]),
                    "end_seconds": float(w["end"]),
                    "text": text,
                    "style": {"preset": style_preset},
                }
            )
    return out


# Legacy alias — tests + any external callers still using the old name.
# The unified `caption_filters` reads segment.style, so to reproduce the
# legacy "tiktok" output we explode segments + tag them before rendering.
def caption_filters_tiktok(
    segments: list[dict],
    clip_start_offset: float,
    *,
    fontfile: str | None = None,
) -> str:
    """DEPRECATED: legacy alias for `caption_filters` with TikTok-style
    explosion. New callers should pass already-exploded + styled segments
    to `caption_filters` directly."""
    exploded = explode_segments_to_words(segments, style_preset="tiktok")
    return caption_filters(exploded, clip_start_offset, fontfile=fontfile)


def _run_ffmpeg(cmd: list[str]) -> tuple[bool, str | None]:
    Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return False, "ffmpeg not found"
    if result.returncode != 0:
        # Keep the TAIL of stderr — that's where ffmpeg's actual error
        # message lives (it writes a long banner first).
        return False, result.stderr[-1500:]
    return True, None


def _src_args(asset_path: str, start: float, end: float) -> list[str]:
    """Trim flags so the effect operates on the requested sub-range in
    a single ffmpeg pass (no separate trim step)."""
    dur = max(0.0, end - start)
    return ["-ss", str(start), "-i", asset_path, "-t", str(dur)]


def apply_zoom(
    asset_path: str,
    out_path: str,
    *,
    start: float,
    end: float,
    factor: float,
    roi: str | dict,
    aspect: str,
) -> tuple[bool, str | None]:
    w, h, x, y = resolve_roi(roi, factor)
    chain = [f"crop={w}:{h}:{x}:{y}", "scale=iw*2:ih*2"]  # upscale post-crop
    tail = aspect_filter(aspect)
    if tail:
        chain.append(tail)
    cmd = [
        settings.ffmpeg_path,
        "-y",
        *_src_args(asset_path, start, end),
        "-vf",
        ",".join(chain),
        "-c:a",
        "copy",
        out_path,
    ]
    return _run_ffmpeg(cmd)


def apply_caption(
    asset_path: str,
    out_path: str,
    *,
    start: float,
    end: float,
    segments: list[dict],
    aspect: str,
) -> tuple[bool, str | None]:
    chain = [caption_filters(segments, clip_start_offset=start)]
    tail = aspect_filter(aspect)
    if tail:
        chain.append(tail)
    vf = ",".join(p for p in chain if p) or "null"
    cmd = [
        settings.ffmpeg_path,
        "-y",
        *_src_args(asset_path, start, end),
        "-vf",
        vf,
        "-c:a",
        "copy",
        out_path,
    ]
    return _run_ffmpeg(cmd)


def apply_focus(
    asset_path: str,
    out_path: str,
    *,
    start: float,
    end: float,
    x_frac: float,
    y_frac: float,
    r_frac: float,
    dim: float,
    aspect: str,
) -> tuple[bool, str | None]:
    """Spotlight: dim the frame to `dim` brightness everywhere except a
    soft circle at (x_frac, y_frac) of radius `r_frac` x min(iw,ih).
    Implemented with a luminance `geq` — slow per-frame but accurate
    and needs no extra deps."""
    cx = f"iw*{x_frac}"
    cy = f"ih*{y_frac}"
    rr = f"(min(iw,ih)*{r_frac})"
    # Inside circle → 1.0, outside → `dim`. Linear falloff on the edge.
    mask = f"if(lt(hypot(X-{cx},Y-{cy}),{rr}),lum(X,Y),lum(X,Y)*{dim})"
    chain = [f"geq=lum='{mask}':cb=cb:cr=cr"]
    tail = aspect_filter(aspect)
    if tail:
        chain.append(tail)
    cmd = [
        settings.ffmpeg_path,
        "-y",
        *_src_args(asset_path, start, end),
        "-vf",
        ",".join(chain),
        "-c:a",
        "copy",
        out_path,
    ]
    return _run_ffmpeg(cmd)
