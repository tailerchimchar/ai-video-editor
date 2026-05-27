"""chunk_segments is pure and the only piece of RAG that can subtly
break without us noticing — embeddings + sqlite-vec are exercised by
integration tests. Cover empty/edge inputs + windowing math."""

from ai_video_editor.rag import chunk_segments


def test_empty_input_returns_empty_list():
    assert chunk_segments([], 25.0, 25.0) == []
    assert chunk_segments([{"start_seconds": 0, "end_seconds": 1, "text": "x"}], 0, 25) == []


def test_groups_segments_into_windows():
    segs = [
        {"start_seconds": 0, "end_seconds": 2, "text": "alpha"},
        {"start_seconds": 5, "end_seconds": 7, "text": "beta gamma"},
        {"start_seconds": 30, "end_seconds": 33, "text": "delta"},
    ]
    out = chunk_segments(segs, window_s=25.0, stride_s=25.0)
    assert len(out) == 2
    assert out[0]["text"] == "alpha beta gamma"
    assert out[0]["start_seconds"] == 0.0
    assert out[0]["end_seconds"] == 7.0
    assert out[1]["text"] == "delta"
    assert out[1]["start_seconds"] == 30.0


def test_overlap_when_stride_lt_window():
    segs = [{"start_seconds": i, "end_seconds": i + 1, "text": str(i)} for i in range(10)]
    # window 10, stride 5 → at least two overlapping chunks
    out = chunk_segments(segs, 10.0, 5.0)
    assert len(out) >= 2
