"""Bucketing + planning tests. Pure functions — no ffmpeg, no I/O.

Verifies the determinism rules in the plan file
`.claude/plans/cached-wibbling-hejlsberg.md`:

- Same clip metadata always lands in the same buckets
- Same clip list + args produces the same ShortPlan sequence
- Phase cutoffs match the 0-15/15-25/25+ minute rules
- Multikill / teamfight adjacency detection
- Outplay reason-string heuristics
- Voice-over emits 1 short per clip; montage groups adjacent clips
"""

from __future__ import annotations

from pathlib import Path

from ai_video_editor.shorts import (
    ShortClip,
    _find_adjacent_indices,
    categorize_clips,
    plan_shorts,
    short_filename,
)


def _clip(
    file: str,
    anchor: float,
    event: str = "kill",
    hype: float = 0.8,
    reason: str = "",
) -> ShortClip:
    """Build a ShortClip whose anchor midpoint lands exactly at `anchor`."""
    return ShortClip(
        file=file,
        path=Path("/tmp") / file,
        event_type=event,
        start_seconds=anchor - 5,
        end_seconds=anchor + 5,
        hype_score=hype,
        funny_score=0.5,
        story_score=0.5,
        reason=reason,
    )


# ---------------------------------------------------------------------
# Phase bucketing
# ---------------------------------------------------------------------


def test_phase_cutoffs_match_plan() -> None:
    """0-15 laning, 15-25 mid, 25+ late. Boundaries are `< 900` and `< 1500`."""
    clips = [
        _clip("laning.mp4", anchor=100),
        _clip("laning_edge.mp4", anchor=899),
        _clip("mid_edge.mp4", anchor=900),
        _clip("mid_late.mp4", anchor=1499),
        _clip("late.mp4", anchor=1500),
        _clip("very_late.mp4", anchor=2500),
    ]
    b = categorize_clips(clips)
    laning = {c.file for c in b.get("laning_phase", [])}
    mid = {c.file for c in b.get("mid_game", [])}
    late = {c.file for c in b.get("late_game", [])}
    assert laning == {"laning.mp4", "laning_edge.mp4"}
    assert mid == {"mid_edge.mp4", "mid_late.mp4"}
    assert late == {"late.mp4", "very_late.mp4"}


def test_no_bucket_overlap_between_phases() -> None:
    clips = [_clip("a.mp4", anchor=500), _clip("b.mp4", anchor=1200)]
    b = categorize_clips(clips)
    assert "a.mp4" not in {c.file for c in b.get("mid_game", [])}
    assert "b.mp4" not in {c.file for c in b.get("laning_phase", [])}


# ---------------------------------------------------------------------
# First blood
# ---------------------------------------------------------------------


def test_first_blood_is_earliest_kill_only() -> None:
    clips = [
        _clip("assist_early.mp4", anchor=200, event="assist"),
        _clip("first_kill.mp4", anchor=350, event="kill"),
        _clip("second_kill.mp4", anchor=500, event="kill"),
    ]
    b = categorize_clips(clips)
    fb = b.get("first_blood", [])
    # first_blood picks the earliest KILL-ish; assist_early counts as
    # kill-ish (per _is_kill_event including assist).
    assert len(fb) == 1
    assert fb[0].file == "assist_early.mp4"


def test_first_blood_uses_full_clip_list_when_provided() -> None:
    """Regression: when threshold-filtered `clips` drops the actual first
    kill, we must NOT re-label a later kill as first_blood. Passing
    `first_blood_source` fixes this."""
    all_clips = [
        _clip("first.mp4", anchor=100, event="kill", hype=0.4),   # dropped by threshold
        _clip("second.mp4", anchor=300, event="kill", hype=0.9),  # keeps
        _clip("third.mp4", anchor=500, event="kill", hype=0.9),   # keeps
    ]
    kept = [c for c in all_clips if c.hype_score >= 0.6]
    # Naive call (no source) → misidentifies second as first blood
    naive = categorize_clips(kept)
    assert naive["first_blood"][0].file == "second.mp4"
    # Correct call (source=full) → first_blood is empty because the
    # actual first kill is below threshold + not in `kept`
    corrected = categorize_clips(kept, first_blood_source=all_clips)
    assert not corrected.get("first_blood")


