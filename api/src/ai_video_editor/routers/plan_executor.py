"""`PlanExecutor` backed by the compilation mutators in `routers/edits.py`.

Each step re-renders through `_do_edit`, which journals the new state, so the
journal version recorded before the plan is a valid revert point.

Supported today: verifying `source_clips` refs, `order`, clip effects (zoom,
focus, caption) and music (`music_path` on the spec). Cards, sfx and other
effect kinds cannot be expressed by the existing mutators; `preflight` reports
them so `execute_plan` refuses the whole plan before changing anything.
"""

import logging
from collections.abc import Callable
from pathlib import Path

from ..compile import add_effect as spec_add_effect
from ..compile import load_spec, resolve_clip_ref
from ..compile import reorder_clips_explicit as spec_reorder_clips_explicit
from ..compile_journal import read_journal
from ..edit_plan import EditPlan, load_progress
from ..music.library import Library
from .edits import AddEffectBody, _do_edit, _effect_from_body

logger = logging.getLogger(__name__)

_MUTATOR_KINDS = ("zoom", "focus", "caption")


class UnsupportedPlanStepError(RuntimeError):
    """The plan asks for something the current mutators cannot apply."""


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
        if plan.music and self._music_file(plan.music.song_id) is None:
            problems.append(f"music: song '{plan.music.song_id}' not found")
        return problems

    def _music_file(self, song_id: str) -> Path | None:
        """Library song first, then a plain file path."""
        row = self._library.get(song_id)
        candidate = Path(row["audio_path"]) if row and row.get("audio_path") else Path(song_id)
        return candidate if candidate.is_file() else None

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
        for effect in plan.effects:
            clip_id = self._clip_id(effect.clip_ref)
            body = AddEffectBody(clip_ref=clip_id, kind=effect.kind, **effect.params)
            idx = resolve_clip_ref(load_spec(self._folder), clip_id)
            payload = _effect_from_body(body)

            def mutator(s: dict, idx=idx, payload=payload) -> tuple[dict, set[str]]:
                return spec_add_effect(s, idx, payload)

            self._edit(
                mutator,
                f"plan:effect:{effect.kind}",
                {"clip_ref": effect.clip_ref, "effect": payload},
            )

    def apply_sfx(self, plan: EditPlan) -> None:
        """Nothing to do: `preflight` rejects plans that contain sfx."""

    def apply_music(self, plan: EditPlan) -> None:
        if not plan.music:
            return
        music = plan.music
        if music.trim_to_peak or music.trim_start_s is not None or music.trim_end_s is not None:
            logger.info("music trimming is not applied yet; using the full song")
        music_path = self._music_file(music.song_id).as_posix()

        def mutator(s: dict) -> tuple[dict, set[str]]:
            return {**s, "music_path": music_path}, set()

        self._edit(mutator, "plan:music", {"song_id": music.song_id, "music_path": music_path})
