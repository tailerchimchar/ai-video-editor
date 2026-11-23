import json
import shutil
from pathlib import Path

import pytest

from ai_video_editor.grokbot import digest as dg
from ai_video_editor.league.play_models import (
    GameModel,
    Play,
    PlayResult,
    PlaySides,
    PlayTimes,
)

FIXTURE = Path(__file__).parent / "fixtures" / "panel_round"


@pytest.fixture
def fixture_round(tmp_path):
    dest = tmp_path / "2026-01-01_fixture"
    shutil.copytree(FIXTURE, dest)
    return dest


def _play(pid: str, first: float, last: float) -> Play:
    return Play(
        id=pid,
        type="skirmish",
        recording_time=PlayTimes(first_kill_s=first, last_kill_s=last),
        zone_relative="mid_lane",
        sides=PlaySides(numbers="3v3"),
        result=PlayResult(enemy_deaths=2, ally_deaths=1, outcome="won"),
    )


def _digest(*clips: tuple[float, float]) -> dg.Digest:
    return dg.Digest(
        game="lol",
        round="r",
        built_at="t",
        clips=[
            dg.DigestClip(ref=f"{i}m0s", event="kill", start_s=s, end_s=e)
            for i, (s, e) in enumerate(clips)
        ],
    )


def test_v2_digest_json_loads_with_game_model_absent():
    old = {"game": "lol", "round": "r", "clips": [], "built_at": "t"}
    d = dg.Digest.model_validate(old)
    assert d.game_model is None
    assert d.version == 3
    assert d.game == "lol"


def test_attach_plays_links_clip_by_midpoint_then_overlap():
    gm = GameModel(plays=[_play("p14", 1310, 1312), _play("p15", 1395, 1410)])
    d = dg.attach_plays(_digest((1390, 1415), (1300, 1313), (1313.5, 1316), (50, 60)), gm)
    assert [c.play_id for c in d.clips] == ["p15", "p14", None, None]
    assert d.game_model is gm
    assert all(p.text for p in gm.plays)


def test_attach_plays_overlap_fallback():
    # midpoint 1380 is in no play; p15 overlaps the clip, p14 does not.
    gm = GameModel(plays=[_play("p15", 1395, 1410)])
    d = dg.attach_plays(_digest((1350, 1410)), gm)
    assert d.clips[0].play_id == "p15"


def test_play_text_is_templated_and_passes_leak_check(fixture_round):
    gm = GameModel(plays=[_play("p1", 100, 110)])
    d = dg.build_digest(fixture_round, game="lol", game_model=gm)
    assert d.game_model.plays[0].text == "3v3 skirmish in mid_lane, owner absent, won 2-for-1"
    transcript = fixture_round / "transcript.json"
    transcript.write_text(
        json.dumps([{"start": 0, "end": 3, "text": "jhin kills miss fortune in mid lane"}]),
        encoding="utf-8",
    )
    assert dg.digest_leaks(d, transcript) == []
    # the exemption is scoped to game_model: the same phrase in a clip voice still leaks
    d.clips[0].voice = "he fights in mid lane"
    assert dg.digest_leaks(d, transcript) != []


def test_digest_json_is_pretty():
    out = _digest().model_dump_json(indent=2)
    assert '\n  "version": 3,' in out