def test_first_blood_included_when_actual_first_is_kept() -> None:
    """When the actual earliest kill IS above threshold, it stays in
    first_blood even when `first_blood_source` is passed."""
    all_clips = [
        _clip("first.mp4", anchor=100, event="kill", hype=0.9),
        _clip("second.mp4", anchor=300, event="kill", hype=0.9),
    ]
    kept = [c for c in all_clips if c.hype_score >= 0.6]
    buckets = categorize_clips(kept, first_blood_source=all_clips)
    assert [c.file for c in buckets["first_blood"]] == ["first.mp4"]


def test_first_blood_ignores_non_kill_events() -> None:
    clips = [
        _clip("audio.mp4", anchor=100, event="audio_peak"),
        _clip("first.mp4", anchor=200, event="kill"),
    ]
    b = categorize_clips(clips)
    fb = b.get("first_blood", [])
    assert len(fb) == 1
    assert fb[0].file == "first.mp4"


# ---------------------------------------------------------------------
# Multikill
# ---------------------------------------------------------------------


def test_multikill_requires_two_or_more_adjacent_kills() -> None:
    # Three kills all within 30s of each other -> multikill
    clips = [
        _clip("k1.mp4", anchor=1000, event="kill"),
        _clip("k2.mp4", anchor=1015, event="kill"),
        _clip("k3.mp4", anchor=1040, event="kill"),
    ]
    b = categorize_clips(clips)
    mk = {c.file for c in b.get("multikill", [])}
    assert mk == {"k1.mp4", "k2.mp4", "k3.mp4"}


def test_multikill_gap_over_30s_breaks_group() -> None:
    clips = [
        _clip("solo1.mp4", anchor=100, event="kill"),
        _clip("solo2.mp4", anchor=500, event="kill"),  # 400s apart
    ]
    b = categorize_clips(clips)
    assert not b.get("multikill")


def test_multikill_requires_kill_events_not_audio_peak() -> None:
    clips = [
        _clip("audio1.mp4", anchor=1000, event="audio_peak"),
        _clip("audio2.mp4", anchor=1015, event="audio_peak"),
    ]
    b = categorize_clips(clips)
    assert not b.get("multikill")


# ---------------------------------------------------------------------
# Teamfight — 3+ adjacent within 60s
# ---------------------------------------------------------------------


def test_teamfight_needs_three_adjacent_within_60s() -> None:
    clips = [
        _clip("t1.mp4", anchor=1500, event="kill"),
        _clip("t2.mp4", anchor=1520, event="assist"),
        _clip("t3.mp4", anchor=1550, event="death"),
    ]
    b = categorize_clips(clips)
    tf = {c.file for c in b.get("teamfight", [])}
    assert tf == {"t1.mp4", "t2.mp4", "t3.mp4"}


def test_teamfight_two_adjacent_does_not_qualify() -> None:
    clips = [
        _clip("k1.mp4", anchor=1500, event="kill"),
        _clip("k2.mp4", anchor=1510, event="kill"),
    ]
    b = categorize_clips(clips)
    assert not b.get("teamfight")


# ---------------------------------------------------------------------
# Objective steal
# ---------------------------------------------------------------------


def test_objective_steal_recognizes_baron_dragon_herald() -> None:
    clips = [
        _clip("baron.mp4", anchor=1400, event="baron"),
        _clip("dragon.mp4", anchor=1200, event="dragon"),
        _clip("herald.mp4", anchor=600, event="herald"),
        _clip("kill.mp4", anchor=800, event="kill"),  # not an objective
    ]
    b = categorize_clips(clips)
    obj = {c.file for c in b.get("objective_steal", [])}
    assert obj == {"baron.mp4", "dragon.mp4", "herald.mp4"}


# ---------------------------------------------------------------------
# Outplay heuristic
# ---------------------------------------------------------------------


def test_outplay_matches_2v3_pattern() -> None:
    clips = [
        _clip("op.mp4", anchor=1200, reason="Insane 2v3 outplay with the ult"),
        _clip("plain.mp4", anchor=1300, reason="Solo kill top lane"),
    ]
    b = categorize_clips(clips)
    op = {c.file for c in b.get("outplay", [])}
    assert op == {"op.mp4"}


def test_outplay_matches_case_insensitive() -> None:
    clips = [_clip("op.mp4", anchor=800, reason="OUTNUMBERED but clutch")]
    b = categorize_clips(clips)
    assert {c.file for c in b.get("outplay", [])} == {"op.mp4"}


