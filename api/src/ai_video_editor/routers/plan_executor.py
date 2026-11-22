"""`PlanExecutor` backed by the compilation mutators in `routers/edits.py`.

Each step re-renders through `_do_edit`, which journals the new state, so the
journal version recorded before the plan is a valid revert point.

Supported today: verifying `source_clips` refs, `order`, clip effects (zoom,
focus, caption) and music (`music_path` on the spec). Cards, sfx and other
effect kinds cannot be expressed by the existing mutators; `preflight` reports
them so `execute_plan` refuses the whole plan before changing anything.

The music trim (`trim_to_peak`, `trim_start_s`, `trim_end_s`) is not rendered
yet: the full song plays from 0 s. `warnings` reports it so the caller sees
it instead of a silent success.
"""

from collections.abc import Callable
from pathlib import Path

from ..compile import add_effects as spec_add_effects
from ..compile import load_spec, resolve_clip_ref
from ..compile import reorder_clips_explicit as spec_reorder_clips_explicit
from ..compile_journal import read_journal
from ..edit_plan import EditPlan, PlanEffect, load_progress
from ..music.library import Library, music_folder_of
from .edits import AddEffectBody, _do_edit, effect_payload

_MUTATOR_KINDS = ("zoom", "focus", "caption")


class UnsupportedPlanStepError(RuntimeError):
    """The plan asks for something the current mutators cannot apply."""


def _effect_body(effect: PlanEffect) -> AddEffectBody:
    unknown = set(effect.params) - set(AddEffectBody.model_fields)
    if unknown:
        raise ValueError(f"unknown params {sorted(unknown)}")
    return AddEffectBody(clip_ref=effect.clip_ref, kind=effect.kind, **effect.params)


def _dry_run(effect: PlanEffect) -> str | None:
    """Build the effect exactly as `apply_effects` will, without touching the
    spec; the reason it cannot be built, or None."""
    try:
        effect_payload(_effect_body(effect))
    except (TypeError, ValueError) as exc:  # pydantic's ValidationError is a ValueError
        return f"effects[{effect.clip_ref}@{effect.at}]: '{effect.kind}': {exc}"
    return None


