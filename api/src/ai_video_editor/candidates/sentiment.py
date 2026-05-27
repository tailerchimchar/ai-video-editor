"""Per-segment sentiment scoring (G1b).

VADER under the hood with two adjustments specific to gameplay speech:

1. **Gamer-vocab lexicon overrides.** VADER's stock lexicon treats
   "insane"/"sick"/"disgusting" as negative (their psychiatric senses).
   In gaming context they are uniformly positive — overriding the
   weights here gets us correct sign on the most common false negatives.

2. **Arousal-based scoring, not valence.** A clip where the player is
   yelling has high `pos + neg` intensity regardless of whether the
   words happen to be positive or negative. We score on that combined
   "intensity," which is what we actually want for "is this a hype
   moment".

Interface (`score_sentiment(text) -> float in [0,1]`) is the seam:
swap VADER for DistilBERT later without touching call sites.
"""

from __future__ import annotations

from functools import lru_cache

# Gamer-domain lexicon overrides. Values in roughly VADER's [-4, +4]
# scale (each unit moves the compound score noticeably). Positive
# weights flip negative-by-default words; negative ones tag genuine
# negatives ("throw", "feed") that VADER misses.
_GAMER_LEXICON = {
    # positive in gaming, negative in stock VADER
    "insane": 3.0,
    "sick": 2.5,
    "disgusting": 2.0,
    "filthy": 2.0,
    "nasty": 2.0,
    "dirty": 1.5,
    "crazy": 2.0,
    "wild": 2.0,
    "demonic": 2.0,
    "cracked": 2.5,
    "cooked": -1.5,  # "we're cooked" = lost — negative in context
    # gamer-specific hype not in stock lexicon
    "clutch": 3.0,
    "pog": 3.0,
    "pogchamp": 3.0,
    "poggers": 2.5,
    "based": 2.0,
    "outplayed": 2.5,
    "pentakill": 3.5,
    "quadrakill": 3.0,
    "shutdown": 2.5,
    "lmao": 2.0,
    "lmfao": 2.5,
    "kek": 2.0,
    "kekw": 2.5,
    "lol": 1.0,
    # genuine negatives stock lexicon under-weights
    "feeder": -2.5,
    "feeders": -2.5,
    "feeding": -2.0,
    "throw": -2.0,
    "throwing": -2.5,
    "ff": -2.0,  # surrender vote
    "trolling": -2.5,
    "inting": -2.5,
    "tilted": -1.5,
}


@lru_cache(maxsize=1)
def _analyzer():
    """Lazy single-process VADER instance with our overrides applied."""
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

    a = SentimentIntensityAnalyzer()
    a.lexicon.update(_GAMER_LEXICON)
    return a


def score_sentiment(text: str) -> float:
    """Return [0, 1] arousal-based excitement score for one segment.

    Empty / whitespace-only text → 0.0. The score is `pos + neg` from
    VADER (clipped at 1.0), so loud reactions land high regardless of
    whether the underlying words are positive or negative — both
    "OH MY GOD" and "ARE YOU KIDDING ME" are hype.
    """
    text = (text or "").strip()
    if not text:
        return 0.0
    scores = _analyzer().polarity_scores(text)
    arousal = float(scores.get("pos", 0.0)) + float(scores.get("neg", 0.0))
    return min(1.0, max(0.0, arousal))


def score_segments(segments: list[dict]) -> list[dict]:
    """Stamp `sentiment_score` onto every segment dict (in place,
    returns same list). Convenience for batch processing."""
    for s in segments:
        s["sentiment_score"] = score_sentiment(s.get("text", ""))
    return segments