def test_outplay_ignores_plain_multikill_language() -> None:
    clips = [_clip("plain.mp4", anchor=800, reason="Double kill in mid lane")]
    b = categorize_clips(clips)
    assert not b.get("outplay")


# ---------------------------------------------------------------------
# Determinism — pillar for the whole design
# ---------------------------------------------------------------------


def test_categorize_is_deterministic() -> None:
    clips = [
        _clip("a.mp4", anchor=100),
        _clip("b.mp4", anchor=1500),
        _clip("c.mp4", anchor=1520),
        _clip("d.mp4", anchor=1540),
    ]
    b1 = categorize_clips(clips)
    b2 = categorize_clips(clips)
    # Same buckets, same clips, same order
    assert list(b1.keys()) == list(b2.keys())
    for k in b1:
        assert [c.file for c in b1[k]] == [c.file for c in b2[k]]


def test_categorize_stable_under_input_reorder() -> None:
    a = _clip("early.mp4", anchor=100)
    b = _clip("late.mp4", anchor=2000)
    forward = categorize_clips([a, b])
    reversed_ = categorize_clips([b, a])
    for bucket in forward:
        assert [c.file for c in forward[bucket]] == [
            c.file for c in reversed_.get(bucket, [])
        ]


# ---------------------------------------------------------------------
# plan_shorts — voice-over vs montage
# ---------------------------------------------------------------------


def test_voiceover_mode_one_short_per_clip() -> None:
    clips = [
        _clip("k1.mp4", anchor=100, event="kill", hype=0.8),
        _clip("k2.mp4", anchor=500, event="kill", hype=0.7),
    ]
    plans = plan_shorts(clips, mode="voiceover", hype_threshold=0.5)
    # Both clips fall into laning_phase -> 2 voiceover plans
    laning_plans = [p for p in plans if p.bucket == "laning_phase"]
    assert len(laning_plans) == 2
    assert all(len(p.clips) == 1 for p in laning_plans)


def test_montage_mode_groups_adjacent_clips() -> None:
    clips = [
        _clip("k1.mp4", anchor=1500, event="kill"),
        _clip("k2.mp4", anchor=1520, event="kill"),  # 20s apart -> same group
        _clip("k3.mp4", anchor=1600, event="kill"),  # 80s apart -> new group
    ]
    plans = plan_shorts(clips, mode="montage", hype_threshold=0.5)
    # Look at the late_game bucket (all three are past 1500s)
    lg_plans = [p for p in plans if p.bucket == "late_game"]
    assert len(lg_plans) == 2
    # First group: k1 + k2. Second group: k3 alone.
    assert len(lg_plans[0].clips) == 2
    assert len(lg_plans[1].clips) == 1


def test_montage_respects_max_clips_per_short() -> None:
    clips = [
        _clip(f"k{i}.mp4", anchor=1500 + i * 5, event="kill") for i in range(7)
    ]
    plans = plan_shorts(
        clips, mode="montage", max_clips_per_short=3, hype_threshold=0.0
    )
    lg_plans = [p for p in plans if p.bucket == "late_game"]
    # 7 clips in one adjacency group, chunked into ceil(7/3) = 3 shorts
    assert len(lg_plans) == 3
    assert [len(p.clips) for p in lg_plans] == [3, 3, 1]


def test_hype_threshold_drops_below_threshold_clips() -> None:
    clips = [
        _clip("keep.mp4", anchor=100, hype=0.8),
        _clip("drop.mp4", anchor=200, hype=0.3),
    ]
    plans = plan_shorts(clips, mode="voiceover", hype_threshold=0.5)
    files = {c.file for p in plans for c in p.clips}
    assert "keep.mp4" in files
    assert "drop.mp4" not in files


def test_topic_filter_matches_bucket_substring() -> None:
    clips = [
        _clip("laning.mp4", anchor=100, event="kill", hype=0.8),
        _clip("mid.mp4", anchor=1000, event="kill", hype=0.8),
    ]
    plans = plan_shorts(clips, mode="voiceover", topic="laning", hype_threshold=0.5)
    files = {c.file for p in plans for c in p.clips}
    assert files == {"laning.mp4"}


