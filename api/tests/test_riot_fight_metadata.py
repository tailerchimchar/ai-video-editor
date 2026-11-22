import pytest

from ai_video_editor.league.candidates.riot import _attach_fight_metadata
from ai_video_editor.league.riot_timeline import RiotTimeline, TimelineEvent


def _timeline(first_kind):
    events = [
        TimelineEvent(first_kind, 100.0, 100.0, [], {}),
        TimelineEvent("kill", 104.0, 104.0, [], {}),
    ]
    return RiotTimeline("m1", "high", 0.0, None, events, [])


@pytest.mark.parametrize(
    ("first_kind", "expected"), [("ally_death", True), ("death", True), ("kill", False)]
)
def test_fight_opens_with_ally_death_flag(first_kind, expected):
    rows = [{"metadata": {"anchor_seconds": 104.0}}]
    _attach_fight_metadata(rows, _timeline(first_kind))
    assert rows[0]["metadata"]["fight_opens_with_ally_death"] is expected
