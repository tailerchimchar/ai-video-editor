"""League of Legends support in the Overwolf candidate source.

League's killfeed shows CHAMPION names, so identity and killfeed
verification must use the champion, not the summoner name. The fixture
is a trimmed excerpt of a real Outplayed log (one League game, 9 kills
including two double kills).
"""

from __future__ import annotations

from datetime import datetime
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


def _league_session_line(stamp: str, champion: str) -> str:
    return (
        f"{stamp} (INFO) </background.js> (:4) - [GameService:League of Legends] "
        'Session start info {"summoner_info":{"name":"Noodlz","region":"NA",'
        f'"champion":"{champion}"}}}}'
    )


def _ms(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return int(datetime(year, month, day, hour, minute).timestamp() * 1000)


@pytest.fixture
def two_sessions(tmp_path) -> Path:
    """Akali game at 01:00, then Zeri game at 02:00."""
    (tmp_path / "background.html.1.log").write_text(
        _league_session_line("2026-11-19 01:00:00,000", "Akali")
        + "\n"
        + _league_session_line("2026-11-19 02:00:00,000", "Zeri")
        + "\n",
        encoding="utf-8",
    )
    return tmp_path


def test_identity_at_video_between_sessions_is_first_champion(two_sessions) -> None:
    identity = player_identity(two_sessions, game="league", at_ms=_ms(2026, 11, 19, 1, 30))
    assert identity["champion"] == "Akali"


def test_identity_at_video_after_last_session_is_last_champion(two_sessions) -> None:
    identity = player_identity(two_sessions, game="league", at_ms=_ms(2026, 11, 19, 3, 0))
    assert identity["champion"] == "Zeri"


def test_identity_without_at_ms_is_newest(two_sessions) -> None:
    assert player_identity(two_sessions, game="league")["champion"] == "Zeri"


def test_identity_before_any_session_falls_back_to_newest(two_sessions) -> None:
    identity = player_identity(two_sessions, game="league", at_ms=_ms(2026, 11, 19, 0, 0))
    assert identity["champion"] == "Zeri"


@pytest.fixture
def rotated_sessions(tmp_path) -> Path:
    """Name order is the reverse of time: `.10.log` (later) sorts before
    `.9.log` (earlier), so the last-scanned file holds the OLDER session."""
    (tmp_path / "background.html.9.log").write_text(
        _league_session_line("2026-11-19 01:00:00,000", "Akali") + "\n",
        encoding="utf-8",
    )
    (tmp_path / "background.html.10.log").write_text(
        _league_session_line("2026-11-19 02:00:00,000", "Zeri") + "\n",
        encoding="utf-8",
    )
    return tmp_path


def test_rotated_logs_after_both_sessions_pick_latest_by_time(rotated_sessions) -> None:
    identity = player_identity(rotated_sessions, game="league", at_ms=_ms(2026, 11, 19, 3, 0))
    assert identity["champion"] == "Zeri"


def test_rotated_logs_between_sessions_pick_earlier_by_time(rotated_sessions) -> None:
    identity = player_identity(rotated_sessions, game="league", at_ms=_ms(2026, 11, 19, 1, 30))
    assert identity["champion"] == "Akali"


def test_rotated_logs_without_at_ms_pick_latest_by_time(rotated_sessions) -> None:
    assert player_identity(rotated_sessions, game="league")["champion"] == "Zeri"


def test_league_identity_skips_nested_player_info_lines(tmp_path) -> None:
    nested = (
        "2026-11-19 01:00:00,000 (INFO) </background.js> (:4) - "
        '{"summoner_info":{"is_garena_user":"false","player_info":'
        '"{\\"displayName\\":\\"Noodlz\\"}"}}'
    )
    (tmp_path / "background.html.1.log").write_text(nested + "\n", encoding="utf-8")
    assert player_identity(tmp_path, game="league") is None


def test_league_identity_missing_from_valorant_only_log(tmp_path) -> None:
    (tmp_path / "background.html.1.log").write_text(VALORANT_SESSION_LINE + "\n", encoding="utf-8")
    assert player_identity(tmp_path, game="league") is None


def test_league_kills_survive_uncalibrated_killfeed_ocr(log_dir, monkeypatch) -> None:
    """League's killfeed crop is uncalibrated; log kills are kept unverified."""
    calls: list[tuple] = []

    def always_false(*args):
        calls.append(args)
        return [False] * len(args[-1])

    monkeypatch.setattr(overwolf_events, "verify_windows_have_kill", always_false)

    rows = detect_overwolf_events(VIDEO, DURATION, "League of Legends")
    kills = [r for r in rows if r["event_type"] == "kill"]

    assert calls == []
    assert len(kills) == 9
    assert all(k["metadata"]["killfeed_verified"] is False for k in kills)


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