def test_topic_filter_kill_matches_multikill_bucket() -> None:
    clips = [
        _clip("k1.mp4", anchor=1500, event="kill", hype=0.8),
        _clip("k2.mp4", anchor=1515, event="kill", hype=0.8),
    ]
    plans = plan_shorts(clips, mode="montage", topic="kill", hype_threshold=0.5)
    # `topic="kill"` should match `multikill` bucket AND anything with
    # "kill" in the slug (empty here). At minimum multikill grouped.
    assert any(p.bucket == "multikill" for p in plans)


def test_plan_indexes_are_sequential_and_deterministic() -> None:
    clips = [
        _clip("a.mp4", anchor=1500),
        _clip("b.mp4", anchor=1520),
        _clip("c.mp4", anchor=1800),
    ]
    plans = plan_shorts(clips, mode="voiceover", hype_threshold=0.5)
    indexes = [p.index for p in plans]
    assert indexes == list(range(1, len(plans) + 1))
    # Second run yields same indexes
    plans2 = plan_shorts(clips, mode="voiceover", hype_threshold=0.5)
    assert [p.index for p in plans2] == indexes


# ---------------------------------------------------------------------
# short_filename — deterministic
# ---------------------------------------------------------------------


def test_short_filename_uses_bucket_and_mmss() -> None:
    from ai_video_editor.shorts import ShortPlan

    plan = ShortPlan(
        bucket="teamfight",
        clips=(_clip("k1.mp4", anchor=1125),),
        title="TEAMFIGHT",
        vo_prompt="What I was thinking",
        index=4,
    )
    # 1125s = 18:45
    assert short_filename(plan) == "short_04_teamfight_18m45s.mp4"


# ---------------------------------------------------------------------
# _find_adjacent_indices — helper edge cases
# ---------------------------------------------------------------------


def test_find_adjacent_indices_empty() -> None:
    assert _find_adjacent_indices([], 30) == []


def test_find_adjacent_indices_single_clip() -> None:
    groups = _find_adjacent_indices([_clip("a.mp4", anchor=100)], 30)
    assert groups == [[0]]


def test_find_adjacent_indices_uses_running_last_not_first() -> None:
    # Chain: 0 -> 25 -> 50 -> 75, each 25s from the prior -> one group
    clips = [_clip(f"{i}.mp4", anchor=i * 25) for i in range(4)]
    groups = _find_adjacent_indices(clips, 30)
    assert groups == [[0, 1, 2, 3]]


# ---------------------------------------------------------------------
# Layout switch — cropped_hud vs blur_fill filter graphs
# ---------------------------------------------------------------------


def test_indexed_layout_chain_cropped_hud_rewrites_labels() -> None:
    from ai_video_editor.shorts import _indexed_layout_chain

    c0 = _indexed_layout_chain("cropped_hud", 0)
    c1 = _indexed_layout_chain("cropped_hud", 1)
    # Entry label rewritten per index
    assert "[0:v]" in c0 and "[0:v]" not in c1
    assert "[1:v]" in c1
    # Exit label always [vN]
    assert c0.endswith("[v0]")
    assert c1.endswith("[v1]")
    # Internal labels get the index suffix so a concat doesn't collide
    assert "[main0]" in c0
    assert "[main1]" in c1
    # blur-fill's labels shouldn't leak into cropped_hud
    assert "[fga0]" not in c0


def test_indexed_layout_chain_blur_fill_still_available() -> None:
    from ai_video_editor.shorts import _indexed_layout_chain

    chain = _indexed_layout_chain("blur_fill", 0)
    assert "boxblur" in chain
    assert chain.endswith("[v0]")


def test_indexed_layout_chain_unknown_raises() -> None:
    import pytest

    from ai_video_editor.shorts import _indexed_layout_chain

    with pytest.raises(ValueError):
        _indexed_layout_chain("blimey", 0)


def test_cropped_hud_graph_contains_hud_overlays() -> None:
    """The cropped_hud filter graph must overlay killfeed + minimap
    from the SOURCE (via split=3) — that's the whole point vs blur-fill."""
    from ai_video_editor.edits import cropped_hud_9x16

    g = cropped_hud_9x16()
    assert "split=3" in g
    # Killfeed ROI (from _ROI_PRESETS['killfeed_lol'])
    assert "iw*0.22:ih*0.28:iw*0.78:ih*0.08" in g
    # Minimap ROI (from _ROI_PRESETS['minimap_lol'])
    assert "ih*0.22:ih*0.22:iw-ih*0.23:ih*0.77" in g
    # Two overlays chained: killfeed onto bg, then minimap on top
    assert g.count("overlay=") == 2
