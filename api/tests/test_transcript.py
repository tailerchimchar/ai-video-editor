"""The transcript_keyword source is a pure cue scanner over segments."""

from ai_video_editor.candidates.transcript import detect_transcript_keywords


def test_no_cues_returns_no_candidates():
    segs = [{"start_seconds": 1, "end_seconds": 2, "text": "just walking"}]
    assert detect_transcript_keywords(segs) == []


def test_hype_cue_emits_hype_callout():
    segs = [{"start_seconds": 10, "end_seconds": 12, "text": "LET'S GO that was insane"}]
    out = detect_transcript_keywords(segs)
    assert len(out) == 1
    c = out[0]
    assert c["event_type"] == "hype_callout"
    # Two distinct cues -> bumped confidence
    assert c["confidence"] > 0.7
    assert "insane" in c["metadata"]["cues"] and "let's go" in c["metadata"]["cues"]


def test_funny_cue_emits_funny_callout():
    segs = [{"start_seconds": 60, "end_seconds": 63, "text": "lmao bro what"}]
    out = detect_transcript_keywords(segs)
    assert out[0]["event_type"] == "funny_callout"


def test_results_sorted_by_confidence_desc():
    segs = [
        {"start_seconds": 5, "end_seconds": 6, "text": "haha"},  # 1 funny cue
        {"start_seconds": 10, "end_seconds": 11, "text": "let's go insane ace"},  # 3 hype cues
    ]
    out = detect_transcript_keywords(segs)
    assert out[0]["event_type"] == "hype_callout"
    assert out[0]["confidence"] >= out[1]["confidence"]
