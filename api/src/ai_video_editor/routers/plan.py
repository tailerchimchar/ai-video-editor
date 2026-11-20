"""POST /compilations/{id}/plan: validate then execute an edit plan."""

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..edit_plan import (
    EditPlan,
    PlanNotExecutableError,
    PlanProgress,
    execute_plan,
    validate_plan,
)
from .edits import _load_compilation_folder, _update_compilation_output
from .plan_executor import EditsPlanExecutor, UnsupportedPlanStepError

router = APIRouter(tags=["edit"], prefix="/compilations")


class PlanResponse(BaseModel):
    progress: PlanProgress
    problems: list[str]
    """Non-fatal: parts of the applied plan that were accepted but not rendered."""


@router.post("/{compilation_id}/plan", response_model=PlanResponse)
async def apply_plan(compilation_id: str, plan: EditPlan):
    """Validate the plan against the registry; if clean, snapshot and execute it.

    Invalid plans are rejected with 422 before anything is rendered. A plan
    the executor cannot apply returns 409 with the reasons, also before
    anything is rendered. A plan that fails mid-run returns 409 and can be re-posted to resume.
    """
    problems = validate_plan(plan)
    if problems:
        raise HTTPException(422, {"problems": problems})
    folder = await _load_compilation_folder(compilation_id)
    executor = EditsPlanExecutor(folder, compilation_id)
    try:
        progress = await asyncio.to_thread(execute_plan, plan, compilation_id, executor)
    except PlanNotExecutableError as exc:
        raise HTTPException(409, {"unsupported": exc.problems}) from None
    except (UnsupportedPlanStepError, KeyError, ValueError) as exc:
        raise HTTPException(409, f"plan stopped: {exc}") from None
    if executor.last_output:
        await _update_compilation_output(compilation_id, executor.last_output)
    return PlanResponse(progress=progress, problems=executor.warnings(plan))


@router.get("/{compilation_id}/plan/version")
async def plan_version(compilation_id: str) -> dict:
    """The compilation's current journal version, for optimistic-concurrency checks."""
    folder = await _load_compilation_folder(compilation_id)
    return {"version": EditsPlanExecutor(folder, compilation_id).current_version(compilation_id)}
