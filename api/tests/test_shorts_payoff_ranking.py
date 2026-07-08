"""Payoff-type ranking + drop tests for the Outplayed pool ranker."""

from __future__ import annotations

from pathlib import Path

from ai_video_editor.shorts import (
    OutplayedClipRef,
    _AnalyzedOutplayedClip,
    _rank_analyzed_pool,
)


def _ref(name: str, mtime: float = 1_800_000_000.0) -> OutplayedClipRef:
    return OutplayedClipRef(
        path=Path("/tmp") / name,
        duration_seconds=30.0,
        mtime_epoch=mtime,
    )


def _a(
    name: str,
    payoff_type: str,
    confidence: float = 0.8,
    mtime: float = 1_800_000_000.0,
) -> _AnalyzedOutplayedClip:
    from ai_video_editor.shorts import _PAYOFF_TYPE_SCORE

    return _AnalyzedOutplayedClip(
        ref=_ref(name, mtime),
        analysis_payoff_type=payoff_type,
        analysis_confidence=confidence,
        analysis_description="",
        payoff_score=_PAYOFF_TYPE_SCORE[payoff_type],
    )


def test_drop_other_payoff_clips() -> None:
    """Death / downtime clips (payoff_type='other') are always dropped
    regardless of confidence."""
    analyzed = [
        _a("death1.mp4", "other", confidence=0.95),
        _a("kill1.mp4", "kill", confidence=0.7),
    ]
    kept, dropped = _rank_analyzed_pool(analyzed, limit=5)
    kept_files = {k.ref.path.name for k in kept}
    dropped_files = {d.ref.path.name for d in dropped}
    assert kept_files == {"kill1.mp4"}
    assert "death1.mp4" in dropped_files


def test_drop_below_confidence_floor() -> None:
    """Below the analyzer floor → dropped even for a real payoff."""
    analyzed = [
        _a("unsure_kill.mp4", "kill", confidence=0.3),
        _a("confident.mp4", "kill", confidence=0.8),
    ]
    kept, _ = _rank_analyzed_pool(
        analyzed, limit=5, min_confidence=0.5
    )
    files = {k.ref.path.name for k in kept}
    assert files == {"confident.mp4"}


def test_ace_beats_multi_beats_kill() -> None:
    """Payoff-type ranking: ace > multi_kill > kill (with same conf)."""
    analyzed = [
        _a("plain_kill.mp4", "kill", confidence=0.9),
        _a("ace.mp4", "ace", confidence=0.9),
        _a("multi.mp4", "multi_kill", confidence=0.9),
    ]
    kept, _ = _rank_analyzed_pool(analyzed, limit=3)
    order = [k.ref.path.name for k in kept]
    assert order == ["ace.mp4", "multi.mp4", "plain_kill.mp4"]


def test_confidence_breaks_ties_within_type() -> None:
    """Same payoff type → higher confidence first."""
    analyzed = [
        _a("low.mp4", "kill", confidence=0.6),
        _a("high.mp4", "kill", confidence=0.9),
    ]
    kept, _ = _rank_analyzed_pool(analyzed, limit=2)
    assert [k.ref.path.name for k in kept] == ["high.mp4", "low.mp4"]


def test_mtime_breaks_ties_within_confidence() -> None:
    """Same type + confidence → newer clip wins."""
    analyzed = [
        _a("older.mp4", "kill", confidence=0.8, mtime=1_000),
        _a("newer.mp4", "kill", confidence=0.8, mtime=2_000),
    ]
    kept, _ = _rank_analyzed_pool(analyzed, limit=2)
    assert [k.ref.path.name for k in kept] == ["newer.mp4", "older.mp4"]


def test_ranking_is_deterministic() -> None:
    """Same input → same output every time."""
    analyzed = [
        _a("a.mp4", "kill"),
        _a("b.mp4", "ace"),
        _a("c.mp4", "multi_kill"),
        _a("d.mp4", "other"),
    ]
    r1 = _rank_analyzed_pool(analyzed, limit=3)
    r2 = _rank_analyzed_pool(analyzed, limit=3)
    assert [k.ref.path.name for k in r1[0]] == [
        k.ref.path.name for k in r2[0]
    ]


def test_limit_caps_kept_pool() -> None:
    """Only `limit` clips survive to the kept list."""
    analyzed = [_a(f"c{i}.mp4", "kill") for i in range(10)]
    kept, dropped = _rank_analyzed_pool(analyzed, limit=3)
    assert len(kept) == 3
    # The rest get labeled 'outranked' (in dropped), not filtered out
    outranked = [d for d in dropped]
    assert len(outranked) == 7
