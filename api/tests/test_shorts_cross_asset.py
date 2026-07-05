"""Cross-asset "best of" montage tests. Pure functions + tmp_path scans
— no ffmpeg, no network.

Covers:
- Session date prefix parsing (both `_HHhMM` and `_<Champion>` variants)
- `_scan_sessions` window filtering (day / week / all)
- Cross-asset ranking: group_size > hype_score > session_date
- `preview_cross_asset_montage` end-to-end (walks tmp workspace)
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ai_video_editor.shorts import (
    _CrossAssetClip,
    _gather_cross_asset_multikills,
    _parse_session_date,
    _rank_cross_asset_clips,
    _scan_sessions,
    preview_cross_asset_montage,
)

# ---------------------------------------------------------------------
# Session date parsing
# ---------------------------------------------------------------------


def test_parse_session_date_with_time_suffix() -> None:
    """`MM-DD-YYYY_HHhMM` → parses the leading date."""
    assert _parse_session_date("05-15-2026_19h55") == date(2026, 5, 15)
    assert _parse_session_date("07-04-2026_15h15") == date(2026, 7, 4)


def test_parse_session_date_with_champion_suffix() -> None:
    """Riot-correlated folders trail with `_<Champion>` (no time)."""
    assert _parse_session_date("05-15-2026_Jhin") == date(2026, 5, 15)
    assert _parse_session_date("07-04-2026_Ezreal") == date(2026, 7, 4)


def test_parse_session_date_bad_returns_none() -> None:
    """Non-date-prefix folder names return None (skip, don't crash)."""
    assert _parse_session_date("clips_2026-07-04") is None
    assert _parse_session_date("random-folder") is None
    assert _parse_session_date("") is None


def test_parse_session_date_invalid_month_returns_none() -> None:
    """13-99-2026 doesn't parse to a real date."""
    assert _parse_session_date("13-01-2026_10h00") is None


# ---------------------------------------------------------------------
# Session scan windowing
# ---------------------------------------------------------------------


def _make_session_folder(root: Path, name: str) -> Path:
    """Create an empty session folder — the scan reads folder names,
    not contents."""
    d = root / name
    d.mkdir(parents=True)
    return d


def test_scan_sessions_all_returns_every_parseable_folder(
    tmp_path, monkeypatch
) -> None:
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    root = tmp_path / "highlights" / "League of Legends"
    _make_session_folder(root, "05-15-2026_19h55")
    _make_session_folder(root, "05-29-2026_20h00")
    _make_session_folder(root, "07-04-2026_15h15")
    _make_session_folder(root, "not-a-date")  # ignored

    sessions = _scan_sessions(
        "League of Legends", "all", today=date(2026, 7, 5)
    )
    assert len(sessions) == 3
    assert [s.date for s in sessions] == [
        date(2026, 7, 4),
        date(2026, 5, 29),
        date(2026, 5, 15),
    ]


def test_scan_sessions_week_filters_within_7_days(tmp_path, monkeypatch) -> None:
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    root = tmp_path / "highlights" / "League of Legends"
    _make_session_folder(root, "07-04-2026_15h15")  # in
    _make_session_folder(root, "06-29-2026_10h00")  # in (6 days out)
    _make_session_folder(root, "06-27-2026_10h00")  # out (8 days out)

    sessions = _scan_sessions(
        "League of Legends", "week", today=date(2026, 7, 5)
    )
    dates = {s.date for s in sessions}
    assert dates == {date(2026, 7, 4), date(2026, 6, 29)}


def test_scan_sessions_day_includes_today_and_yesterday(
    tmp_path, monkeypatch
) -> None:
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    root = tmp_path / "highlights" / "League of Legends"
    _make_session_folder(root, "07-05-2026_10h00")  # today, in
    _make_session_folder(root, "07-04-2026_15h15")  # yesterday, in
    _make_session_folder(root, "07-03-2026_10h00")  # 2 days ago, out

    sessions = _scan_sessions(
        "League of Legends", "day", today=date(2026, 7, 5)
    )
    dates = {s.date for s in sessions}
    assert dates == {date(2026, 7, 5), date(2026, 7, 4)}


def test_scan_sessions_missing_root_returns_empty(tmp_path, monkeypatch) -> None:
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    # No highlights folder at all
    sessions = _scan_sessions(
        "League of Legends", "all", today=date(2026, 7, 5)
    )
    assert sessions == []


def test_scan_sessions_invalid_window_raises() -> None:
    with pytest.raises(ValueError, match="unknown window"):
        _scan_sessions("League of Legends", "month", today=date(2026, 7, 5))


# ---------------------------------------------------------------------
# Cross-asset ranking
# ---------------------------------------------------------------------


def _dummy_clip(file: str, anchor: float, hype: float, event: str = "kill"):
    from ai_video_editor.shorts import ShortClip

    return ShortClip(
        file=file,
        path=Path("/tmp") / file,
        event_type=event,
        start_seconds=anchor - 5,
        end_seconds=anchor + 5,
        hype_score=hype,
        funny_score=0.5,
        story_score=0.5,
        reason="",
    )


def _dummy_session(name: str, d: date):
    from ai_video_editor.shorts import SessionRef

    return SessionRef(folder=Path("/tmp") / name, date=d, game="League of Legends")


def test_rank_3plus_beats_2kill_regardless_of_hype() -> None:
    """A triple kill with hype=0.5 should still outrank a double kill
    with hype=0.99."""
    sess_a = _dummy_session("a", date(2026, 5, 15))
    sess_b = _dummy_session("b", date(2026, 7, 4))
    ranked = _rank_cross_asset_clips(
        [
            _CrossAssetClip(
                clip=_dummy_clip("double1.mp4", 1000, hype=0.99),
                session=sess_b,
                group_size=2,
            ),
            _CrossAssetClip(
                clip=_dummy_clip("triple1.mp4", 500, hype=0.5),
                session=sess_a,
                group_size=3,
            ),
        ],
        max_clips=5,
    )
    assert ranked[0].clip.file == "triple1.mp4"


def test_rank_same_group_size_hype_wins() -> None:
    """When group_size ties, higher hype comes first."""
    sess = _dummy_session("s", date(2026, 7, 4))
    ranked = _rank_cross_asset_clips(
        [
            _CrossAssetClip(
                clip=_dummy_clip("low.mp4", 1000, hype=0.7),
                session=sess,
                group_size=2,
            ),
            _CrossAssetClip(
                clip=_dummy_clip("high.mp4", 2000, hype=0.95),
                session=sess,
                group_size=2,
            ),
        ],
        max_clips=5,
    )
    assert ranked[0].clip.file == "high.mp4"


def test_rank_caps_at_max_clips() -> None:
    """Only the top `max_clips` survive."""
    sess = _dummy_session("s", date(2026, 7, 4))
    clips = [
        _CrossAssetClip(
            clip=_dummy_clip(f"c{i}.mp4", i * 100, hype=0.9),
            session=sess,
            group_size=2,
        )
        for i in range(10)
    ]
    ranked = _rank_cross_asset_clips(clips, max_clips=3)
    assert len(ranked) == 3


def test_rank_deterministic_across_runs() -> None:
    """Same input → same output order every time."""
    sess = _dummy_session("s", date(2026, 7, 4))
    clips = [
        _CrossAssetClip(
            clip=_dummy_clip(f"c{i}.mp4", i * 100, hype=0.9),
            session=sess,
            group_size=2,
        )
        for i in range(5)
    ]
    r1 = _rank_cross_asset_clips(clips, max_clips=5)
    r2 = _rank_cross_asset_clips(clips, max_clips=5)
    assert [c.clip.file for c in r1] == [c.clip.file for c in r2]


# ---------------------------------------------------------------------
# preview_cross_asset_montage (walks filesystem)
# ---------------------------------------------------------------------


def _write_session_index(folder: Path, clip_entries: list[dict]) -> None:
    """Write a minimal `index.json` a la the real highlights pipeline."""
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "game": "League of Legends",
        "champion": None,
        "clips_written": len(clip_entries),
        "clips_total": len(clip_entries),
        "clips": clip_entries,
    }
    (folder / "index.json").write_text(json.dumps(payload), encoding="utf-8")
    # Also create the dummy .mp4 files so paths resolve; we don't render.
    for e in clip_entries:
        (folder / e["file"]).write_bytes(b"")


