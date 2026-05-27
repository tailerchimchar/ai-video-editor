"""Branded intro clips — reusable across compilations.

An intro is a small, fully-rendered .mp4 that gets prepended to any
compilation. The storage layout mirrors the compilation pattern so
the same "edit the config, re-render" loop applies:

    WORKSPACE/intros/<name>/
        intro.json          <-- mutable source of truth
        intro.mp4           <-- rendered output
        source/
            logo.png        <-- user-supplied (or cropped from elsewhere)
            music.mp3       <-- optional bg track

`intro.json` carries every knob the renderer reads — bounce amplitude,
duration, scale, fade timing, music volume. Iterate by editing the
JSON and calling `render_intro` again. No DB row, no asset record —
intros live entirely under `WORKSPACE/intros/`.

Integration with compilations: a rendered intro.mp4 is just another
video file. The compile pipeline accepts it via the spec mutator
`insert_intro_clip` (prepends a clip with `event_type="intro"` and a
direct asset_path, no asset_id needed).
"""

from __future__ import annotations

from .config import (
    IntroConfig,
    default_intro_config,
    default_text_intro_config,
    load_intro,
    save_intro,
)
from .render import render_intro
from .storage import (
    ensure_presets_root,
    get_default_intro_name,
    intro_folder,
    intro_logo_path,
    intro_music_path,
    intro_output_path,
    intros_root,
    list_intros,
    list_presets,
    preset_path,
    presets_root,
    set_default_intro_name,
)

__all__ = [
    "IntroConfig",
    "default_intro_config",
    "default_text_intro_config",
    "ensure_presets_root",
    "get_default_intro_name",
    "intro_folder",
    "intro_logo_path",
    "intro_music_path",
    "intro_output_path",
    "intros_root",
    "list_intros",
    "list_presets",
    "load_intro",
    "preset_path",
    "presets_root",
    "render_intro",
    "save_intro",
    "set_default_intro_name",
]
