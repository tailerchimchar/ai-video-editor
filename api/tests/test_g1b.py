"""G1b — game-aware transcription post-processing + sentiment.

Pure tests on:
  - fuzzy_correct (rapidfuzz post-correction)
  - score_sentiment (VADER with gamer-domain overrides + arousal)
  - detect_transcript_keywords (now reads game profile cues and emits
    a high-sentiment branch)
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_profile_cache():
    from ai_video_editor.profiles import _registry

    _registry.cache_clear()
    yield
    _registry.cache_clear()


# ----- fuzzy_correct -----


def test_fuzzy_correct_passes_clean_text_through():
    from ai_video_editor.candidates.fuzzy_correct import fuzzy_correct

    vocab = ["Yasuo", "Ahri", "Lee Sin"]
    text = "I just had a good game on Yasuo"
    assert fuzzy_correct(text, vocab) == text


def test_fuzzy_correct_snaps_close_typo_to_canonical():
    """'yasso' is edit-distance 1 from 'yasuo' (Whisper drops the 'u')
    — WRatio ≈ 80 at the default threshold."""
    from ai_video_editor.candidates.fuzzy_correct import fuzzy_correct

    out = fuzzy_correct("yasso just hit a triple", ["Yasuo"])
    assert "Yasuo" in out


def test_fuzzy_correct_canonicalizes_case():
    """Whisper outputs lowercase ('ahri'); we want 'Ahri' in output."""
    from ai_video_editor.candidates.fuzzy_correct import fuzzy_correct

    out = fuzzy_correct("ahri ulted them", ["Ahri"])
    assert out.startswith("Ahri ")


def test_fuzzy_correct_skips_short_tokens():
    """3-char tokens have too little signal — never snap."""
    from ai_video_editor.candidates.fuzzy_correct import fuzzy_correct

    # 'ya' phonetically resembles 'Yasuo' but is too short to risk.
    out = fuzzy_correct("ya the team grouped up", ["Yasuo"])
    assert "Yasuo" not in out
    assert "ya " in out  # stays as-is


def test_fuzzy_correct_empty_vocab_no_op():
    from ai_video_editor.candidates.fuzzy_correct import fuzzy_correct

    assert fuzzy_correct("anything goes", []) == "anything goes"


def test_fuzzy_correct_below_threshold_stays():
    """A word too distant from any vocab entry must not be corrupted."""
    from ai_video_editor.candidates.fuzzy_correct import fuzzy_correct

    # 'hello' shouldn't get yanked toward 'Yasuo' / 'Ahri' / 'Lee Sin'.
    out = fuzzy_correct("hello everyone good morning", ["Yasuo", "Ahri", "Lee Sin"])
    assert "Yasuo" not in out
    assert "Ahri" not in out
    assert "hello" in out


def test_fuzzy_correct_aliases_bypass_threshold():
    """The whole reason aliases exist: 'noodles' vs 'noodlz' scores 76.9
    on WRatio — just under the 80 threshold. The general fuzz path can't
    catch it. An explicit alias mapping snaps regardless of score."""
    from ai_video_editor.candidates.fuzzy_correct import fuzzy_correct

    aliases = {"Noodlz": ["noodles", "noodle"]}
    out = fuzzy_correct(
        "Don't need a lot of gold to spend on noodles for my next item",
        vocabulary=["Noodlz"],
        aliases=aliases,
    )
    assert "Noodlz" in out
    assert "noodles" not in out


def test_fuzzy_correct_aliases_match_short_tokens():
    """Aliases bypass the 4-char min — useful for short streamer names
    that the general fuzz path would skip on signal/noise grounds."""
    from ai_video_editor.candidates.fuzzy_correct import fuzzy_correct

    aliases = {"Lyn": ["lin", "len"]}
    out = fuzzy_correct("lin played well", vocabulary=[], aliases=aliases)
    assert out.startswith("Lyn")


def test_fuzzy_correct_aliases_are_case_insensitive():
    from ai_video_editor.candidates.fuzzy_correct import fuzzy_correct

    aliases = {"Noodlz": ["noodles"]}
    out = fuzzy_correct("NOODLES big play", vocabulary=[], aliases=aliases)
    # Token matched case-insensitively, replaced with canonical case.
    assert "Noodlz" in out


# ----- score_sentiment -----


def test_score_sentiment_empty_text_is_zero():
    from ai_video_editor.candidates.sentiment import score_sentiment

    assert score_sentiment("") == 0.0
    assert score_sentiment("   ") == 0.0


def test_score_sentiment_neutral_speech_is_low():
    from ai_video_editor.candidates.sentiment import score_sentiment

    s = score_sentiment("Let me go back to base and recall")
    assert s < 0.3


def test_score_sentiment_yelling_is_high():
    from ai_video_editor.candidates.sentiment import score_sentiment

    # Both positive AND negative high-intensity should score high
    # (arousal-based, not valence-based).
    assert score_sentiment("OH MY GOD WHAT A PLAY") > 0.3
    assert score_sentiment("ARE YOU KIDDING ME") > 0.3


def test_score_sentiment_gamer_insane_is_positive():
    """The whole reason we override VADER's lexicon: 'insane' in gaming
    is positive, not psychiatric. Stock VADER would score it negative."""
    from ai_video_editor.candidates.sentiment import score_sentiment

    s = score_sentiment("I just hit something insane")
    assert s > 0.0  # arousal regardless, but specifically NOT 0


def test_score_sentiment_returns_in_unit_range():
    from ai_video_editor.candidates.sentiment import score_sentiment

    for t in ["normal text", "pentakill clutch", "ARE YOU SERIOUS RIGHT NOW"]:
        s = score_sentiment(t)
        assert 0.0 <= s <= 1.0


# ----- detect_transcript_keywords G1b branches -----


def _seg(start: float, end: float, text: str, sentiment: float | None = None) -> dict:
    return {
        "start_seconds": start,
        "end_seconds": end,
        "text": text,
        "sentiment_score": sentiment,
    }


def test_detect_transcript_keywords_cue_branch_existing_behavior():
    from ai_video_editor.candidates.transcript import detect_transcript_keywords

    segments = [_seg(10, 15, "oh my god that was clutch")]
    out = detect_transcript_keywords(segments)
    assert len(out) == 1
    assert out[0]["event_type"] == "hype_callout"
    assert "oh my god" in out[0]["metadata"]["cues"]


def test_detect_transcript_keywords_high_sentiment_branch_no_cue():
    """A segment with no cue but high sentiment_score still emits a
    hype candidate via the G1b branch."""
    from ai_video_editor.candidates.transcript import detect_transcript_keywords

    # Generic chatter that wouldn't match any cue, but pre-scored hot.
    segments = [_seg(20, 25, "and then he just like rotated", sentiment=0.85)]
    out = detect_transcript_keywords(segments)
    assert len(out) == 1
    assert out[0]["event_type"] == "hype_callout"
    assert out[0]["metadata"]["rationale"] == "high-arousal speech"
    assert out[0]["metadata"]["sentiment_score"] == 0.85


def test_detect_transcript_keywords_low_sentiment_no_cue_emits_nothing():
    from ai_video_editor.candidates.transcript import detect_transcript_keywords

    segments = [_seg(20, 25, "just walking around", sentiment=0.1)]
    out = detect_transcript_keywords(segments)
    assert out == []


def test_detect_transcript_keywords_cue_match_does_not_double_count_with_sentiment():
    """When a segment has BOTH a cue match AND high sentiment, emit
    exactly one candidate (cue branch wins; sentiment goes in metadata)."""
    from ai_video_editor.candidates.transcript import detect_transcript_keywords

    segments = [_seg(30, 35, "what the fuck oh my god", sentiment=0.9)]
    out = detect_transcript_keywords(segments)
    assert len(out) == 1
    assert out[0]["metadata"]["rationale"] == "transcript spoken cue"
    assert out[0]["metadata"]["sentiment_score"] == 0.9


def test_detect_transcript_keywords_reads_game_specific_cues_from_profile():
    """profile.transcription.hype_cues extends the generic list."""
    from ai_video_editor.candidates.transcript import detect_transcript_keywords

    # 'pentakill' is a LoL-profile cue, not in the generic _HYPE_CUES.
    segments = [_seg(40, 45, "yo I just got a pentakill")]
    # Without game → no match (pentakill not in generic list anymore)
    out_no_game = detect_transcript_keywords(segments)
    assert out_no_game == []
    # With game="league" → match via profile
    out_lol = detect_transcript_keywords(segments, game="League of Legends")
    assert len(out_lol) == 1
    assert "pentakill" in out_lol[0]["metadata"]["cues"]


def test_detect_transcript_keywords_unknown_game_falls_back_to_generic():
    """A profile that doesn't exist → default profile (empty cues) →
    only generic cross-game cues fire."""
    from ai_video_editor.candidates.transcript import detect_transcript_keywords

    segments = [_seg(10, 15, "oh my god what a play")]
    out = detect_transcript_keywords(segments, game="Bowling")
    assert len(out) == 1  # generic 'oh my god' still hits


def test_detect_transcript_keywords_sentiment_branch_confidence_scales_with_arousal():
    from ai_video_editor.candidates.transcript import detect_transcript_keywords

    just_above = detect_transcript_keywords([_seg(0, 1, "neutral phrasing", sentiment=0.51)])
    much_above = detect_transcript_keywords([_seg(0, 1, "neutral phrasing", sentiment=0.95)])
    assert just_above and much_above
    assert much_above[0]["confidence"] > just_above[0]["confidence"]


# ----- Profile.Transcription wiring -----


def test_profile_loads_transcription_section():
    from ai_video_editor.profiles import load_profile

    p = load_profile("league")
    assert p.transcription.initial_prompt  # non-empty
    assert "Yasuo" in p.transcription.vocabulary
    assert "pentakill" in p.transcription.hype_cues


def test_default_profile_has_empty_transcription_block():
    """The default fallback profile has no transcription hints — that's
    by design (we ship stock-Whisper behavior for unknown games)."""
    from ai_video_editor.profiles import load_profile

    p = load_profile(None)
    assert p.transcription.initial_prompt == ""
    assert p.transcription.vocabulary == []
    assert p.transcription.hype_cues == []
