from typing import Literal

from pydantic import BaseModel, computed_field


# --- Assets ---
class AssetOut(BaseModel):
    id: str
    filename: str
    path: str
    game: str | None
    created_at: str
    indexed_at: str
    # 'imported' = found by scan; 'downloaded' = ingested via URL.
    # Drives the auto-delete safety rule in the cleanup tool.
    source_origin: str | None = "imported"
    # Set when the source FILE was deleted. The asset row stays so
    # compilations referencing it keep their FK; this flag tells callers
    # the underlying .mp4 is no longer on disk.
    source_deleted_at: str | None = None


class ScanResponse(BaseModel):
    new_assets: int
    total_assets: int


# --- Clips ---
class ClipCreate(BaseModel):
    asset_id: str
    start_seconds: float
    end_seconds: float


class ClipOut(BaseModel):
    id: str
    asset_id: str
    start_seconds: float
    end_seconds: float
    output_path: str
    created_at: str
    job_id: str


# --- Projects ---
class ProjectCreate(BaseModel):
    name: str


class ProjectOut(BaseModel):
    id: str
    name: str
    created_at: str


class TimelineItemCreate(BaseModel):
    clip_id: str
    position: int


class TimelineItemOut(BaseModel):
    id: str
    project_id: str
    clip_id: str
    position: int
    created_at: str


class RenderResponse(BaseModel):
    job_id: str


# --- Jobs ---
class JobOut(BaseModel):
    id: str
    project_id: str | None
    type: str
    status: str
    output_path: str | None
    error: str | None
    created_at: str
    completed_at: str | None

    @computed_field
    @property
    def summary(self) -> str:
        if self.status == "completed" and self.output_path:
            return f"Done! Output: {self.output_path}"
        if self.status == "failed":
            return f"Failed: {self.error or 'unknown error'}"
        return f"Status: {self.status}"


# --- Phase 2: highlight candidates ---
CandidateSource = Literal[
    "outplayed_clip",
    "audio_peak",
    "transcript_keyword",
    "manual_marker",
    "riot_api",
    "overwolf_game_event",
]


class HighlightCandidate(BaseModel):
    id: str
    video_id: str
    source: CandidateSource
    start_seconds: float
    end_seconds: float
    event_type: str | None = None
    confidence: float | None = None
    metadata: dict | None = None
    created_at: str


class GenerateCandidatesResponse(BaseModel):
    video_id: str
    new_candidates: int
    total_candidates: int
    by_source: dict[str, int]


class RankedCandidate(BaseModel):
    candidate_id: str
    keep: bool
    funny_score: float
    hype_score: float
    story_score: float
    suggested_start_seconds: float
    suggested_end_seconds: float
    reason: str


class RankResponse(BaseModel):
    job_id: str
