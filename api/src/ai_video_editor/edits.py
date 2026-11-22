"""Per-clip editing primitives — Phase 3, Milestone A.

Three ops: zoom (crop+scale on an ROI), caption (burn transcript text),
focus (spotlight: dim everything outside a circle). Each is one
ffmpeg invocation built from validated inputs (no shell, no user-raw
args). Pure parts (aspect/ROI/caption filter builders) are unit-tested
without ffmpeg.

Aspect is configurable per render — `"16:9"` is a passthrough,
`"9:16"` does a center crop + scale to 720x1280 for TikTok/Reels.
`"9:16_hud"` (flag-gated via `HUD_ASPECT_ENABLED`) runs zoom/focus
then the existing `cropped_hud_9x16` layout so minimap/scoreline survive;
captions are burned after that layout in output-frame coordinates.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import settings

# Flag-gated vertical aspect that preserves HUD via cropped_hud_9x16.
HUD_ASPECT = "9:16_hud"
# Top-band caption y for 9:16_hud — output-frame coords (720x1280) after
# the HUD layout. Must clear the LoL scoreline overlay (see
# `lol_scoreline_overlay_rect`); `h/8` (~160) overlapped it. Default
# bottom caption `h-th-60` lands on the ability bar / minimap.
HUD_CAPTION_Y = "40"

# LoL scoreline overlay geometry on the 720x1280 HUD frame (must stay
# in sync with `_cropped_hud_lol`). Width matches the scale=320 strip;
# height is the source ROI (422x108) after that scale.
_HUD_SCORELINE_WIDTH_PX = 320
_HUD_SCORELINE_HEIGHT_PX = 82  # ROI 422x108 scaled to width 320
_HUD_SCORELINE_MARGIN_X = 16

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
    # Valorant killfeed (top-right) — mirrors profiles/valorant.toml
    # [regions.killfeed]. Wide + tall enough to catch multi-row kill
    # announcements.
    "killfeed_val": ("iw*0.22", "ih*0.28", "iw*0.78", "ih*0.08"),
    # Valorant minimap (TOP-left — unlike LoL's bottom-right). Mirrors
    # profiles/valorant.toml [regions.minimap].
    "minimap_val": ("iw*0.20", "ih*0.30", "iw*0.01", "ih*0.02"),
    # Valorant scoreboard bar (top-center: round score + timer + team
    # alive count). Mirrors profiles/valorant.toml [regions.scoreboard].
    "scoreboard_val": ("iw*0.40", "ih*0.07", "iw*0.30", "0"),
}


def aspect_filter(aspect: str) -> str:
    """Filter tail to convert the working stream to the target aspect.

    `"16:9"` (default) is a no-op. `"9:16"` does a centered crop to 9:16
    and scales to a 720x1280 mobile frame (TikTok/Reels canonical).

    `"9:16_hud"` is NOT handled here — it needs `-filter_complex` via
    `build_hud_aspect_filter_complex`. Callers must branch before using
    this helper.
    """
    if aspect == "9:16":
        return "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=720:1280"
    return ""  # 16:9 / passthrough / unknown


def validate_edit_aspect(aspect: str, *, hud_enabled: bool) -> None:
    """Raise ValueError when `aspect` is not allowed under current flags.

    Pure — flag is injected by the caller (router / apply_*). Does not
    read settings. Legacy aspects are always allowed.
    """
    if aspect == HUD_ASPECT and not hud_enabled:
        raise ValueError(
            "aspect '9:16_hud' requires HUD_ASPECT_ENABLED=true "
            "(set the flag in config / .env to enable the HUD layout)"
        )


def hud_caption_segments(segments: list[dict]) -> list[dict]:
    """Copy segments, injecting top-band `y_position` unless already set.

    Explicit `style.y_position` always wins. Used only for `9:16_hud` so
    default bottom captions do not land on the ability bar / minimap.
    """
    out: list[dict] = []
    for s in segments or []:
        s = dict(s)
        style = dict(s.get("style") or {})
        if "y_position" not in style:
            style["y_position"] = HUD_CAPTION_Y
            s["style"] = style
        out.append(s)
    return out


def zoom_upscale_filter(*, scale_to_1080p: bool, aspect: str) -> str:
    """Post-crop scale fragment for zoom. Empty string = skip upscale.

    Legacy (flag off): always `scale=iw*2:ih*2`. That only lands on
    1920x1080 when factor is 2.0 with a center crop of a 1080p source —
    factor 1.5 yields 2560x1440, so later fixed-fontsize captions look
    tiny. Flag on: scale the cropped frame to height 1080 (width
    auto, lanczos). For `9:16` / `9:16_hud`, return "" — those paths
    already target 720x1280 via aspect_filter or cropped_hud_9x16.
    """
    if not scale_to_1080p:
        return "scale=iw*2:ih*2"
    if aspect in ("9:16", HUD_ASPECT):
        return ""
    return "scale=-2:1080:flags=lanczos"


# Base 9:16 output resolution — TikTok / Reels / Shorts canonical.
_SHORT_WIDTH = 720
_SHORT_HEIGHT = 1280


def build_hud_aspect_filter_complex(
    effect_vf: str,
    *,
    game: str | None = None,
    caption_vf: str = "",
) -> str:
    """Chain zoom/focus → `cropped_hud_9x16` → optional caption drawtext.

    Pure filter builder — reuses the existing HUD layout; does not
    duplicate overlay geometry.

    Order matters:
      - `effect_vf` (crop/scale, geq, …) runs BEFORE the HUD layout so
        zoom/focus still operate in source coordinates.
      - `caption_vf` (drawtext) runs AFTER the layout's ``[out]`` so
        ``y_position`` / ``HUD_CAPTION_Y`` are 720x1280 output-frame
        coords and captions are not copied into the blur/HUD crops.

    Empty / ``"null"`` for either side skips that stage. Entry is still
    driven from input 0; exit label is ``[out]``.
    """
    hud = cropped_hud_9x16(game=game)
    if effect_vf and effect_vf != "null":
        # Apply the effect once, then feed that stream into the HUD graph
        # (which otherwise starts at [0:v]).
        graph = f"[0:v]{effect_vf}[fx];" + hud.replace("[0:v]", "[fx]", 1)
    else:
        graph = hud
    if caption_vf and caption_vf != "null":
        if not graph.endswith("[out]"):
            raise ValueError("HUD graph must end with [out] before caption")
        # Relabel layout exit, then burn captions on the composed frame.
        graph = graph[: -len("[out]")] + "[hud]"
        graph = f"{graph};[hud]{caption_vf}[out]"
    return graph


def cropped_hud_9x16(
    zoom_out: float | None = None,
    game: str | None = None,
) -> str:
    """9:16 layout that trades a tighter play area for OVERLAID HUD.

    Dispatches to a per-game composer. The default (game=None or LoL)
    keeps the current League behavior — scoreline top-right, minimap
    bottom-right. Valorant swaps in the killfeed (top-right) and moves
    the minimap to match Val's HUD (top-left).

    Reads `settings.shorts_zoom_out` for the crop width multiplier
    when not overridden per-call. Filter graph shape: entry `[0:v]`,
    exit `[out]`, `filter_complex`.
    """
    resolved = _hud_game(game)
    if resolved == "valorant":
        return _cropped_hud_valorant(zoom_out)
    return _cropped_hud_lol(zoom_out)


def _hud_game(game: str | None) -> str:
    """Normalize a game name to a HUD composer key.

    Handles fuzzy game names (e.g. "League of Legends_..." session
    folder-style strings) via substring match — mirrors the pattern
    used in `vlm/prompts.py::load_game_hints`.
    """
    if not game:
        return "league_of_legends"
    g = game.lower().strip()
    if "valorant" in g:
        return "valorant"
    # Default to LoL for backward compatibility.
    return "league_of_legends"


def lol_scoreline_overlay_rect(
    zoom_out: float | None = None,
) -> tuple[int, int, int, int]:
    """LoL scoreline overlay (x, y, w, h) on the 720x1280 HUD frame.

    Pure helper mirroring `_cropped_hud_lol` geometry so callers (and
    tests) can reason about caption clearance without duplicating the
    letterbox math. Position: bottom edge sits just above the sharp
    play area (in the blurred letterbox). Does not change overlay
    placement — `_cropped_hud_lol` is the sole writer of those filters.
    """
    from .config import settings

    factor = float(zoom_out) if zoom_out is not None else float(settings.shorts_zoom_out)
    letterbox_top = 640.0 * (1.0 - 1.0 / factor) if factor > 0 else 0.0
    y = max(4, round(letterbox_top - _HUD_SCORELINE_HEIGHT_PX - 4))
    x = _SHORT_WIDTH - _HUD_SCORELINE_WIDTH_PX - _HUD_SCORELINE_MARGIN_X
    return (x, y, _HUD_SCORELINE_WIDTH_PX, _HUD_SCORELINE_HEIGHT_PX)


def _cropped_hud_lol(zoom_out: float | None) -> str:
    """LoL variant — scoreline top-right + minimap bottom-right."""
    from .config import settings

    factor = float(zoom_out) if zoom_out is not None else float(settings.shorts_zoom_out)
    crop_w = f"ih*9/16*{factor:.4f}"
    _, scoreline_y, _, _ = lol_scoreline_overlay_rect(factor)
    return (
        "[0:v]split=4[main][blur_src][sl][mm];"
        f"[main]crop={crop_w}:ih:(iw-{crop_w})/2:0,"
        "scale=720:-2:flags=lanczos,setsar=1[fg];"
        "[blur_src]scale=720:1280:force_original_aspect_ratio=increase:"
        "flags=lanczos,crop=720:1280,boxblur=luma_radius=25:luma_power=2,"
        "setsar=1[bg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[main_composed];"
        "[sl]crop=iw*0.22:ih*0.10:iw*0.78:ih*0.005,"
        f"scale={_HUD_SCORELINE_WIDTH_PX}:-1:flags=lanczos,setsar=1[sls];"
        "[mm]crop=ih*0.22:ih*0.22:iw-ih*0.23:ih*0.77,"
        "scale=240:240:flags=lanczos,setsar=1[mms];"
        f"[main_composed][sls]overlay=W-w-{_HUD_SCORELINE_MARGIN_X}:{scoreline_y}[step1];"
        "[step1][mms]overlay=W-w-16:H-h-40[out]"
    )


def _cropped_hud_valorant(zoom_out: float | None) -> str:
    """Valorant variant — killfeed top-right (BIG + readable) +
    minimap top-left.

    Val's HUD is very different from LoL's:
      - Killfeed top-right, sized big enough that player names are
        legible (that's how the viewer knows the user got the kill).
        Positioned lower so it bleeds into the sharp play area a bit
        — better than losing it in the blurred letterbox.
      - Minimap top-LEFT (Val convention — mirrors valorant.toml).
      - We skip a separate scoreboard overlay for MVP — the top-center
        Val score/timer is thin and would clash with the killfeed on
        the same row.
    """
    from .config import settings

    factor = float(zoom_out) if zoom_out is not None else float(settings.shorts_zoom_out)
    crop_w = f"ih*9/16*{factor:.4f}"

    # Killfeed strip position:
    # Move it DOWN into the sharp play area so the names are legible
    # over gameplay (rather than getting lost in the blurred letterbox
    # at the very top). Lower + slightly right-of-edge margin.
    killfeed_y = 110  # ~1/12 down the 1280 vertical

    # Minimap: TOP-LEFT for Valorant. Scale to 200 wide.
    minimap_x = 12
    minimap_y = 12
    return (
        "[0:v]split=4[main][blur_src][kf][mm];"
        # Main: zoom-out center crop, scaled to 720 wide.
        f"[main]crop={crop_w}:ih:(iw-{crop_w})/2:0,"
        "scale=720:-2:flags=lanczos,setsar=1[fg];"
        # Blurred fill same as LoL.
        "[blur_src]scale=720:1280:force_original_aspect_ratio=increase:"
        "flags=lanczos,crop=720:1280,boxblur=luma_radius=25:luma_power=2,"
        "setsar=1[bg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[main_composed];"
        # Killfeed strip — tighter crop of the source (top 2-3 rows
        # only, h=0.12 vs the full 0.28 ROI) so the killfeed feed shows
        # just the freshest kills. Scale to 420 wide (was 320) for
        # readable player names — height auto-preserves aspect.
        "[kf]crop=iw*0.22:ih*0.12:iw*0.78:ih*0.08,"
        "scale=420:-1:flags=lanczos,setsar=1[kfs];"
        # Minimap — Val's minimap ROI (top-left, rectangular).
        "[mm]crop=iw*0.20:ih*0.30:iw*0.01:ih*0.02,"
        "scale=200:-1:flags=lanczos,setsar=1[mms];"
        # Killfeed top-right of the vertical output, positioned lower
        # so it overlays a bit onto the sharp play area (names legible).
        f"[main_composed][kfs]overlay=W-w-16:{killfeed_y}[step1];"
        # Minimap top-left.
        f"[step1][mms]overlay={minimap_x}:{minimap_y}[out]"
    )


# Labels that appear inside the two 9:16 filter graphs above. When the
# shorts pipeline concats multiple clips into one output, each per-clip
# instance of the graph needs its internal labels suffixed with the
# clip index. Callers use these constants to do that rewrite.
BLUR_FILL_9X16_LABELS: tuple[str, ...] = ("fga", "bga", "fg", "bg")

# LoL cropped_hud labels (scoreline `sl`/`sls`, minimap bottom-right).
CROPPED_HUD_9X16_LABELS_LOL: tuple[str, ...] = (
    "main",
    "blur_src",
    "sl",
    "mm",
    "fg",
    "bg",
    "main_composed",
    "sls",
    "mms",
    "step1",
)

# Valorant cropped_hud labels (killfeed `kf`/`kfs`, minimap top-left).
CROPPED_HUD_9X16_LABELS_VAL: tuple[str, ...] = (
    "main",
    "blur_src",
    "kf",
    "mm",
    "fg",
    "bg",
    "main_composed",
    "kfs",
    "mms",
    "step1",
)


def cropped_hud_labels_for_game(game: str | None) -> tuple[str, ...]:
    """Pick the right label set for the game's cropped_hud variant."""
    if _hud_game(game) == "valorant":
        return CROPPED_HUD_9X16_LABELS_VAL
    return CROPPED_HUD_9X16_LABELS_LOL


# Back-compat alias — old callers import this name. Points at LoL for
# backward-compat (LoL was the ONLY variant before this refactor).
CROPPED_HUD_9X16_LABELS = CROPPED_HUD_9X16_LABELS_LOL


def blur_fill_9x16() -> str:
    """Convert a 16:9 source into 9:16 WITHOUT cropping — the full frame
    stays visible in the vertical center; a blurred, scaled copy of the
    same frame fills the top+bottom bars.

    Preferred over `aspect_filter("9:16")` for gameplay footage where
    edge HUD elements matter (League killfeed top-right, minimap
    bottom-right, portrait bottom-left, item bar bottom-center). The
    crop-based aspect filter chops those off; this one preserves them.

    Emits a `filter_complex`-shaped chain (uses labels + a `split`), so
    callers must use ffmpeg's `-filter_complex` flag, NOT `-vf`. The
    output label is `[out]`.

    Filter graph:
      [0:v]split=2 -> [fg][bg]
      [fg]  scale=720:-2 (preserves 16:9 aspect, ~720x405)
      [bg]  scale=720:1280 (fills, then boxblur)
      overlay[fg] onto [bg] centered

    Reference: `.claude/skills/editing-expert/SKILL.md:41-44` for the
    same shape (kept in the skill for future callers).
    """
    return (
        f"[0:v]split=2[fga][bga];"
        f"[fga]scale={_SHORT_WIDTH}:-2:flags=lanczos,setsar=1[fg];"
        f"[bga]scale={_SHORT_WIDTH}:{_SHORT_HEIGHT}:"
        f"force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={_SHORT_WIDTH}:{_SHORT_HEIGHT},"
        f"boxblur=luma_radius=25:luma_power=2,setsar=1[bg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[out]"
    )


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


def _wrap_text(text: str, fontsize: int, max_width: int, max_lines: int = 3) -> tuple[str, int]:
    """Wrap text onto multiple lines if needed, then shrink if still too wide.

    Returns (wrapped_text, final_fontsize). Lines are separated by newlines
    for ffmpeg drawtext's multi-line support.

    Pure function: no settings access.

    **Never drops words.** If text cannot fit in max_lines at min fontsize,
    keeps shrinking below 24px (hard floor 16px). As a last resort, returns
    all text even if it overflows.
    """
    char_width_ratio = 0.6
    hard_floor_fontsize = 16

    def line_width(line: str, fs: int) -> float:
        return len(line) * fs * char_width_ratio

    words = text.split()
    if not words:
        return text, fontsize

    def wrap_at_fontsize(fs: int) -> list[str]:
        lines: list[str] = []
        current: list[str] = []
        for word in words:
            test_line = " ".join([*current, word])
            if line_width(test_line, fs) <= max_width:
                current.append(word)
            else:
                if current:
                    lines.append(" ".join(current))
                current = [word]
        if current:
            lines.append(" ".join(current))
        return lines

    lines = wrap_at_fontsize(fontsize)
    if len(lines) <= max_lines:
        max_line_width = max(line_width(ln, fontsize) for ln in lines)
        if max_line_width <= max_width:
            return "\n".join(lines), fontsize

    for fs in range(fontsize - 1, hard_floor_fontsize - 1, -1):
        lines = wrap_at_fontsize(fs)
        if len(lines) <= max_lines:
            max_line_width = max(line_width(ln, fs) for ln in lines)
            if max_line_width <= max_width:
                return "\n".join(lines), fs

    lines = wrap_at_fontsize(hard_floor_fontsize)
    return "\n".join(lines), hard_floor_fontsize


def scale_fontsize_for_output(
    fontsize: int,
    text: str,
    output_height: int,
    output_width: int,
    *,
    scale_enabled: bool,
) -> tuple[str, int]:
    """Scale fontsize relative to 1080px reference and wrap/clamp to fit frame.

    Pure function: `scale_enabled` is passed in by the caller (resolved from
    settings at the entry point).

    When scale_enabled is False, returns (text, fontsize) unchanged.
    When True:
      1. Scale by output_height / 1080 (reference height).
      2. Wrap onto up to 3 lines if needed.
      3. Shrink fontsize as a last resort (min 24px preferred, hard floor 16px).
      4. Never drops words — keeps all text even if it overflows.

    Returns (possibly_wrapped_text, final_fontsize).
    """
    if not scale_enabled:
        return text, fontsize

    ref_height = 1080
    scale_factor = output_height / ref_height
    scaled = round(fontsize * scale_factor)

    margin_ratio = 0.10
    max_width = int(output_width * (1.0 - 2 * margin_ratio))

    return _wrap_text(text, scaled, max_width, max_lines=3)


def caption_filters(
    segments: list[dict],
    clip_start_offset: float,
    *,
    fontfile: str | None = None,
    output_height: int = 1080,
    output_width: int = 1920,
    scale_enabled: bool = False,
) -> str:
    """Build a `drawtext` chain — one filter per segment.

    `clip_start_offset` is the source-VOD time at which the OUTPUT clip
    begins; we subtract it so the `enable=between(t,a,b)` times are
    relative to the clip's own timeline (which starts at 0).

    Each segment's `style` field (optional) picks the preset and any
    per-segment overrides. Segments with no style use `default`.

    `output_height` and `output_width` are the actual output dimensions.
    For 9:16_hud that is 720x1280. When `scale_enabled` is True, fontsize
    scales relative to a 1080px reference height and text is wrapped/clamped
    to fit the frame. When False (default), fontsize is used as-is for
    backward compatibility.

    This function REPLACES the old `caption_filters` + `caption_filters_tiktok`
    pair — both modes are now driven by the same code with different
    `style` values on the segments.
    """
    if fontfile is None:
        fontfile = settings.caption_font_path
    default_ff = _escape_fontfile_path(fontfile)
    parts: list[str] = []
    for s in segments or []:
        raw_text = (s.get("text") or "").strip()
        if not raw_text:
            continue
        style = resolve_caption_style(s.get("style"))
        raw_fontsize = int(style["fontsize"])
        processed_text, fontsize = scale_fontsize_for_output(
            raw_fontsize, raw_text, output_height, output_width, scale_enabled=scale_enabled
        )
        escaped_text = _escape_drawtext(processed_text)
        seg_font = s.get("font_path")
        ff = _escape_fontfile_path(seg_font) if seg_font else default_ff
        min_dur = float(style.get("min_duration_seconds") or 0.1)
        a = max(0.0, float(s["start_seconds"]) - clip_start_offset)
        b = max(a + min_dur, float(s["end_seconds"]) - clip_start_offset)
        parts.append(
            "drawtext="
            f"fontfile='{ff}'"  # single-quote so Windows 'C:/...' stays literal
            f":text='{escaped_text}'"
            f":fontsize={fontsize}:fontcolor={style['color']}"
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


def explode_segments_to_words(segments: list[dict], *, style_preset: str = "tiktok") -> list[dict]:
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
    output_height: int = 1080,
    output_width: int = 1920,
    scale_enabled: bool = False,
) -> str:
    """DEPRECATED: legacy alias for `caption_filters` with TikTok-style
    explosion. New callers should pass already-exploded + styled segments
    to `caption_filters` directly."""
    exploded = explode_segments_to_words(segments, style_preset="tiktok")
    return caption_filters(
        exploded,
        clip_start_offset,
        fontfile=fontfile,
        output_height=output_height,
        output_width=output_width,
        scale_enabled=scale_enabled,
    )


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


def _apply_with_hud(
    asset_path: str,
    out_path: str,
    *,
    start: float,
    end: float,
    effect_vf: str,
    game: str | None,
    caption_vf: str = "",
) -> tuple[bool, str | None]:
    """Run zoom/focus → `cropped_hud_9x16` → optional caption via `-filter_complex`."""
    fc = build_hud_aspect_filter_complex(effect_vf, game=game, caption_vf=caption_vf)
    cmd = [
        settings.ffmpeg_path,
        "-y",
        *_src_args(asset_path, start, end),
        "-filter_complex",
        fc,
        "-map",
        "[out]",
        "-map",
        "0:a?",
        "-c:a",
        "copy",
        out_path,
    ]
    return _run_ffmpeg(cmd)


def apply_zoom(
    asset_path: str,
    out_path: str,
    *,
    start: float,
    end: float,
    factor: float,
    roi: str | dict,
    aspect: str,
    game: str | None = None,
    scale_to_1080p: bool = False,
) -> tuple[bool, str | None]:
    w, h, x, y = resolve_roi(roi, factor)
    chain = [f"crop={w}:{h}:{x}:{y}"]
    up = zoom_upscale_filter(scale_to_1080p=scale_to_1080p, aspect=aspect)
    if up:
        chain.append(up)
    if aspect == HUD_ASPECT:
        return _apply_with_hud(
            asset_path,
            out_path,
            start=start,
            end=end,
            effect_vf=",".join(chain),
            game=game,
        )
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
    game: str | None = None,
    scale_enabled: bool = False,
) -> tuple[bool, str | None]:
    segs = hud_caption_segments(segments) if aspect == HUD_ASPECT else segments
    if aspect == HUD_ASPECT or aspect == "9:16":
        out_w, out_h = 720, 1280
    else:
        out_w, out_h = 1920, 1080
    caps = caption_filters(
        segs,
        clip_start_offset=start,
        output_height=out_h,
        output_width=out_w,
        scale_enabled=scale_enabled,
    )
    if aspect == HUD_ASPECT:
        # Caption after layout so HUD_CAPTION_Y / y_position are output coords.
        return _apply_with_hud(
            asset_path,
            out_path,
            start=start,
            end=end,
            effect_vf="null",
            game=game,
            caption_vf=caps or "null",
        )
    chain = [caps]
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
    game: str | None = None,
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
    effect = f"geq=lum='{mask}':cb=cb:cr=cr"
    if aspect == HUD_ASPECT:
        return _apply_with_hud(
            asset_path, out_path, start=start, end=end, effect_vf=effect, game=game
        )
    chain = [effect]
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