class EditsPlanExecutor:
    def __init__(self, folder: Path, compilation_id: str, library: Library | None = None):
        self._folder = folder
        self._compilation_id = compilation_id
        self._library = library or Library()
        self.last_output: str | None = None

    def preflight(self, plan: EditPlan) -> list[str]:
        problems = []
        if not read_journal(self._folder):
            problems.append("compilation has no journal entries")
        if plan.cards:
            problems.append("cards cannot be applied yet")
        if plan.sfx:
            problems.append("sfx cannot be applied yet")
        problems += [
            f"effect '{kind}' cannot be applied yet"
            for kind in sorted({e.kind for e in plan.effects} - set(_MUTATOR_KINDS))
        ]
        problems += [
            problem
            for effect in plan.effects
            if effect.kind in _MUTATOR_KINDS and (problem := _dry_run(effect))
        ]
        if plan.music and (problem := self._music_problem(plan.music.song_id)):
            problems.append(problem)
        return problems

    def _music_candidate(self, song_id: str) -> Path | None:
        """Library song first, then a plain file path; None when missing."""
        row = self._library.get(song_id)
        candidate = Path(row["audio_path"]) if row and row.get("audio_path") else Path(song_id)
        return candidate if candidate.is_file() else None

    def _music_problem(self, song_id: str) -> str | None:
        """Why `song_id` cannot be used: missing, or not licensed royalty_free
        (owner_recs and unlicensed music never go into a compilation)."""
        candidate = self._music_candidate(song_id)
        if candidate is None:
            return f"music: song '{song_id}' not found"
        folder = music_folder_of(candidate)
        if folder != "royalty_free":
            return f"music: song '{song_id}' is {folder}; refused"
        return None

    def _music_file(self, song_id: str) -> Path | None:
        """The song's file, only when it is licensed royalty_free music."""
        return None if self._music_problem(song_id) else self._music_candidate(song_id)

    def compilation_folder(self, compilation_id: str) -> Path:
        return self._folder

    def current_version(self, compilation_id: str) -> str:
        return str(len(read_journal(self._folder)))

    def _edit(self, mutator: Callable, action: str, details: dict) -> None:
        summary = _do_edit(
            self._folder, mutator, self._compilation_id, action=action, details=details
        )
        self.last_output = summary.get("output")

    def _clip_id(self, ref: str) -> str:
        """Resolve a plan `clip_ref` to a clip id.

        Refs are positional, so they mean the compilation as it was before the
        plan ran (the journal snapshot); resolving against the current spec
        would shift them after the reorder step.
        """
        progress = load_progress(self._folder)
        entries = read_journal(self._folder)
        original = entries[int(progress.snapshot_version) - 1]["spec"]
        try:
            return original["clips"][resolve_clip_ref(original, ref)]["id"]
        except KeyError as exc:
            raise UnsupportedPlanStepError(f"clip_ref '{ref}' not in compilation: {exc}") from None

    def apply_source_clips(self, plan: EditPlan) -> None:
        for clip in plan.source_clips:
            self._clip_id(clip.clip_ref)

    def apply_order(self, plan: EditPlan) -> None:
        if not plan.order:
            return
        spec = load_spec(self._folder)
        ordered = [self._clip_id(ref) for ref in plan.order]
        rest = [c["id"] for c in spec["clips"] if c["id"] not in ordered]

        def mutator(s: dict) -> tuple[dict, set[str]]:
            return spec_reorder_clips_explicit(s, ordered + rest)

        self._edit(mutator, "plan:order", {"clip_ids": ordered + rest})

    def apply_cards(self, plan: EditPlan) -> None:
        """Nothing to do: `preflight` rejects plans that contain cards."""

    def apply_effects(self, plan: EditPlan) -> None:
        """All effects in one mutation, one render and one journal entry, so
        the step is atomic: a failure adds none of them and a resume cannot
        apply any twice."""
        if not plan.effects:
            return
        spec = load_spec(self._folder)
        batch = [
            (
                resolve_clip_ref(spec, self._clip_id(effect.clip_ref)),
                effect_payload(_effect_body(effect)),
            )
            for effect in plan.effects
        ]

        def mutator(s: dict) -> tuple[dict, set[str]]:
            return spec_add_effects(s, batch)

        self._edit(
            mutator,
            "plan:effects",
            {
                "effects": [
                    {"clip_ref": effect.clip_ref, "effect": payload}
                    for effect, (_idx, payload) in zip(plan.effects, batch, strict=True)
                ]
            },
        )

    def apply_sfx(self, plan: EditPlan) -> None:
        """Nothing to do: `preflight` rejects plans that contain sfx."""

    def warnings(self, plan: EditPlan) -> list[str]:
        """Non-fatal: parts of a valid plan this executor accepts but ignores."""
        music = plan.music
        if music is None or not (
            music.trim_to_peak or music.trim_start_s is not None or music.trim_end_s is not None
        ):
            return []
        return [
            f"music: trim (start={music.trim_start_s}, end={music.trim_end_s}, "
            f"trim_to_peak={music.trim_to_peak}) not applied yet; the full song plays from 0 s"
        ]

    def apply_music(self, plan: EditPlan) -> None:
        """Sets the song only; any trim is reported by `warnings`, not rendered."""
        if not plan.music:
            return
        music = plan.music
        problem = self._music_problem(music.song_id)
        if problem:
            raise UnsupportedPlanStepError(problem)
        music_path = self._music_candidate(music.song_id).as_posix()
        music_folder = music_folder_of(music_path)

        def mutator(s: dict) -> tuple[dict, set[str]]:
            # music_folder rides on the spec so the Drive guard sees provenance.
            return {**s, "music_path": music_path, "music_folder": music_folder}, set()

        self._edit(mutator, "plan:music", {"song_id": music.song_id, "music_path": music_path})
