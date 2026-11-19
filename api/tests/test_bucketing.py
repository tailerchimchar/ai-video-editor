"""Tests for the content-type router (mentor bucket routing).

Pure -- exercises the routing rules with synthetic clip dicts. No
filesystem, no LLM, no real index.json.
"""

from __future__ import annotations

from ai_video_editor.bucketing import (
    BucketManifest,
    route_clip,
    route_session_index,
)


def _clip(
    *,
    file: str = "c.mp4",
    event: str = "kill",
    hype: float = 0.5,
    funny: float = 0.5,
    story: float = 0.5,
    ok: bool = True,
) -> dict:
    return {
        "file": file,
        "event": event,
        "hype_score": hype,
        "funny_score": funny,
        "story_score": story,
        "ok": ok,
    }


def test_ace_routes_to_hype() -> None:
    bucket, reason = route_clip(_clip(event="ace"))
    assert bucket == "hype"
    assert "ace" in reason


def test_multi_kill_3_routes_to_hype() -> None:
    assert route_clip(_clip(event="multi_kill_3"))[0] == "hype"


def test_multi_kill_4_routes_to_hype() -> None:
    assert route_clip(_clip(event="multi_kill_4"))[0] == "hype"


def test_pentakill_routes_to_hype() -> None:
    assert route_clip(_clip(event="pentakill"))[0] == "hype"


def test_multi_kill_2_stays_in_voice_over() -> None:
    """Per mentor rule 1: no 2Ks in hype. 2Ks are voice-over material."""
    assert route_clip(_clip(event="multi_kill_2"))[0] == "voice_over"


def test_single_kill_routes_to_voice_over() -> None:
    assert route_clip(_clip(event="kill"))[0] == "voice_over"


def test_funny_callout_routes_to_funny() -> None:
    assert route_clip(_clip(event="funny_callout"))[0] == "funny"


def test_hype_callout_routes_to_voice_over() -> None:
    """Hype callouts are talking; they're voice-over material."""
    assert route_clip(_clip(event="hype_callout"))[0] == "voice_over"


def test_high_funny_score_overrides_kill_to_funny() -> None:
    bucket, reason = route_clip(_clip(event="kill", funny=0.9, hype=0.5))
    assert bucket == "funny"
    assert "funny_score" in reason


def test_high_hype_beats_funny_override() -> None:
    """A kill with hype 0.9 stays voice_over even with funny 0.9 --
    because the funny override requires hype < 0.7."""
    bucket, _ = route_clip(_clip(event="kill", funny=0.9, hype=0.9))
    assert bucket == "voice_over"


def test_unknown_event_type_gets_skipped() -> None:
    bucket, reason = route_clip(_clip(event="some_new_thing"))
    assert bucket == "skipped"
    assert "did not match" in reason


def test_death_routes_to_voice_over() -> None:
    """Own deaths can be voice-over storytelling material."""
    assert route_clip(_clip(event="death"))[0] == "voice_over"


def test_clutch_routes_to_hype() -> None:
    """Mentor rule: 1v3+ clutches qualify for hype montage."""
    assert route_clip(_clip(event="clutch"))[0] == "hype"


def test_victory_routes_to_voice_over() -> None:
    """Round-win / match-won screens are reel-closer material."""
    assert route_clip(_clip(event="victory"))[0] == "voice_over"


def test_legacy_clip_event_type_routes_to_voice_over() -> None:
    """Old rankings before event_type propagation used 'clip' as generic --
    don't drop those on the floor; land them in voice_over."""
    assert route_clip(_clip(event="clip"))[0] == "voice_over"


def test_route_session_index_populates_buckets() -> None:
    idx = {
        "folder": "C:/x/y/07-10-2026_10h00",
        "clips": [
            _clip(file="01_ace_5m.mp4", event="ace"),
            _clip(file="02_kill_10m.mp4", event="kill"),
            _clip(file="03_funny_15m.mp4", event="funny_callout"),
            _clip(file="04_3k_20m.mp4", event="multi_kill_3"),
        ],
    }
    m = route_session_index(idx)
    assert isinstance(m, BucketManifest)
    assert m.session == "07-10-2026_10h00"
    assert [p.file for p in m.hype] == ["01_ace_5m.mp4", "04_3k_20m.mp4"]
    assert [p.file for p in m.voice_over] == ["02_kill_10m.mp4"]
    assert [p.file for p in m.funny] == ["03_funny_15m.mp4"]
    assert m.story == []
    assert m.total_routed() == 4


def test_route_session_skips_failed_cuts() -> None:
    """Clips where the ffmpeg cut failed (`ok: False`) don't route."""
    idx = {
        "folder": "session",
        "clips": [
            _clip(file="good.mp4", event="ace", ok=True),
            _clip(file="bad.mp4", event="ace", ok=False),
            _clip(file="", event="kill", ok=True),  # empty file
        ],
    }
    m = route_session_index(idx)
    assert [p.file for p in m.hype] == ["good.mp4"]
    assert m.total_routed() == 1
