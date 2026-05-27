"""Filesystem layout for intros — pure path helpers.

Every intro lives at `<workspace>/intros/<name>/` with a fixed
sub-structure. Keeping this in one module means the renderer, router,
and MCP tools all agree on where files go without hand-coding paths.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import settings

_SOURCE_DIRNAME = "source"
_CONFIG_FILENAME = "intro.json"
_OUTPUT_FILENAME = "intro.mp4"
_LOGO_FILENAME = "logo.png"
_MUSIC_FILENAME = "music.mp3"


def intros_root() -> Path:
    """Canonical absolute path of the intros directory.

    Read at call-time so tests can monkeypatch `settings.workspace_dir`."""
    return (settings.workspace_dir / "intros").resolve(strict=False)


def intro_folder(name: str) -> Path:
    """Per-intro folder. Created lazily by callers that need it."""
    return intros_root() / name


def intro_config_path(name: str) -> Path:
    return intro_folder(name) / _CONFIG_FILENAME


def intro_output_path(name: str) -> Path:
    return intro_folder(name) / _OUTPUT_FILENAME


def intro_logo_path(name: str) -> Path:
    return intro_folder(name) / _SOURCE_DIRNAME / _LOGO_FILENAME


def intro_music_path(name: str) -> Path:
    return intro_folder(name) / _SOURCE_DIRNAME / _MUSIC_FILENAME


def list_intros() -> list[str]:
    """Names of intros that have an `intro.json` on disk.

    Used by the MCP `list_intros` tool. Skips folders without a
    config (in-progress / hand-deleted intros)."""
    root = intros_root()
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir() and (d / _CONFIG_FILENAME).is_file())


def ensure_intro_folder(name: str) -> Path:
    """Create the per-intro folder and `source/` subdir if missing.

    Returns the intro folder. Idempotent."""
    folder = intro_folder(name)
    (folder / _SOURCE_DIRNAME).mkdir(parents=True, exist_ok=True)
    return folder


# --- PNG preset library ------------------------------------------------
#
# Presets live at `<workspace>/intros/_presets/*.png`. The leading
# underscore keeps them out of `list_intros()` (which filters on
# `intro.json` presence anyway). Users add styles by dropping PNGs
# into the folder — no DB entry, no config, just a file.


_PRESETS_DIRNAME = "_presets"


def presets_root() -> Path:
    """Canonical absolute path of the intro presets directory."""
    return intros_root() / _PRESETS_DIRNAME


def preset_path(preset_name: str) -> Path:
    """Path to a specific preset PNG (may not exist on disk)."""
    return presets_root() / f"{preset_name}.png"


def list_presets() -> list[str]:
    """Names of preset PNGs in the library (sans `.png` extension).

    Returns `[]` if the presets folder hasn't been created yet — the
    library is lazily bootstrapped on first use.
    """
    root = presets_root()
    if not root.is_dir():
        return []
    return sorted(p.stem for p in root.glob("*.png") if p.is_file())


def ensure_presets_root() -> Path:
    """Create the presets directory if it doesn't exist. Idempotent."""
    root = presets_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


# --- Workspace-level intro settings ------------------------------------
#
# A small JSON file at `<workspace>/intros/_settings.json` holds knobs
# that span all intros — currently just "which intro is the default."
# Kept as a single file (not a sqlite row) so the user can hand-edit
# and so the workspace stays self-contained / portable.


_SETTINGS_FILENAME = "_settings.json"


def settings_path() -> Path:
    return intros_root() / _SETTINGS_FILENAME


def load_intro_settings() -> dict:
    """Read the workspace-level intro settings.

    Returns an empty dict when the file doesn't exist yet (first run).
    Never raises on a malformed file — falls back to empty so a busted
    settings file doesn't break compile or render."""
    path = settings_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_intro_settings(data: dict) -> None:
    """Persist workspace intro settings. Creates parent dirs as needed."""
    intros_root().mkdir(parents=True, exist_ok=True)
    settings_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_default_intro_name() -> str | None:
    """Convenience reader. None when no default is set."""
    return load_intro_settings().get("default_intro")


def set_default_intro_name(name: str | None) -> None:
    """Convenience writer. Pass None to clear the default."""
    data = load_intro_settings()
    if name is None:
        data.pop("default_intro", None)
    else:
        data["default_intro"] = name
    save_intro_settings(data)
