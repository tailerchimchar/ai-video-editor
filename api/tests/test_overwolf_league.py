"""League of Legends support in the Overwolf candidate source.

League's killfeed shows CHAMPION names, so identity and killfeed
verification must use the champion, not the summoner name. The fixture
is a trimmed excerpt of a real Outplayed log (one League game, 9 kills
including two double kills).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ai_video_editor.candidates import overwolf_events
from ai_video_editor.candidates.overwolf_events import (
    detect_overwolf_events,
    kill_anchors_for_video,
)
from ai_video_editor.overwolf_log import clear_dispatch_cache, player_identity

FIXTURE = Path(__file__).parent / "fixtures" / "overwolf_league_zeri.log"
VIDEO = r"C:\Videos\Overwolf\Outplayed\League of Legends\match_zeri.mp4"
DURATION = 1613.0

VALORANT_SESSION_LINE = (
    "2026-11-19 00:05:14,336 (INFO) </background.js> (:4) - "
    '[GameService:Valorant] Session start info {"me":{"player_name":'
    '"Noodlz#NBK","player_id":"abc","region":"na"}}'
)


def _make_log_dir(tmp_path: Path, monkeypatch, lines: list[str]) -> Path:
    clear_dispatch_cache()
    directory = tmp_path / "Overwolf" / "Log" / "Apps" / "Outplayed"
    directory.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    (directory / "background.html.1.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return directory


@pytest.fixture
def log_dir(tmp_path, monkeypatch) -> Path:
    return _make_log_dir(tmp_path, monkeypatch, FIXTURE.read_text(encoding="utf-8").splitlines())


def test_league_identity_reads_summoner_and_champion(log_dir) -> None:
    identity = player_identity(log_dir, game="league")
    assert identity == {
        "player_name": "Noodlz",
        "champion": "Zeri",
        "game": "league",
    }


def test_league_identity_accepts_outplayed_game_label(log_dir) -> None:
    identity = player_identity(log_dir, game="League of Legends_11-19-2026_1-18-39-442")
    assert identity is not None
    assert identity["champion"] == "Zeri"


def test_valorant_identity_is_unchanged(tmp_path) -> None:
    (tmp_path / "background.html.1.log").write_text(VALORANT_SESSION_LINE + "\n", encoding="utf-8")
    identity = player_identity(tmp_path)
    assert identity == {
        "player_name": "Noodlz#NBK",
        "player_id": "abc",
        "region": "na",
    }


def test_league_identity_missing_from_valorant_only_log(tmp_path) -> None:
    (tmp_path / "background.html.1.log").write_text(VALORANT_SESSION_LINE + "\n", encoding="utf-8")
    assert player_identity(tmp_path, game="league") is None


def test_champion_name_verifies_all_nine_kills(log_dir, monkeypatch) -> None:
    """The verifier matches the champion, not the summoner name."""
    seen_names: list[str] = []

    def champion_only(path, player_name, game, anchors):
        seen_names.append(player_name)
        return [player_name == "Zeri"] * len(anchors)

    monkeypatch.setattr(overwolf_events, "verify_windows_have_kill", champion_only)

    rows = detect_overwolf_events(VIDEO, DURATION, "League of Legends")
    kills = [r for r in rows if r["event_type"] == "kill"]

    assert seen_names == ["Zeri"]
    assert len(kills) == 9
    assert all(k["metadata"]["killfeed_verified"] for k in kills)


def test_kills_kept_unverified_without_champion(tmp_path, monkeypatch) -> None:
    dispatch_only = [
        line
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if "Dispatch new video created" in line
    ]
    _make_log_dir(tmp_path, monkeypatch, dispatch_only)

    def must_not_run(*_args):
        raise AssertionError("verifier ran without a champion name")

    monkeypatch.setattr(overwolf_events, "verify_windows_have_kill", must_not_run)

    rows = detect_overwolf_events(VIDEO, DURATION, "League of Legends")
    kills = [r for r in rows if r["event_type"] == "kill"]

    assert len(kills) == 9
    assert all(k["metadata"]["killfeed_verified"] is False for k in kills)


def test_kill_anchors_include_both_double_kill_clusters(log_dir, monkeypatch) -> None:
    monkeypatch.setattr(
        overwolf_events,
        "verify_windows_have_kill",
        lambda path, name, game, anchors: [name == "Zeri"] * len(anchors),
    )

    anchors = kill_anchors_for_video(VIDEO, DURATION)

    assert sum(a.event_type == "kill" for a in anchors) == 9
    doubles = [a for a in anchors if a.event_type == "multi_kill_2"]
    assert len(doubles) == 2
