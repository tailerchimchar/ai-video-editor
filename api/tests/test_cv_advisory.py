from ai_video_editor.candidates.service import apply_cv_advisory


def _cv(start=220.0, end=240.0, conf=0.85):
    return {
        "source": "cv_kda",
        "event_type": "kill",
        "start_seconds": start,
        "end_seconds": end,
        "confidence": conf,
        "metadata": {},
    }


def _riot(anchor, event="kill", match_confidence="high"):
    return {
        "source": "riot_api",
        "event_type": event,
        "start_seconds": anchor - 5,
        "end_seconds": anchor + 5,
        "confidence": 0.95,
        "metadata": {"anchor_seconds": anchor, "match_confidence": match_confidence},
    }


def test_cv_kda_demoted_when_riot_disagrees():
    cv = _cv()
    rows = apply_cv_advisory([cv, _riot(500.0)])
    assert rows[0]["confidence"] == 0.425
    assert rows[0]["metadata"]["riot_disagrees"] is True


def test_cv_kda_unchanged_when_riot_has_event_in_window():
    for event in ("kill", "assist", "death"):
        cv = _cv()
        apply_cv_advisory([cv, _riot(230.0, event)])
        assert cv["confidence"] == 0.85
        assert "riot_disagrees" not in cv["metadata"]


def test_non_combat_riot_event_in_window_does_not_agree():
    cv = _cv()
    apply_cv_advisory([cv, _riot(230.0, "baron")])
    assert cv["metadata"]["riot_disagrees"] is True


def test_cv_kda_unchanged_when_riot_confidence_not_high():
    cv = _cv()
    apply_cv_advisory([cv, _riot(500.0, match_confidence="medium")])
    assert cv["confidence"] == 0.85
    assert "riot_disagrees" not in cv["metadata"]


def test_cv_kda_unchanged_without_riot_rows():
    cv = _cv()
    apply_cv_advisory([cv])
    assert cv["confidence"] == 0.85
