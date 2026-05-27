"""Render an intro to mp4 via a single ffmpeg invocation.

Animation in v1:
- **Solid color background** spanning the configured duration.
- **Logo overlay** with:
  - fade-in (opacity ramp over `fade_in_seconds`)
  - damped sine bounce on the vertical position
  - scale pulse on impact, settling to nominal over the bounce window
- **Optional music** mixed under the visuals with a tail fade-out.

The whole thing compiles to ONE ffmpeg filtergraph so we don't pay
intermediate disk writes. Each visual transform is a separate filter
node connected via labels — easy to extend (e.g. add a glow node)
without re-architecting the pipeline.

Filter-graph diagram:

    color [bg]
                 → [bg][logo_animated] overlay → [out]
    logo → fade → format=rgba → [logo_animated]

Math notes (referenced in filter expressions):

- `t` is the filter-local time in seconds (0 at logo first display).
- Damped sine for bounce:
    y_offset(t) = A * |cos(2π * f * t)| * exp(-k * t)
  where A = bounce_pixels, f = bounce_count / duration, k tuned so the
  oscillation dies out near the end. `|cos|` (not raw sin) keeps the
  motion strictly downward — bouncing on a floor, not floating around.
- Scale-pulse: same damped envelope on a 1.0..pulse_factor multiplier.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..config import settings
from .config import IntroConfig, resolved_logo_path, resolved_music_path
from .storage import intro_output_path


def _resolution_to_dims(resolution: str) -> tuple[int, int]:
    """Parse '1920x1080' → (1920, 1080)."""
    w, h = resolution.lower().split("x")
    return int(w), int(h)


def _bounce_filter_expressions(config: IntroConfig) -> tuple[str, str]:
    """Return (y_offset_expr, scale_expr) as ffmpeg filter expressions.

    `t` is the filter-local time. The expressions are designed so that
    a config with bounce_pixels=0 produces a static centered logo and
    pulse_factor=1.0 disables the scale pulse — no special-casing in
    the caller, the defaults just collapse to a no-op.

    Text-mode logos have no `scale`/`pulse_factor`; the second return
    value is unused for them but the function shape stays consistent
    so callers can share `y_offset_expr` between modes.
    """
    duration = config.duration
    amplitude = config.logo.bounce_pixels
    n = max(1, config.logo.bounce_count)
    f = n / max(0.01, duration)
    k = 2.3 / max(0.01, duration)
    y_offset_expr = f"({amplitude})*abs(cos(2*PI*{f:.4f}*t))*exp(-{k:.4f}*t)"

    # Only image mode has a scale/pulse expression. For text mode we
    # return a sentinel "1.0" the caller can ignore.
    if config.logo.mode != "image":
        return y_offset_expr, "1.0"

    pf = config.logo.pulse_factor
    if pf <= 1.0:
        scale_expr = f"{config.logo.scale:.4f}"
    else:
        env = f"abs(cos(2*PI*{f:.4f}*t))*exp(-{k:.4f}*t)"
        scale_expr = f"({config.logo.scale:.4f})*(1+({pf - 1:.4f})*{env})"
    return y_offset_expr, scale_expr


def _escape_drawtext_text(text: str) -> str:
    """Sanitise a string for drawtext's `text='...'` field.

    Mirrors the escape rules used in `edits._escape_drawtext` for
    transcript captions — drop backslashes, replace `'` with the
    typographic curly quote (U+2019) which needs no escaping, and
    backslash-escape colons + commas (filter-graph separators).
    """
    # The curly apostrophe (U+2019) is intentional — it avoids drawtext's
    # tricky single-quote escaping in filter-graph parsing. See
    # edits._escape_drawtext for the original rationale.
    return text.replace("\\", "").replace("'", "’").replace(":", r"\:").replace(",", r"\,")  # noqa: RUF001


def _escape_fontfile_path(path: str) -> str:
    """Match `edits._escape_fontfile_path` so Windows drive-letter
    colons survive drawtext's filter-graph parser."""
    return path.replace("\\", "/").replace(":", r"\:")


def _image_logo_chain(config: IntroConfig, duration: float, width: int) -> str:
    """Filter-graph fragment for image-mode logos. Output label: [logo]."""
    assert config.logo.mode == "image"
    static_logo_w = f"{width}*{config.logo.scale:.4f}"
    fade = f"fade=in:st=0:d={config.logo.fade_in_seconds:.2f}:alpha=1"
    return (
        f"[0:v]format=rgba,"
        f"scale={static_logo_w}:-1,"
        f"loop=loop=-1:size=1:start=0,"
        f"trim=duration={duration},"
        f"setpts=PTS-STARTPTS,"
        f"{fade}[logo]"
    )


