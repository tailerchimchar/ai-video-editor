"""Typed edit plan: schema, validation against the registry, resumable execution.

The editing-expert returns an `EditPlan`. `validate_plan` rejects anything the
registry cannot render before a single frame is touched. `execute_plan` then
snapshots the compilation's journal version and applies the plan one step at a
time, recording `plan_progress.json` after each step so a failed run resumes
where it stopped (or is reverted with `compile_journal.revert_to_version`).

The executor is a Protocol: the HTTP layer injects the implementation that
calls the real mutators, tests inject fakes.
"""

import hashlib
import os
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field, ValidationError

from .compile_journal import PLAN_PROGRESS_FILENAME
from .config import settings
from .registry import FORMATS, NOT_RENDERABLE, REGISTRY


class SourceClip(BaseModel):
    clip_ref: str
    start: float
    end: float
    intensity: float


class PlanMusic(BaseModel):
    song_id: str
    trim_to_peak: bool = False
    trim_start_s: float | None = None
    trim_end_s: float | None = None


class PlanEffect(BaseModel):
    # `kind` stays a plain str so validate_plan can report unknown kinds
    # instead of failing at parse time.
    clip_ref: str
    kind: str
    at: float
    """Seconds into the clip the panel wants the effect. Accepted to record
    the panel's intent, but not applied today: zoom, focus and caption are
    whole-clip effects."""
    params: dict = Field(default_factory=dict)


class PlanCard(BaseModel):
    after: str | None = None
    text: str
    seconds: float = 2.5


class PlanSfx(BaseModel):
    clip_ref: str
    at: float
    sound: str


class Rejected(BaseModel):
    wanted: str
    why: str


class QualityCriterion(BaseModel):
    """A quality criterion the Editor wants checked after render.

    Visual-led criteria are scored from sampled frames; audio-led criteria
    are scored from digest moment labels and metadata only (never fails
    on frames alone).
    """

    id: str = Field(pattern=r"^[a-z0-9_]{1,64}$", max_length=64)
    text: str = Field(min_length=1, max_length=300)
    audio_led: bool = False


class EditPlan(BaseModel):
    format: str
    source_clips: list[SourceClip] = Field(default_factory=list)
    order: list[str] = Field(default_factory=list)
    music: PlanMusic | None = None
    effects: list[PlanEffect] = Field(default_factory=list)
    cards: list[PlanCard] = Field(default_factory=list)
    sfx: list[PlanSfx] = Field(default_factory=list)
    rejected: list[Rejected] = Field(default_factory=list)
    quality_criteria: list[QualityCriterion] = Field(default_factory=list, max_length=10)


def discover_sfx_names() -> set[str]:
    """Names (file stems) of every sfx in `WORKSPACE/media_library/<game>/sfx/`."""
    library = settings.workspace_dir / "media_library"
    return {path.stem for path in library.glob("*/sfx/*") if path.is_file()}


def _effect_problems(effect: PlanEffect) -> list[str]:
    where = f"effects[{effect.clip_ref}@{effect.at}]"
    if effect.kind in NOT_RENDERABLE:
        return [f"{where}: '{effect.kind}' is not renderable yet"]
    spec = REGISTRY.get(effect.kind)
    if spec is None:
        return [f"{where}: unknown kind '{effect.kind}'"]
    try:
        spec.params_model.model_validate(effect.params)
    except ValidationError as exc:
        return [f"{where}: bad params for '{effect.kind}': {exc}"]
    return []


def validate_plan(plan: EditPlan, known_sounds: Collection[str] | None = None) -> list[str]:
    """Return one message per problem; an empty list means the plan is renderable.

    `known_sounds` defaults to the sfx files found in the workspace library.
    """
    sounds = discover_sfx_names() if known_sounds is None else set(known_sounds)
    problems: list[str] = []
    if plan.format not in FORMATS:
        problems.append(f"unknown format '{plan.format}' (known: {', '.join(FORMATS)})")
    for effect in plan.effects:
        problems += _effect_problems(effect)
    for hit in plan.sfx:
        if hit.sound not in sounds:
            problems.append(f"sfx[{hit.clip_ref}@{hit.at}]: unknown sound '{hit.sound}'")
    known_refs = {clip.clip_ref for clip in plan.source_clips}
    for ref in plan.order:
        if ref not in known_refs:
            problems.append(f"order: clip_ref '{ref}' is not in source_clips")
    return problems


