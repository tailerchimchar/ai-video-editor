"""HTTP layer for branded intros.

Endpoints mirror the compilation editing pattern:

    POST   /intros                  create + render a new intro
    GET    /intros                  list intros
    GET    /intros/{name}           current config
    POST   /intros/{name}/render    re-render from current intro.json
    PATCH  /intros/{name}           update config fields + re-render

All ffmpeg work runs in `asyncio.to_thread` so the event loop stays
free. Intros are small (~3s, ~100KB) so renders complete in seconds —
we wait inline rather than enqueuing a background job.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..intros import (
    IntroConfig,
    default_intro_config,
    default_text_intro_config,
    get_default_intro_name,
    intro_folder,
    intro_logo_path,
    intro_music_path,
    intro_output_path,
    list_intros,
    list_presets,
    load_intro,
    preset_path,
    render_intro,
    save_intro,
    set_default_intro_name,
)
from ..intros.storage import ensure_intro_folder

router = APIRouter(tags=["intro"], prefix="/intros")


class CreateIntroRequest(BaseModel):
    """Image-mode intro creation. Either supply `logo_source_path`
    (we copy the file in) OR `preset` (we copy from the preset
    library). Exactly one is required."""

    name: str = Field(..., min_length=1, max_length=64)
    # Path to a logo PNG. We COPY it into the intro folder rather than
    # symlinking so the intro folder is self-contained / portable.
    logo_source_path: str | None = None
    # Or pick a preset by name (one of `list_presets()`).
    preset: str | None = None
    # Optional music file (mp3/wav/ogg).
    music_source_path: str | None = None
    # Override the default config — any unspecified field uses the default.
    duration: float | None = None
    background_color: str | None = None
    logo_scale: float | None = None
    bounce_pixels: int | None = None
    bounce_count: int | None = None
    fade_in_seconds: float | None = None
    pulse_factor: float | None = None
    music_volume: float | None = None


class CreateTextIntroRequest(BaseModel):
    """Text-mode intro creation — no PNG needed, drawtext renders
    everything from the supplied config."""

    name: str = Field(..., min_length=1, max_length=64)
    text: str = Field(..., min_length=1)
    font_path: str = ""
    font_size: int = 200
    font_color: str = "white"
    stroke_width: int = 0
    stroke_color: str = "black"
    letter_spacing: int = 0
    alignment: str = "center"
    shadow_offset_x: int = 0
    shadow_offset_y: int = 0
    shadow_color: str = "black"
    shadow_alpha: float = 0.5
    # Shared
    duration: float | None = None
    background_color: str | None = None
    bounce_pixels: int | None = None
    bounce_count: int | None = None
    fade_in_seconds: float | None = None
    music_source_path: str | None = None
    music_volume: float | None = None


class UpdateIntroRequest(BaseModel):
    """Patch config fields and re-render. Only fields you pass change."""

    duration: float | None = None
    background_color: str | None = None
    logo_scale: float | None = None
    bounce_pixels: int | None = None
    bounce_count: int | None = None
    fade_in_seconds: float | None = None
    pulse_factor: float | None = None
    music_volume: float | None = None
    music_fade_out_seconds: float | None = None


def _apply_overrides(
    cfg: IntroConfig,
    *,
    duration: float | None = None,
    background_color: str | None = None,
    logo_scale: float | None = None,
    bounce_pixels: int | None = None,
    bounce_count: int | None = None,
    fade_in_seconds: float | None = None,
    pulse_factor: float | None = None,
    music_volume: float | None = None,
    music_fade_out_seconds: float | None = None,
) -> IntroConfig:
    """Merge optional overrides into a config. Returns the new config.

    Kept as a flat function (rather than a method) so callers don't
    need to construct an UpdateIntroRequest dict for each field.
    """
    data = cfg.model_dump()
    if duration is not None:
        data["duration"] = duration
    if background_color is not None:
        data["background"]["color"] = background_color
    if logo_scale is not None:
        data["logo"]["scale"] = logo_scale
    if bounce_pixels is not None:
        data["logo"]["bounce_pixels"] = bounce_pixels
    if bounce_count is not None:
        data["logo"]["bounce_count"] = bounce_count
    if fade_in_seconds is not None:
        data["logo"]["fade_in_seconds"] = fade_in_seconds
    if pulse_factor is not None:
        data["logo"]["pulse_factor"] = pulse_factor
    if music_volume is not None or music_fade_out_seconds is not None:
        data.setdefault("music", {"filename": "music.mp3", "volume": 0.5, "fade_out_seconds": 0.5})
        if music_volume is not None:
            data["music"]["volume"] = music_volume
        if music_fade_out_seconds is not None:
            data["music"]["fade_out_seconds"] = music_fade_out_seconds
    return IntroConfig.model_validate(data)


@router.post("")
async def create_intro(req: CreateIntroRequest):
    """Create + render an image-mode intro.

    Provide EITHER `logo_source_path` (a path on disk) OR `preset`
    (a name from `list_presets`). The chosen PNG is COPIED into the
    intro folder so it's self-contained. Music is also copied when
    provided."""
    if not req.logo_source_path and not req.preset:
        raise HTTPException(400, "must supply `logo_source_path` or `preset`")
    if req.logo_source_path and req.preset:
        raise HTTPException(400, "supply only one of `logo_source_path` or `preset`")

    if req.preset:
        logo_src = preset_path(req.preset)
        if not logo_src.is_file():
            raise HTTPException(404, f"preset not found: {req.preset}")
    else:
        logo_src = Path(req.logo_source_path)
        if not logo_src.is_file():
            raise HTTPException(404, f"logo not found: {logo_src}")

    ensure_intro_folder(req.name)
    shutil.copyfile(logo_src, intro_logo_path(req.name))

    music_src: Path | None = None
    if req.music_source_path:
        music_src = Path(req.music_source_path)
        if not music_src.is_file():
            raise HTTPException(404, f"music not found: {music_src}")
        shutil.copyfile(music_src, intro_music_path(req.name))

    cfg = default_intro_config(req.name)
    if music_src is not None:
        # Reading from default music slot — wire up the config.
        from ..intros.config import _MusicConfig

        cfg = cfg.model_copy(update={"music": _MusicConfig()})
    cfg = _apply_overrides(
        cfg,
        duration=req.duration,
        background_color=req.background_color,
        logo_scale=req.logo_scale,
        bounce_pixels=req.bounce_pixels,
        bounce_count=req.bounce_count,
        fade_in_seconds=req.fade_in_seconds,
        pulse_factor=req.pulse_factor,
        music_volume=req.music_volume,
    )
    save_intro(cfg)

    result = await asyncio.to_thread(render_intro, cfg)
    if not result["ok"]:
        raise HTTPException(500, f"render failed: {result.get('error')}")
    return {"name": req.name, **result, "config": cfg.model_dump()}


@router.post("/text")
async def create_text_intro(req: CreateTextIntroRequest):
    """Create + render a text-mode intro — no PNG required.

    The brand text is rendered via per-letter `drawtext` so
    letter-spacing and alignment work as configured. Stroke and
    shadow are also config-driven.
    """
    ensure_intro_folder(req.name)

    # Music handling parallels image-mode create.
    if req.music_source_path:
        music_src = Path(req.music_source_path)
        if not music_src.is_file():
            raise HTTPException(404, f"music not found: {music_src}")
        shutil.copyfile(music_src, intro_music_path(req.name))

    cfg = default_text_intro_config(req.name, req.text)
    # Build a fresh logo config from the request so the model is
    # validated end-to-end (font_size > 0, alpha in [0,1], etc.).
    from ..intros.config import _MusicConfig, _TextLogoConfig

    text_logo_kwargs = {
        "text": req.text,
        "font_path": req.font_path,
        "font_size": req.font_size,
        "font_color": req.font_color,
        "stroke_width": req.stroke_width,
        "stroke_color": req.stroke_color,
        "letter_spacing": req.letter_spacing,
        "alignment": req.alignment,
        "shadow_offset_x": req.shadow_offset_x,
        "shadow_offset_y": req.shadow_offset_y,
        "shadow_color": req.shadow_color,
        "shadow_alpha": req.shadow_alpha,
    }
    for k in ("bounce_pixels", "bounce_count", "fade_in_seconds"):
        v = getattr(req, k)
        if v is not None:
            text_logo_kwargs[k] = v
    cfg = cfg.model_copy(update={"logo": _TextLogoConfig(**text_logo_kwargs)})
    if req.duration is not None:
        cfg = cfg.model_copy(update={"duration": req.duration})
    if req.background_color is not None:
        from ..intros.config import _BackgroundConfig

        cfg = cfg.model_copy(update={"background": _BackgroundConfig(color=req.background_color)})
    if req.music_source_path:
        cfg = cfg.model_copy(
            update={
                "music": _MusicConfig(
                    **({"volume": req.music_volume} if req.music_volume is not None else {})
                )
            }
        )
    save_intro(cfg)

    result = await asyncio.to_thread(render_intro, cfg)
    if not result["ok"]:
        raise HTTPException(500, f"render failed: {result.get('error')}")
    return {"name": req.name, **result, "config": cfg.model_dump()}


class SetDefaultBody(BaseModel):
    # None clears the default; a name sets it.
    intro_name: str | None = None


@router.post("/default")
async def set_default_intro_endpoint(body: SetDefaultBody):
    """Mark an intro as the workspace default.

    Once set, `set_compilation_intro` can be called without an
    `intro_name` and will use this default. Pass `intro_name: null`
    to clear the default (compilations will then require an explicit
    name again).
    """
    if body.intro_name is not None and not intro_folder(body.intro_name).is_dir():
        raise HTTPException(404, f"intro not found: {body.intro_name}")
    set_default_intro_name(body.intro_name)
    return {"default_intro": body.intro_name}


@router.get("/default")
async def get_default_intro_endpoint():
    """Return the workspace default intro name, or null if unset."""
    return {"default_intro": get_default_intro_name()}


@router.get("/presets")
async def list_presets_endpoint():
    """Names of preset PNGs in the intro library.

    Drop new `.png` files into `<workspace>/intros/_presets/` to add
    more styles; they appear here on the next call.
    """
    return list_presets()


@router.get("")
async def list_intros_endpoint():
    """Names of intros with an `intro.json` on disk."""
    return list_intros()


@router.get("/{name}")
async def get_intro(name: str):
    """Current config + paths for one intro."""
    if not intro_folder(name).is_dir():
        raise HTTPException(404, f"intro not found: {name}")
    try:
        cfg = load_intro(name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from None
    output = intro_output_path(name)
    return {
        "name": name,
        "config": cfg.model_dump(),
        "rendered_path": output.as_posix() if output.exists() else None,
        "folder": intro_folder(name).as_posix(),
    }


@router.post("/{name}/render")
async def render_intro_endpoint(name: str):
    """Re-render an existing intro from its current `intro.json`.

    Cheap (~1s for a 3s intro). Useful after editing `intro.json` by
    hand, or for re-rendering when the logo/music has been swapped.
    """
    if not intro_folder(name).is_dir():
        raise HTTPException(404, f"intro not found: {name}")
    cfg = load_intro(name)
    result = await asyncio.to_thread(render_intro, cfg)
    if not result["ok"]:
        raise HTTPException(500, f"render failed: {result.get('error')}")
    return {"name": name, **result}


@router.patch("/{name}")
async def update_intro_endpoint(name: str, req: UpdateIntroRequest):
    """Patch a subset of config fields and re-render.

    Only fields you supply are changed — the rest are preserved. The
    spec is overwritten on disk and the intro re-renders.
    """
    if not intro_folder(name).is_dir():
        raise HTTPException(404, f"intro not found: {name}")
    cfg = load_intro(name)
    cfg = _apply_overrides(cfg, **req.model_dump(exclude_none=True))
    save_intro(cfg)
    result = await asyncio.to_thread(render_intro, cfg)
    if not result["ok"]:
        raise HTTPException(500, f"render failed: {result.get('error')}")
    return {"name": name, **result, "config": cfg.model_dump()}