def _text_logo_chain(
    config: IntroConfig, duration: float, width: int, y_offset_expr: str
) -> tuple[str, str]:
    """Filter-graph fragment for text-mode logos.

    Returns (bg_with_text_chain, final_label). The text path doesn't
    use the same [bg][logo]overlay pattern — drawtext draws DIRECTLY
    onto the background source, one filter per letter (so we can
    space them). The whole thing produces `[vid]` directly without
    an intermediate `[logo]` PNG.

    Letter positioning uses a monospace approximation
    (`font_size * 0.6` per char) — not pixel-perfect kerning, but
    good enough for display fonts where letter_spacing is a creative
    knob rather than a typesetting requirement.
    """
    assert config.logo.mode == "text"
    logo = config.logo

    # Choose font: explicit field → caption font default → empty
    # (drawtext fails clearly when not set, better than guessing).
    font_path = logo.font_path or settings.caption_font_path
    ff = _escape_fontfile_path(font_path)

    text = logo.text
    n = len(text)
    if n == 0:
        # Validated at config level (min_length=1), but defensive
        return "[bg]copy[vid]", "vid"

    # Char-width approximation: most display fonts sit between 0.5
    # and 0.7 of fontsize for ALL CAPS. 0.6 is a reasonable midpoint
    # for the brand-text use case. Future: measure via Pillow ImageFont.
    char_w = int(logo.font_size * 0.6) + logo.letter_spacing
    total_w = char_w * n - logo.letter_spacing  # last char has no trailing gap

    # Starting x by alignment (relative to frame width W in ffmpeg expr).
    if logo.alignment == "left":
        start_x_expr = "60"  # padding from left edge
    elif logo.alignment == "right":
        start_x_expr = f"W-{total_w}-60"
    else:  # center
        start_x_expr = f"(W-{total_w})/2"

    # Fade-in alpha expression: 1.0 within fade_in_seconds, ramping
    # from 0 to 1 linearly. Same shape across letters so they all
    # appear together.
    fade_in = config.logo.fade_in_seconds
    alpha_expr = f"if(lt(t\\,{fade_in:.2f})\\,t/{fade_in:.2f}\\,1)" if fade_in > 0 else "1"

    # Build N drawtext filters chained off [bg]. Each letter's `y`
    # expression includes the same bounce, so letters move together.
    base_y = f"(h-text_h)/2-({y_offset_expr})"
    drawtexts: list[str] = []
    for i, ch in enumerate(text):
        escaped = _escape_drawtext_text(ch)
        x_expr = f"{start_x_expr}+{i * char_w}"
        parts = [
            f"fontfile='{ff}'",
            f"text='{escaped}'",
            f"fontsize={logo.font_size}",
            f"fontcolor={logo.font_color}",
            f"alpha='{alpha_expr}'",
            f"x={x_expr}",
            f"y={base_y}",
        ]
        if logo.stroke_width > 0:
            parts.append(f"borderw={logo.stroke_width}")
            parts.append(f"bordercolor={logo.stroke_color}")
        if logo.shadow_offset_x != 0 or logo.shadow_offset_y != 0:
            # drawtext shadowcolor accepts color@alpha
            shadow = f"{logo.shadow_color}@{logo.shadow_alpha:.2f}"
            parts.append(f"shadowx={logo.shadow_offset_x}")
            parts.append(f"shadowy={logo.shadow_offset_y}")
            parts.append(f"shadowcolor={shadow}")
        drawtexts.append("drawtext=" + ":".join(parts))

    chain = ",".join(drawtexts)
    full_chain = f"[bg]{chain}[vid]"
    return full_chain, "vid"


