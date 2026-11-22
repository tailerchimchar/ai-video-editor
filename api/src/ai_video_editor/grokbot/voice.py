"""Voice helpers for the digest: transcript slice, Haiku paraphrase, energy label.

The transcript slice is read on the PC and only ever leaves it as one
Haiku-written sentence (spec D4); the digest leak check still guards the
result (R8).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

from ..anthropic_surface import check_budget, make_client, record_spend, reserve_call
from ..config import settings

logger = logging.getLogger(__name__)

PARAPHRASE_SYSTEM = (
    "One sentence describing what the player says or reacts to. No quotes, no names, no slurs."
)
_MAX_TOKENS = 60
_QUOTE_CHARS = "\"'\u201c\u201d\u2018\u2019"
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
_LAUGH = re.compile(r"\b(?:a?ha(?:ha)+h?|lol+|lmao+)\b|\[laugh", re.IGNORECASE)


def _whole_sentences(text: str) -> str:
    """`text` cut after its last full sentence; "" when there is none."""
    ends = [m.end() for m in _SENTENCE_END.finditer(text)]
    return text[: ends[-1]].strip() if ends else ""


def voice_paraphrase(text: str, *, asset_id: str | None = None, client=None) -> str:
    """One Haiku sentence about `text`; "" on empty input or any failure."""
    if not text or not text.strip():
        return ""
    try:
        check_budget(asset_id)
        reserve_call("paraphrase", settings.paraphrase_max_calls, "PARAPHRASE_MAX_CALLS")
        client = client or make_client()
        response = client.messages.create(
            model=settings.paraphrase_model,
            max_tokens=_MAX_TOKENS,
            system=PARAPHRASE_SYSTEM,
            messages=[{"role": "user", "content": text}],
        )
    except Exception as e:  # optional enrichment: the digest ships without it
        logger.warning("voice paraphrase skipped: %s", e)
        return ""
    try:
        record_spend(asset_id, "paraphrase", settings.paraphrase_model, response.usage)
    except Exception as e:  # a paid answer must not be lost to accounting
        logger.warning("spend ledger write failed: %s", e)
    try:
        reply = next(b.text for b in response.content if getattr(b, "type", None) == "text")
    except Exception:
        return ""
    reply = reply.strip().strip(_QUOTE_CHARS).strip()
    if getattr(response, "stop_reason", None) == "max_tokens":
        reply = _whole_sentences(reply)  # never ship half a sentence
    return reply


def _rows(db_path: Path, sql: str, params: tuple) -> list[tuple]:
    if not Path(db_path).is_file():
        return []
    try:
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning("voice: transcript read failed: %s", e)
        return []


_OVERLAP = "WHERE video_id = ? AND start_seconds < ? AND end_seconds > ?"


def transcript_slice(db_path: Path, video_id: str, start_s: float, end_s: float) -> str:
    """Transcript text overlapping [start_s, end_s], in time order; "" if none."""
    rows = _rows(
        db_path,
        f"SELECT text FROM transcripts {_OVERLAP} ORDER BY start_seconds",
        (video_id, end_s, start_s),
    )
    return " ".join(t.strip() for (t,) in rows if t and t.strip())


def window_sentiment(db_path: Path, video_id: str, start_s: float, end_s: float) -> float | None:
    """Mean `sentiment_score` of transcript rows overlapping the window; None if none."""
    rows = _rows(
        db_path,
        f"SELECT AVG(sentiment_score) FROM transcripts {_OVERLAP}",
        (video_id, end_s, start_s),
    )
    return rows[0][0] if rows else None


def energy_label(loudness_db: float | None, arousal: float | None, text: str = "") -> str:
    """laughter / hype_rising / calm / unknown for one clip window.

    `arousal` is the window's mean VADER arousal in [0, 1] (the
    `transcripts.sentiment_score` column); `loudness_db` is optional and
    only tightens hype or adds calm when given; `text` is the window's
    transcript slice, scanned for laughter tokens. Laughter is checked
    first because it is the most specific evidence. `panic` stays in the
    digest's vocabulary but has no rule yet (it needs signed valence).
    """
    if text and _LAUGH.search(text):
        return "laughter"
    if arousal is not None and arousal >= 0.6 and (loudness_db is None or loudness_db >= -20):
        return "hype_rising"
    if (arousal is not None and arousal <= 0.2) or (loudness_db is not None and loudness_db < -30):
        return "calm"
    return "unknown"
