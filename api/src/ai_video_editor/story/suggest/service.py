"""Orchestration for one round: read the rendered `story.json` and the cached
Riot timeline, run the engine, write `suggestions.json`. The router's
`editor_suggest` job and nothing else calls this."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ...config import settings
from ..cli import load_timeline
from ..planner import StoryPlan
from .engine import JevPanelEngine, SuggestionEngine
from .models import SuggestionSet
from .placement import video_second
from .store import FileSuggestionStore

STORY_FILE = "story.json"
# Only League has a builder today; another game passes its own key.
DEFAULT_GAME = "league"


class SuggestInputError(Exception):
    """A missing input: no rendered story, or no cached timeline."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_plan(story_dir: Path) -> StoryPlan:
    path = Path(story_dir) / STORY_FILE
    try:
        return StoryPlan.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SuggestInputError(
            f"no usable {STORY_FILE} in {story_dir}: render the story first"
        ) from e


def story_asset_id(story_dir: Path) -> str:
    asset_id = load_plan(story_dir).asset_id
    if not asset_id:
        raise SuggestInputError(f"{STORY_FILE} has no asset_id")
    return asset_id


def run_suggestions(
    round_name: str,
    story_dir: Path,
    *,
    engine: SuggestionEngine | None = None,
    game: str = DEFAULT_GAME,
) -> SuggestionSet:
    plan = load_plan(story_dir)
    timeline = load_timeline(plan.asset_id)
    if timeline is None:
        raise SuggestInputError(f"no usable Riot timeline cached for asset {plan.asset_id}")
    items = (engine or JevPanelEngine()).suggest(plan, timeline, game)
    card_s = settings.story_card_seconds
    items = [
        item.model_copy(
            update={
                "video_s": video_second(
                    plan, item.chapter_index, item.start_s + timeline.offset_s, card_s
                )
            }
        )
        for item in items
    ]
    result = SuggestionSet(round=round_name, created_at=now_iso(), status="ready", items=items)
    FileSuggestionStore(story_dir).save(result)
    return result