def apply_sfx(
    source_path: str,
    out_path: str,
    *,
    sfx: list[dict],
) -> tuple[bool, str | None]:
    """Overlay one or more SFX one-shots onto `source_path`'s audio.

    `sfx` is a list of dicts, each shaping:
      - `sfx_path`: absolute path to a .wav/.mp3/.ogg file
      - `at_seconds`: playback time (source-timeline) to trigger
      - `volume` (optional, default 1.0): 0.0..2.0 linear gain

    Video stream is copied unchanged (no re-encode). Audio is remixed
    via ffmpeg's `adelay` per SFX + a single `amix`. Missing sfx files
    are skipped -- other overlays still land. Returns (False, reason)
    when zero SFX entries are valid so the caller sees the misconfig.

    Extension pattern: the same shape supports SFX libraries later --
    higher layers can resolve names like "vine_boom" to a path in a
    WORKSPACE/sfx/ dir before calling. Keep this primitive path-based.
    """
    valid = [s for s in (sfx or []) if Path(s.get("sfx_path", "")).is_file()]
    if not valid:
        return False, "no valid sfx entries (paths missing?)"

    inputs: list[str] = ["-i", source_path]
    filter_parts: list[str] = []
    mix_ins: list[str] = ["[0:a]"]
    for idx, s in enumerate(valid, start=1):
        inputs.extend(["-i", s["sfx_path"]])
        at_ms = max(0, round(float(s.get("at_seconds") or 0.0) * 1000))
        vol = float(s.get("volume") or 1.0)
        label = f"[sfx{idx}]"
        # adelay=Nms|Nms delays BOTH channels; volume per SFX after that.
        filter_parts.append(f"[{idx}:a]adelay={at_ms}|{at_ms},volume={vol:.3f}{label}")
        mix_ins.append(label)
    # amix -- normalize=0 preserves per-SFX levels; dropout_transition=0
    # keeps gain constant when a track ends.
    filter_parts.append(
        f"{''.join(mix_ins)}amix=inputs={len(mix_ins)}:dropout_transition=0:normalize=0[aout]"
    )
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        settings.ffmpeg_path,
        "-y",
        *inputs,
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        "0:v",
        "-map",
        "[aout]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        out_path,
    ]
    return _run_ffmpeg(cmd)
