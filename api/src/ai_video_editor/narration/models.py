"""Narration data shapes (pure data, no I/O). Contract for AVE-68 tasks."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

KEY_PATTERN = r"^[0-9A-Za-z_\-]+$"
MIN_TAKE_S = 0.3


class TargetRef(BaseModel):
    """What is being narrated: a registered target `kind` and its `key`
    (for kind "story", the round folder name)."""

    kind: str = Field(..., pattern=KEY_PATTERN)
    key: str = Field(..., pattern=KEY_PATTERN)


class Take(BaseModel):
    """One recorded take, pinned to the video second it started at."""

    id: str = Field(..., pattern=KEY_PATTERN)
    start_s: float = Field(..., ge=0.0)
    duration_s: float = Field(..., gt=0.0)
    media: str  # file name inside the store's takes folder, e.g. "<id>.webm"
    has_camera: bool = False
    created_at: str  # ISO-8601 UTC
    media_in_s: float = Field(default=0.0, ge=0.0)  # where in the media this take plays from
    offset_s: float = Field(default=0.0, ge=-3.0, le=3.0)  # nudge vs start_s
    gain: float = Field(default=1.0, gt=0.0, le=4.0)
    muted: bool = False

    @property
    def end_s(self) -> float:
        return self.start_s + self.duration_s

    @property
    def start_effective_s(self) -> float:
        return self.start_s + self.offset_s


class CameraBox(BaseModel):
    """The owner's camera box. Position fields are fractions of the video
    frame (top-left corner); None means "use the game profile default".
    Height follows the camera's aspect ratio (16:9 unless measured)."""

    enabled: bool = False  # shown in the export; recording camera is separate
    width_frac: float | None = Field(default=None, ge=0.08, le=0.5)
    x: float | None = Field(default=None, ge=0.0, le=1.0)
    y: float | None = Field(default=None, ge=0.0, le=1.0)
    rounded: bool = True


class _Region(BaseModel):
    id: str = Field(..., pattern=KEY_PATTERN)
    start_s: float = Field(..., ge=0.0)
    end_s: float

    @model_validator(mode="after")
    def _end_after_start(self):
        if self.end_s <= self.start_s:
            raise ValueError("end_s must be greater than start_s")
        return self


class GameAudioRegion(_Region):
    """A stretch of video where the game audio plays at `level`."""

    level: float = Field(..., ge=0.0, le=2.0)


class CameraRegion(_Region):
    """A stretch of video where the camera box is shown with `box`."""

    box: CameraBox


class TakeEdit(BaseModel):
    """A partial take change; unset fields are left alone."""

    start_s: float | None = Field(default=None, ge=0.0)
    duration_s: float | None = Field(default=None, gt=0.0)
    media_in_s: float | None = Field(default=None, ge=0.0)
    offset_s: float | None = Field(default=None, ge=-3.0, le=3.0)
    gain: float | None = Field(default=None, gt=0.0, le=4.0)
    muted: bool | None = None


class Marker(BaseModel):
    at_s: float
    label: str
    kind: Literal["chapter", "game", "cue", "insert"]


class FrameStrip(BaseModel):
    sprite_url: str
    every_s: float
    tile_w: int
    tile_h: int
    cols: int
    count: int


class Peaks(BaseModel):
    rate_hz: int
    peaks: list[float]


class AudioMix(BaseModel):
    """How the original audio ducks under the owner's voice."""

    narration_gain: float = Field(default=1.0, gt=0.0, le=4.0)
    duck_threshold: float = Field(default=0.02, gt=0.0, le=1.0)
    duck_ratio: float = Field(default=8.0, ge=1.0, le=20.0)
    duck_attack_ms: float = Field(default=20.0, gt=0.0, le=2000.0)
    duck_release_ms: float = Field(default=400.0, gt=0.0, le=9000.0)
    game_gain: float = Field(default=1.0, ge=0.0, le=2.0)


class NarrationSession(BaseModel):
    """Everything stored for one narrated video."""

    target: TargetRef
    game: str = "default"
    # Fingerprint of the video the takes were recorded against; a different
    # current fingerprint means the takes may no longer line up ("stale").
    video_fingerprint: str = ""
    takes: list[Take] = []
    camera: CameraBox = CameraBox()
    mix: AudioMix = AudioMix()
    game_regions: list[GameAudioRegion] = []
    camera_regions: list[CameraRegion] = []
    pinned_intro_s: float | None = None


class PixelBox(BaseModel):
    """A resolved box in output pixels (even numbers, inside the frame)."""

    x: int
    y: int
    w: int
    h: int


ExportStatus = Literal["ok", "stale", "no_takes"]
