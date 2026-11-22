from ai_video_editor.commentary import commentary_candidates


def _row(start, end, text="a thought", score=0.5):
    return {"start_seconds": start, "end_seconds": end, "text": text, "sentiment_score": score}


def test_merges_owner_rows_into_utterances():
    rows = [_row(10, 12, "so I think"), _row(13, 15, "we go in"), _row(30, 36, "later")]
    out = commentary_candidates(rows, [])
    assert len(out) == 2
    first = next(u for u in out if u["start_s"] == 10)
    assert first["end_s"] == 15
    assert first["text"] == "so I think we go in"


def test_drops_utterances_inside_kept_windows():
    rows = [_row(10, 16), _row(50, 56)]
    out = commentary_candidates(rows, [(5.0, 20.0)])
    assert [u["start_s"] for u in out] == [50]


def test_top_5_by_arousal():
    rows = [_row(i * 100, i * 100 + 5, score=i / 10) for i in range(8)]
    out = commentary_candidates(rows, [])
    assert len(out) == 5
    assert [u["sentiment_score"] for u in out] == [0.7, 0.6, 0.5, 0.4, 0.3]


def test_min_speech_4s():
    rows = [_row(0, 3.9, score=0.9), _row(100, 104, score=0.1)]
    out = commentary_candidates(rows, [])
    assert [u["start_s"] for u in out] == [100]
