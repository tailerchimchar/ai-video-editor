"""Narration data shapes (pure data, no I/O). Contract for AVE-68 tasks."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

KEY_PATTERN = r"^[0-9A-Za-z_\-]+$"


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

    @property
    def end_s(self) -> float:
        return self.start_s + self.duration_s


class CameraBox(BaseModel):
    """The owner's camera box. Position fields are fractions of the video
    frame (top-left corner); None means "use the game profile default".
    Height follows the camera's aspect ratio (16:9 unless measured)."""

    enabled: bool = False  # shown in the export; recording camera is separate
    width_frac: float | None = Field(default=None, ge=0.08, le=0.5)
    x: float | None = Field(default=None, ge=0.0, le=1.0)
    y: float | None = Field(default=None, ge=0.0, le=1.0)
    rounded: bool = True


class AudioMix(BaseModel):
    """How the original audio ducks under the owner's voice."""

    narration_gain: float = Field(default=1.0, gt=0.0, le=4.0)
    duck_threshold: float = Field(default=0.02, gt=0.0, le=1.0)
    duck_ratio: float = Field(default=8.0, ge=1.0, le=20.0)
    duck_attack_ms: float = Field(default=20.0, gt=0.0, le=2000.0)
    duck_release_ms: float = Field(default=400.0, gt=0.0, le=9000.0)


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


class PixelBox(BaseModel):
    """A resolved box in output pixels (even numbers, inside the frame)."""

    x: int
    y: int
    w: int
    h: int


ExportStatus = Literal["ok", "stale", "no_takes"]
