"""Per-game profile registry (Sprint #2).

A profile is the game's *vocabulary*: named ROI regions (fractions of
the frame), expected sound files (under
`WORKSPACE/media_library/<game>/sfx/`), and the `asset.game` strings it
answers to (`aliases`).

Profiles are read-only TOML files shipped with the repo
(`src/ai_video_editor/profiles/<game>.toml`). Audio files are
user-supplied and live in the workspace — the profile only *names*
them. Missing files are warn-and-skip at callsites, never fatal.

Lookup is case-insensitive against `meta.game` and `meta.aliases`.
Unknown games fall back to `default.toml` (empty regions/sounds, so
generic primitives keep working and game-specific tools warn-skip).
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[import-not-found]


PROFILES_DIR = Path(__file__).parent
DEFAULT_PROFILE_KEY = "default"


class Region(BaseModel):
    """A named on-screen rectangle, as fractions of the source frame.

    Resolution-independent: `x`/`y` are the top-left corner, `w`/`h`
    the size. All four must be in `[0, 1]` and the box must stay inside
    the frame (`x + w <= 1`, `y + h <= 1`).
    """

    x: float = Field(..., ge=0.0, le=1.0)
    y: float = Field(..., ge=0.0, le=1.0)
    w: float = Field(..., gt=0.0, le=1.0)
    h: float = Field(..., gt=0.0, le=1.0)
    description: str = ""

    @model_validator(mode="after")
    def _box_inside_frame(self) -> Region:
        if self.x + self.w > 1.0 + 1e-9:
            raise ValueError(f"region extends past right edge: x+w={self.x + self.w}")
        if self.y + self.h > 1.0 + 1e-9:
            raise ValueError(f"region extends past bottom edge: y+h={self.y + self.h}")
        return self


class Sound(BaseModel):
    """A named sound declared by a profile.

    `file` is the basename under `WORKSPACE/media_library/<game>/sfx/`
    — the workspace path is resolved at use time so the profile stays
    portable across machines.
    """

    file: str
    description: str = ""


class Transcription(BaseModel):
    """Game-specific knobs that the STT pipeline reads at transcribe time.

    All fields optional with empty defaults so profiles without a
    [transcription] section keep working (transcribe falls back to
    stock Whisper behavior).
    """

    # Steers Whisper toward expected vocabulary. Pass as `initial_prompt`
    # to faster-whisper. Note: Whisper applies the prompt only to the
    # first 30s unless `condition_on_previous_text=False` is also set.
    initial_prompt: str = ""

    # Used by the post-correction fuzzy matcher (rapidfuzz) to snap
    # mangled tokens like "yes so" back to "Yasuo" when they're a close
    # phonetic match. Also doubles as a "words to keep intact" hint.
    vocabulary: list[str] = Field(default_factory=list)

    # Game-specific hype cues that EXTEND the generic cross-game list
    # in `candidates/transcript.py` (e.g. "pentakill" only fires for
    # LoL recordings; "spike planted" only for Val).
    hype_cues: list[str] = Field(default_factory=list)

    # Explicit alias map for proper nouns Whisper consistently mishears
    # below fuzzy_correct's similarity threshold. e.g. "Noodlz" pronounced
    # like "noodles" — they're 76.9% similar (below 80 cutoff), so the
    # general fuzzy path won't snap them. An entry like
    # `{"Noodlz": ["noodles", "noodle"]}` does a direct lowercase match
    # and bypasses the threshold. Use for streamer names + any term
    # Whisper turns into an obvious English word that breaks captions.
    name_aliases: dict[str, list[str]] = Field(default_factory=dict)


class ProfileMeta(BaseModel):
    game: str
    aliases: list[str] = Field(default_factory=list)
    default_aspect: Literal["16:9", "9:16"] = "16:9"


class Profile(BaseModel):
    meta: ProfileMeta
    regions: dict[str, Region] = Field(default_factory=dict)
    sounds: dict[str, Sound] = Field(default_factory=dict)
    transcription: Transcription = Field(default_factory=Transcription)


def _load_toml(path: Path) -> Profile:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return Profile(**data)


@lru_cache(maxsize=1)
def _registry() -> dict[str, Profile]:
    """All profiles in `PROFILES_DIR`, keyed lowercase by `meta.game`
    and each alias. Loaded once per process; tests that need a clean
    state can call `_registry.cache_clear()`."""
    registry: dict[str, Profile] = {}
    for path in sorted(PROFILES_DIR.glob("*.toml")):
        profile = _load_toml(path)
        keys = {profile.meta.game.lower(), *(a.lower() for a in profile.meta.aliases)}
        for k in keys:
            registry[k] = profile
    return registry


def _empty_profile() -> Profile:
    """Fallback when even default.toml is missing — keep callers alive."""
    return Profile(meta=ProfileMeta(game="default"))


def load_profile(game: str | None) -> Profile:
    """Resolve an `asset.game` value to a Profile.

    Outplayed records the field as `<game name>_<session date>`
    (e.g. `'League of Legends_05-22-2026_0-53-57-757'`), so we try a
    direct alias lookup first and then fall back to splitting at the
    first underscore — which catches the date suffix without
    misinterpreting legitimately-underscored names if a direct match
    exists.

    Unknown game / `None` → the `default` profile (empty regions and
    sounds, so generic primitives still work and game-specific tools
    can warn-and-skip rather than crash).
    """
    if game:
        reg = _registry()
        # 1) direct match (case-insensitive)
        profile = reg.get(game.lower())
        if profile is not None:
            return profile
        # 2) strip Outplayed-style "<name>_<date>" suffix and retry
        if "_" in game:
            head = game.split("_", 1)[0]
            profile = reg.get(head.lower())
            if profile is not None:
                return profile
    return _registry().get(DEFAULT_PROFILE_KEY, _empty_profile())


def region_box(game: str | None, region_name: str) -> Region | None:
    """Look up a named region in the game's profile.

    Returns None if the profile doesn't declare it — the caller should
    warn and skip rather than crash (game-specific tools should never
    take down a render).
    """
    return load_profile(game).regions.get(region_name)


def sound_path(game: str | None, sound_name: str) -> Path | None:
    """Resolve a profile sound to its workspace `.wav` path.

    Returns None when (a) the profile doesn't declare the sound, or
    (b) the workspace file doesn't exist yet (user hasn't extracted it).
    Callsites should warn-and-skip in both cases.
    """
    from ..config import settings  # local import — circular at module load

    profile = load_profile(game)
    sound = profile.sounds.get(sound_name)
    if sound is None:
        return None
    path = settings.workspace_dir / "media_library" / profile.meta.game / "sfx" / sound.file
    return path if path.exists() else None
