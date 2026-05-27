"""Intro config — Pydantic model + load/save helpers.

`intro.json` is THE editable description of how an intro renders.
The `logo` field is a discriminated union — pick **image mode** to
overlay a PNG, or **text mode** to render the brand text procedurally
from the config (any text, font, color, stroke, shadow, letter-spacing,
alignment). Iterate by editing the JSON + re-rendering.

Shared knobs:
- `duration` — total runtime in seconds
- `resolution` — output frame size (1920x1080 default for 16:9 reels)
- `background.color` — solid color background
- `logo.bounce_pixels` — vertical amplitude of the damped bounce
- `logo.bounce_count` — how many bounces before it settles
- `logo.fade_in_seconds` — opacity ramp at the start
- `music.volume` — 0..1 mix level under the visuals (when music present)
- `music.fade_out_seconds` — tail-fade at the end

Image-mode-only:
- `logo.filename` — PNG inside `source/` (relative, keeps folder portable)
- `logo.scale` — logo width as fraction of frame width
- `logo.pulse_factor` — scale-pulse amount at impact (1.0 = none)

Text-mode-only:
- `logo.text` — the rendered string ("NOODLZ", "MAIN CHANNEL", etc.)
- `logo.font_path` — absolute path to a .ttf/.otf
- `logo.font_size` — point size in pixels
- `logo.font_color` — hex or named color
- `logo.stroke_width` / `stroke_color` — outline around the text
- `logo.letter_spacing` — extra px between letters (per-letter drawtext)
- `logo.alignment` — left | center | right
- `logo.shadow_offset_x` / `_y` / `_color` / `_alpha` — drop shadow
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .storage import (
    ensure_intro_folder,
    intro_config_path,
    intro_logo_path,
    intro_music_path,
)


class _BackgroundConfig(BaseModel):
    # ffmpeg color names ("black", "0x0a0a0a") or HTML hex. Solid color
    # only for v1; video/image backgrounds are a v2 enhancement.
    color: str = "black"


class _ImageLogoConfig(BaseModel):
    """Overlay-a-PNG logo. The "image" tag is the discriminator that
    chooses this branch when the spec is loaded."""

    mode: Literal["image"] = "image"

    # Filename inside `source/` — keep relative so the intro folder
    # is portable (copy-pasting the folder works without path rewrites).
    filename: str = "logo.png"
    # Logo width as fraction of frame width. 0.55 ≈ medium-large.
    scale: float = Field(0.55, gt=0.0, le=1.0)
    # Damped vertical bounce amplitude in pixels. 0 disables.
    bounce_pixels: int = Field(40, ge=0)
    # Number of perceptible bounces before damping settles it.
    bounce_count: int = Field(2, ge=0)
    # Opacity ramp at the start (seconds). 0 = pop in.
    fade_in_seconds: float = Field(0.3, ge=0.0)
    # Scale-pulse on impact: peak scale = scale * pulse_factor at impact,
    # settles to `scale` over `bounce_count` bounces. 1.0 = no pulse.
    pulse_factor: float = Field(1.15, ge=1.0)


class _TextLogoConfig(BaseModel):
    """Procedural text logo — every pixel comes from drawtext.

    Letter-spacing is implemented by drawing each letter as its own
    `drawtext` filter, positioned with a monospace approximation
    (`font_size * 0.6` per char). It's not pixel-perfect kerning;
    real designed logos still belong in image mode.
    """

    mode: Literal["text"] = "text"

    text: str = Field(..., min_length=1)
    # Absolute path to a TTF/OTF. Inherits the config-default caption
    # font when omitted; callers can override per-intro.
    font_path: str = ""
    font_size: int = Field(200, gt=0)
    font_color: str = "white"

    # Stroke / outline. width=0 disables.
    stroke_width: int = Field(0, ge=0)
    stroke_color: str = "black"

    # Spacing + alignment.
    letter_spacing: int = Field(0, ge=-20)  # allow slight negative kerning
    alignment: Literal["left", "center", "right"] = "center"

    # Drop shadow. offset=0 effectively disables.
    shadow_offset_x: int = 0
    shadow_offset_y: int = 0
    shadow_color: str = "black"
    shadow_alpha: float = Field(0.5, ge=0.0, le=1.0)

    # Animation params (mirror image-mode for consistency).
    bounce_pixels: int = Field(40, ge=0)
    bounce_count: int = Field(2, ge=0)
    fade_in_seconds: float = Field(0.3, ge=0.0)


class _MusicConfig(BaseModel):
    filename: str = "music.mp3"
    # Mix level 0..1. Quiet under-bed by default.
    volume: float = Field(0.5, ge=0.0, le=1.0)
    # Tail-fade so the intro ends clean even without music ending naturally.
    fade_out_seconds: float = Field(0.5, ge=0.0)


LogoConfig = _ImageLogoConfig | _TextLogoConfig


class IntroConfig(BaseModel):
    """Top-level intro config persisted as `intro.json`.

    `logo` is a discriminated union — the `mode` field on the value
    picks image vs text rendering. Pydantic v2 picks the right branch
    automatically when loading from JSON.
    """

    name: str
    duration: float = Field(3.0, gt=0.0, le=10.0)
    # 1920x1080 matches the default compilation aspect; a future 9:16
    # variant would carry "1080x1920" here.
    resolution: str = "1920x1080"
    background: _BackgroundConfig = Field(default_factory=_BackgroundConfig)
    logo: LogoConfig = Field(default_factory=_ImageLogoConfig, discriminator="mode")
    music: _MusicConfig | None = None  # None = silent intro


def default_intro_config(name: str) -> IntroConfig:
    """A reasonable starting point — the user edits from here."""
    return IntroConfig(name=name)


def default_text_intro_config(name: str, text: str) -> IntroConfig:
    """Text-mode equivalent — used when create_intro skips the logo
    upload and goes straight to drawtext."""
    return IntroConfig(name=name, logo=_TextLogoConfig(text=text))


def load_intro(name: str) -> IntroConfig:
    """Read `<intro>/intro.json` into a validated model."""
    path = intro_config_path(name)
    data = json.loads(path.read_text(encoding="utf-8"))
    return IntroConfig.model_validate(data)


def save_intro(config: IntroConfig) -> Path:
    """Persist `intro.json`. Creates the folder if missing."""
    ensure_intro_folder(config.name)
    path = intro_config_path(config.name)
    path.write_text(config.model_dump_json(indent=2), encoding="utf-8")
    return path


def resolved_logo_path(config: IntroConfig) -> Path | None:
    """Absolute path of the logo image this config references.

    Returns None for text-mode intros — they have no source image."""
    if config.logo.mode != "image":
        return None
    if config.logo.filename == "logo.png":
        return intro_logo_path(config.name)
    return intro_logo_path(config.name).parent / config.logo.filename


def resolved_music_path(config: IntroConfig) -> Path | None:
    """Absolute path of the music file, or None when the intro is silent."""
    if config.music is None:
        return None
    if config.music.filename == "music.mp3":
        return intro_music_path(config.name)
    return intro_music_path(config.name).parent / config.music.filename