def _clip_entry(name: str, start: float, end: float, hype: float, event="kill"):
    return {
        "file": name,
        "event": event,
        "start_seconds": start,
        "end_seconds": end,
        "hype_score": hype,
        "funny_score": 0.5,
        "story_score": 0.5,
        "reason": "",
        "ok": True,
    }


def test_preview_picks_3plus_from_recent_session(
    tmp_path, monkeypatch
) -> None:
    """A 3-kill cluster in a recent session should be the top pick."""
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    root = tmp_path / "highlights" / "League of Legends"
    # Recent session: a triple-kill cluster within 30s
    _write_session_index(
        root / "07-04-2026_15h15",
        [
            _clip_entry("k1.mp4", 995, 1005, hype=0.9),
            _clip_entry("k2.mp4", 1005, 1015, hype=0.9),
            _clip_entry("k3.mp4", 1015, 1025, hype=0.9),
        ],
    )
    # Older session with just a double-kill
    _write_session_index(
        root / "05-15-2026_19h55",
        [
            _clip_entry("d1.mp4", 500, 510, hype=0.9),
            _clip_entry("d2.mp4", 510, 520, hype=0.9),
        ],
    )

    result = preview_cross_asset_montage(
        game="League of Legends",
        window="all",
        today=date(2026, 7, 5),
        max_clips=10,
    )
    assert result["ok"] is True
    assert result["sessions_scanned_count"] == 2
    # Top candidate should be a group_size=3 clip
    assert result["candidates"][0]["group_size"] == 3
    assert result["candidates"][0]["is_high_priority"] is True


def test_preview_empty_when_no_sessions(tmp_path, monkeypatch) -> None:
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    result = preview_cross_asset_montage(
        game="League of Legends",
        window="all",
        today=date(2026, 7, 5),
    )
    assert result["ok"] is True
    assert result["sessions_scanned_count"] == 0
    assert result["candidates"] == []


def test_preview_invalid_window_returns_error(tmp_path, monkeypatch) -> None:
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    result = preview_cross_asset_montage(
        game="League of Legends",
        window="month",  # invalid
        today=date(2026, 7, 5),
    )
    assert result["ok"] is False
    assert "unknown window" in result["error"]


def test_gather_skips_sessions_without_multikills(
    tmp_path, monkeypatch
) -> None:
    """A session with only isolated single kills (no adjacency cluster)
    should contribute nothing to the montage."""
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    root = tmp_path / "highlights" / "League of Legends"
    # Kills far apart — no adjacency cluster
    _write_session_index(
        root / "07-04-2026_15h15",
        [
            _clip_entry("k1.mp4", 100, 110, hype=0.9),
            _clip_entry("k2.mp4", 500, 510, hype=0.9),
        ],
    )
    sessions = _scan_sessions(
        "League of Legends", "all", today=date(2026, 7, 5)
    )
    gathered = _gather_cross_asset_multikills(sessions)
    assert gathered == []