def _build_filtergraph(config: IntroConfig, has_music: bool) -> str:
    """Compose the full -filter_complex string. Dispatches on logo.mode."""
    width, height = _resolution_to_dims(config.resolution)
    duration = config.duration
    y_offset_expr, _scale_expr = _bounce_filter_expressions(config)

    # Background generator — same for both modes.
    bg = f"color=c={config.background.color}:s={width}x{height}:d={duration}:r=30[bg]"

    if config.logo.mode == "image":
        # Image path: scale-pulse + overlay chain (legacy v1 design).
        logo_chain = _image_logo_chain(config, duration, width)
        overlay_y = f"(H-h)/2-({y_offset_expr})"
        overlay = f"[bg][logo]overlay=x=(W-w)/2:y={overlay_y}:shortest=1[vid]"
        parts = [bg, logo_chain, overlay]
    else:
        # Text path: drawtext directly onto [bg], one filter per letter.
        text_chain, _label = _text_logo_chain(config, duration, width, y_offset_expr)
        parts = [bg, text_chain]

    if has_music:
        fade_out_st = max(0.0, duration - config.music.fade_out_seconds)
        audio = (
            f"[1:a]volume={config.music.volume:.2f},"
            f"afade=t=out:st={fade_out_st:.2f}:d={config.music.fade_out_seconds:.2f},"
            f"atrim=0:{duration:.2f}[aout]"
        )
        parts.append(audio)

    return ";".join(parts)


def _run_ffmpeg(cmd: list[str]) -> tuple[bool, str | None]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return False, "ffmpeg not found"
    if result.returncode != 0:
        # ffmpeg's banner is at the top, actual errors at the bottom.
        return False, result.stderr[-1500:]
    return True, None


def render_intro(config: IntroConfig) -> dict:
    """Render `<intro>/intro.mp4` from a config. Returns a summary dict.

    The command shape depends on logo mode:
      - **image mode**: input 0 = logo PNG, input 1 = music or silent
      - **text mode**: input 0 = music or silent (no PNG input — the
        text is generated entirely from drawtext expressions)

    Returns `{output, duration, ok, error}` — `output` is None on
    failure so callers can short-circuit without inspecting `ok`.
    """
    is_image_mode = config.logo.mode == "image"

    logo_path: Path | None = None
    if is_image_mode:
        logo_path = resolved_logo_path(config)
        if logo_path is None or not logo_path.is_file():
            return {
                "output": None,
                "duration": config.duration,
                "ok": False,
                "error": f"logo not found at {logo_path}",
            }

    music_path = resolved_music_path(config)
    has_music = music_path is not None and music_path.is_file()

    output = intro_output_path(config.name)
    output.parent.mkdir(parents=True, exist_ok=True)

    filtergraph = _build_filtergraph(config, has_music=has_music)
    codec_opts = settings.ffmpeg_video_codec_opts.split()

    cmd: list[str] = [settings.ffmpeg_path, "-y"]
    if is_image_mode:
        # Input 0: the logo PNG. The lavfi color *background* is
        # generated INSIDE the filtergraph (not via -f lavfi -i), so
        # logo is [0:v] in the graph.
        cmd += ["-i", logo_path.as_posix()]
    # Audio input index shifts based on whether there's a logo input.
    # Text mode has no logo input → audio is [0:a]. Image mode → [1:a].
    audio_input_idx = 1 if is_image_mode else 0
    if has_music:
        cmd += ["-i", music_path.as_posix()]
    else:
        # No music → generate a SILENT stereo track at AAC-compatible
        # sample rate. Critical for compilation concat: if the intro
        # is video-only, ffmpeg's concat demuxer drops the audio
        # stream from the joined output (every input must have
        # matching streams in the same order). With a silent track
        # present, downstream concat preserves audio. `-t` on the
        # input caps anullsrc which would otherwise generate
        # indefinitely.
        cmd += [
            "-f",
            "lavfi",
            "-t",
            str(config.duration),
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
        ]
    cmd += [
        "-filter_complex",
        filtergraph,
        "-map",
        "[vid]",
    ]
    if has_music:
        cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
    else:
        # Silent audio — map by the index we tracked above (depends
        # on whether image mode added a logo input ahead of it) and
        # encode AAC to match what `_render_clip_part` produces.
        cmd += ["-map", f"{audio_input_idx}:a", "-c:a", "aac", "-b:a", "128k"]
    cmd += [
        "-c:v",
        settings.ffmpeg_video_codec,
        *codec_opts,
        "-pix_fmt",
        "yuv420p",  # broadly compatible playback
        "-t",
        str(config.duration),
        output.as_posix(),
    ]

    ok, err = _run_ffmpeg(cmd)
    return {
        "output": output.as_posix() if ok else None,
        "duration": config.duration,
        "ok": ok,
        "error": err,
        "has_music": has_music,
    }
