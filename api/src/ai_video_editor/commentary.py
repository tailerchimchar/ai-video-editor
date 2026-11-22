"""Commentary bucket: the owner's voice-only moments, kept as story material.

A commentary moment is lower confidence than a clip: the owner describing a
plan or reacting oddly, with no kill or fight on screen. It feeds the
per-game story video and is never a short on its own.

Pure: takes owner-mic transcript rows (`speaker='owner'`) and the windows
already kept as clips, returns ranked utterances.
"""

from __future__ import annotations

MERGE_GAP_S = 1.5


def _merge(rows: list[dict]) -> list[dict]:
    """Owner rows into utterances: rows closer than MERGE_GAP_S join."""
    out: list[dict] = []
    for r in sorted(rows, key=lambda r: r["start_seconds"]):
        text = (r.get("text") or "").strip()
        score = r.get("sentiment_score")
        if out and r["start_seconds"] - out[-1]["end_s"] <= MERGE_GAP_S:
            u = out[-1]
            u["end_s"] = max(u["end_s"], r["end_seconds"])
            u["text"] = f"{u['text']} {text}".strip()
            if score is not None:
                u["sentiment_score"] = max(u["sentiment_score"], score)
        else:
            out.append(
                {
                    "start_s": r["start_seconds"],
                    "end_s": r["end_seconds"],
                    "text": text,
                    "sentiment_score": score if score is not None else 0.0,
                }
            )
    return out


def commentary_candidates(
    owner_rows: list[dict],
    kept_windows: list[tuple[float, float]],
    *,
    limit: int = 5,
    min_speech_s: float = 4.0,
) -> list[dict]:
    """Top `limit` owner utterances outside every kept window, by arousal
    (`sentiment_score`, max over the merged rows) then length."""
    kept = [
        u
        for u in _merge(owner_rows)
        if u["end_s"] - u["start_s"] >= min_speech_s
        and not any(a <= u["start_s"] and u["end_s"] <= b for a, b in kept_windows)
    ]
    kept.sort(key=lambda u: (-u["sentiment_score"], -(u["end_s"] - u["start_s"])))
    return kept[:limit]
