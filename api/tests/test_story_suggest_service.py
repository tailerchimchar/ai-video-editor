"""service.run_suggestions: story.json + timeline -> engine -> suggestions.json."""

from __future__ import annotations

import pytest

from ai_video_editor.league.riot_timeline import RiotTimeline
from ai_video_editor.story.planner import Chapter, StoryPlan
from ai_video_editor.story.suggest import service
from ai_video_editor.story.suggest.models import SuggestionItem
from ai_video_editor.story.suggest.store import FileSuggestionStore

PLAN = StoryPlan(
    asset_id="a1",
    chapters=[Chapter(title="t", start_s=10, end_s=20, kind="fight")],
    total_s=10,
    mic_stream=None,
    audio="mixed",
)
TL = RiotTimeline("m", "high", 0.0, 100.0, [], [])
ITEM = SuggestionItem(
    id="s-x-zoom",
    kind="zoom",
    chapter_index=0,
    start_s=10,
    end_s=12,
    reason="r",
    backend="rules",
)


class FakeEngine:
    def __init__(self):
        self.calls = []

    def suggest(self, plan, timeline, game):
        self.calls.append((plan, timeline, game))
        return [ITEM]


def _write_story(d):
    d.mkdir(parents=True, exist_ok=True)
    (d / "story.json").write_text(PLAN.model_dump_json(), encoding="utf-8")


def test_run_writes_ready_set(tmp_path, monkeypatch):
    _write_story(tmp_path)
    monkeypatch.setattr(service, "load_timeline", lambda asset_id: TL)
    engine = FakeEngine()
    out = service.run_suggestions("r1", tmp_path, engine=engine)
    # Card (settings.story_card_seconds) + 0 s into the chapter: start_s 10 + offset 0.
    placed = ITEM.model_copy(update={"video_s": service.settings.story_card_seconds})
    assert out.status == "ready" and out.items == [placed] and out.round == "r1"
    assert engine.calls == [(PLAN, TL, "league")]
    assert FileSuggestionStore(tmp_path).load() == out


def test_missing_story_json(tmp_path):
    with pytest.raises(service.SuggestInputError, match=r"story\.json"):
        service.run_suggestions("r1", tmp_path, engine=FakeEngine())


def test_missing_timeline(tmp_path, monkeypatch):
    _write_story(tmp_path)
    monkeypatch.setattr(service, "load_timeline", lambda asset_id: None)
    with pytest.raises(service.SuggestInputError, match="timeline"):
        service.run_suggestions("r1", tmp_path, engine=FakeEngine())


def test_story_asset_id(tmp_path):
    _write_story(tmp_path)
    assert service.story_asset_id(tmp_path) == "a1"
    with pytest.raises(service.SuggestInputError):
        service.story_asset_id(tmp_path / "nope")
