"""Transcript-keyword candidate source.

Pure & deterministic (no LLM, like every other source): scan Whisper
transcript segments for hype/funny spoken cues and emit candidate
windows. The transcript itself is produced separately by
`transcribe.py` and persisted; this just consumes the segments, so it
stays a pure function (testable without audio).

G1b additions:
- Cues from the game profile (`profile.transcription.hype_cues`)
  EXTEND the generic cross-game list — `pentakill` only fires on LoL,
  `spike planted` only on Val.
- A high-sentiment branch emits a `hype_callout` for segments whose
  `sentiment_score` exceeds `_SENTIMENT_THRESHOLD` even if no cue
  matched. Yelling counts as hype regardless of word match.
"""

from ..config import settings

# Cross-game spoken reaction cues. Phrases match case-insensitively as
# substrings of a segment's text. Game-specific cues (`pentakill`,
# `spike planted`, etc.) live in each profile's [transcription].hype_cues.
_HYPE_CUES = (
    "let's go",
    "lets go",
    "no way",
    "oh my god",
    "what the",
    "are you kidding",
    "clutch",
    "insane",
    "got him",
    "got em",
    "outplayed",
    "1v3",
    "1v4",
    "1v5",
    "cracked",
    "let's gooo",
)
_FUNNY_CUES = (
    "lol",
    "lmao",
    "lmfao",
    "haha",
    "bruh",
    "no shot",
    "are you serious",
    "bro what",
    "wtf",
    "i'm dead",
    "im dead",
    "crying",
)

# Above this VADER-arousal score, a segment is emitted as a hype
# candidate even without a cue match (G1b high-sentiment branch).
# Tuned to fire on genuine yelling / loud reactions, not normal speech.
_SENTIMENT_THRESHOLD = 0.5


def _scan(
    text: str, hype_cues: tuple[str, ...], funny_cues: tuple[str, ...]
) -> tuple[str | None, list[str]]:
    """(category, matched cues) for a segment, or (None, []) if nothing."""
    low = text.lower()
    hype = [c for c in hype_cues if c in low]
    funny = [c for c in funny_cues if c in low]
    if hype:
        return "hype_callout", hype + funny
    if funny:
        return "funny_callout", funny
    return None, []


def detect_transcript_keywords(segments: list[dict], game: str | None = None) -> list[dict]:
    """Return candidate windows from transcript cue hits + high-sentiment
    yelling. Pure.

    Two emission branches:
      1. Cue match — segment text contains a known hype/funny phrase.
      2. High-sentiment — `sentiment_score` exceeds the threshold even
         without a cue match. Catches loud reactions that happen not to
         use one of the magic phrases.

    A segment with both a cue and high sentiment is emitted ONCE (cue
    branch wins; sentiment is recorded in metadata).
    """
    pad = settings.analyze_window_padding
    base = {"hype_callout": 0.7, "funny_callout": 0.6}
    out: list[dict] = []

    hype_cues: tuple[str, ...] = _HYPE_CUES
    funny_cues: tuple[str, ...] = _FUNNY_CUES
    if game:
        from ..profiles import load_profile  # local import

        profile_cues = tuple(load_profile(game).transcription.hype_cues)
        if profile_cues:
            hype_cues = _HYPE_CUES + profile_cues

    for seg in segments or []:
        text = seg.get("text", "")
        sentiment = float(seg.get("sentiment_score") or 0.0)
        category, cues = _scan(text, hype_cues, funny_cues)

        if category is not None:
            # Cue-match branch (existing behavior).
            conf = min(0.95, base[category] + 0.1 * (len(set(cues)) - 1))
            rationale = "transcript spoken cue"
            metadata = {
                "text": text,
                "cues": sorted(set(cues)),
                "sentiment_score": round(sentiment, 3) if sentiment else None,
                "rationale": rationale,
            }
        elif sentiment >= _SENTIMENT_THRESHOLD:
            # High-sentiment branch — no cue, but the player is loud.
            category = "hype_callout"
            # Map sentiment [threshold, 1.0] → confidence [0.55, 0.85].
            scale = (sentiment - _SENTIMENT_THRESHOLD) / max(1e-6, 1.0 - _SENTIMENT_THRESHOLD)
            conf = round(0.55 + 0.30 * min(1.0, scale), 3)
            metadata = {
                "text": text,
                "cues": [],
                "sentiment_score": round(sentiment, 3),
                "rationale": "high-arousal speech",
            }
        else:
            continue

        start = max(0.0, float(seg["start_seconds"]) - pad)
        end = float(seg["end_seconds"]) + pad
        out.append(
            {
                "start_seconds": round(start, 2),
                "end_seconds": round(end, 2),
                "event_type": category,
                "confidence": round(conf, 3),
                "metadata": metadata,
            }
        )

    out.sort(key=lambda c: c["confidence"], reverse=True)
    return out[: settings.analyze_max_candidates]