class PlanProgress(BaseModel):
    plan_hash: str
    completed_steps: int = 0
    snapshot_version: str


class PlanExecutor(Protocol):
    """What `execute_plan` needs from the host: a folder, a journal version,
    and one apply method per plan section."""

    def compilation_folder(self, compilation_id: str) -> Path: ...
    def preflight(self, plan: EditPlan) -> list[str]:
        """Reasons this executor cannot apply `plan`; checked before anything mutates."""
        ...

    def current_version(self, compilation_id: str) -> str: ...
    def apply_source_clips(self, plan: EditPlan) -> None: ...
    def apply_order(self, plan: EditPlan) -> None: ...
    def apply_cards(self, plan: EditPlan) -> None: ...
    def apply_effects(self, plan: EditPlan) -> None: ...
    def apply_sfx(self, plan: EditPlan) -> None: ...
    def apply_music(self, plan: EditPlan) -> None: ...


_APPLY: dict[str, Callable[[PlanExecutor, EditPlan], None]] = {
    "source_clips": lambda ex, plan: ex.apply_source_clips(plan),
    "order": lambda ex, plan: ex.apply_order(plan),
    "cards": lambda ex, plan: ex.apply_cards(plan),
    "effects": lambda ex, plan: ex.apply_effects(plan),
    "sfx": lambda ex, plan: ex.apply_sfx(plan),
    "music": lambda ex, plan: ex.apply_music(plan),
}
STEPS = tuple(_APPLY)


class PlanNotExecutableError(RuntimeError):
    """The executor refused the plan up front; nothing was changed."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def plan_hash(plan: EditPlan) -> str:
    """Hash the plan. Empty ``quality_criteria`` is omitted so pre-field
    plans keep a stable hash across the upgrade."""
    if not plan.quality_criteria:
        payload = plan.model_dump_json(exclude={"quality_criteria"})
    else:
        payload = plan.model_dump_json()
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_progress(folder: Path) -> PlanProgress | None:
    path = folder / PLAN_PROGRESS_FILENAME
    if not path.is_file():
        return None
    try:
        return PlanProgress.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError):
        return None


def _save_progress(folder: Path, progress: PlanProgress) -> None:
    path = folder / PLAN_PROGRESS_FILENAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(progress.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp, path)


def execute_plan(plan: EditPlan, compilation_id: str, executor: PlanExecutor) -> PlanProgress:
    """Apply `plan` step by step, resuming a prior run of the same plan.

    The executor's pre-flight runs first, so an unsupported plan changes
    nothing. A fresh run then records the current journal version as the
    revert point. A step that raises leaves progress at the last completed
    step. Progress is discarded when the plan differs or when the journal is
    shorter than the recorded snapshot (the compilation was reverted); an
    unchanged, fully applied plan is a no-op by design.
    """
    problems = executor.preflight(plan)
    if problems:
        raise PlanNotExecutableError(problems)
    folder = executor.compilation_folder(compilation_id)
    digest = plan_hash(plan)
    version = executor.current_version(compilation_id)
    progress = load_progress(folder)
    if (
        progress is None
        or progress.plan_hash != digest
        or int(version) < int(progress.snapshot_version)
    ):
        progress = PlanProgress(plan_hash=digest, snapshot_version=version)
        _save_progress(folder, progress)
    for step in STEPS[progress.completed_steps :]:
        _APPLY[step](executor, plan)
        progress.completed_steps += 1
        _save_progress(folder, progress)
    return progress
