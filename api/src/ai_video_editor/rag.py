"""RAG: semantic search over transcripts.

Local & private by design — same principle as Whisper:
- **Embeddings:** `fastembed` (ONNX, `BAAI/bge-small-en-v1.5`, 384-dim).
  No upload, ~90MB model, fast on CPU.
- **Vector store:** `sqlite-vec` vec0 virtual table in the existing
  `ai_video_editor.db`. No new service.

Whisper segments alone are too short to embed well (a few seconds of
text), so `chunk_segments` rolls them into ~25-second windows for
indexing. Search returns the matched chunks (asset, time range, text,
score) — feed those into a player to actually watch the moments.

Pure parts (`chunk_segments`) are unit-testable; the model + DB layer
sit behind small functions.
"""

import logging
import uuid

import numpy as np
import sqlite_vec

from .config import settings

_log = logging.getLogger(__name__)
_model = None  # lazy fastembed singleton


def _get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding

        _model = TextEmbedding(model_name=settings.embed_model)
    return _model


def embed_texts(texts: list[str]) -> list[np.ndarray]:
    """Embed a batch of strings to float32 vectors. $0/local."""
    if not texts:
        return []
    return [np.asarray(v, dtype=np.float32) for v in _get_model().embed(texts)]


# --- pure: chunk transcript segments into rolling windows ---
def chunk_segments(segments: list[dict], window_s: float, stride_s: float) -> list[dict]:
    """Roll Whisper segments into ~window-second chunks for embedding.

    Each chunk concatenates segments whose start falls in the window.
    Empty windows are skipped. Returns [{start_seconds, end_seconds, text}].
    """
    if not segments or window_s <= 0 or stride_s <= 0:
        return []
    end = max(float(s["end_seconds"]) for s in segments)
    chunks: list[dict] = []
    t = 0.0
    while t < end:
        win_end = t + window_s
        bucket = [s for s in segments if t <= float(s["start_seconds"]) < win_end]
        if bucket:
            text = " ".join(s["text"].strip() for s in bucket if s.get("text"))
            if text:
                chunks.append(
                    {
                        "start_seconds": round(float(bucket[0]["start_seconds"]), 2),
                        "end_seconds": round(float(bucket[-1]["end_seconds"]), 2),
                        "text": text,
                    }
                )
        t += stride_s
    return chunks


# --- I/O: index + search via sqlite-vec on the existing DB ---
async def index_asset_transcript(db, asset_id: str) -> dict:
    """Load this asset's transcript, chunk, embed, replace its embeddings
    rows. Returns a small summary dict. Non-fatal on model/embed errors —
    just returns 0 indexed and logs.
    """
    rows = await db.execute_fetchall(
        "SELECT start_seconds, end_seconds, text FROM transcripts "
        "WHERE video_id = ? ORDER BY start_seconds",
        (asset_id,),
    )
    segments = [dict(r) for r in rows]
    chunks = chunk_segments(segments, settings.chunk_window_seconds, settings.chunk_stride_seconds)
    # Idempotent re-index.
    await db.execute("DELETE FROM embeddings WHERE video_id = ?", (asset_id,))
    if not chunks:
        await db.commit()
        return {"segments": len(segments), "chunks": 0, "indexed": 0}

    try:
        vectors = embed_texts([c["text"] for c in chunks])
    except Exception as e:
        _log.warning("embed failed (non-fatal): %s", e)
        await db.commit()
        return {"segments": len(segments), "chunks": len(chunks), "indexed": 0}

    for c, v in zip(chunks, vectors, strict=True):
        await db.execute(
            "INSERT INTO embeddings (chunk_id, video_id, start_seconds, "
            "end_seconds, text, vector) VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                asset_id,
                c["start_seconds"],
                c["end_seconds"],
                c["text"],
                sqlite_vec.serialize_float32(v.tolist()),
            ),
        )
    await db.commit()
    return {"segments": len(segments), "chunks": len(chunks), "indexed": len(chunks)}


async def search(db, query: str, limit: int = 10, asset_id: str | None = None) -> list[dict]:
    """Semantic search over indexed transcript chunks.

    Returns matches with asset_id, start/end, text, and distance (lower =
    closer). When asset_id is given, results are filtered to that
    recording (post-KNN; fine at typical corpus sizes).
    """
    if not query.strip():
        return []
    try:
        [qv] = embed_texts([query])
    except Exception as e:
        _log.warning("query embed failed: %s", e)
        return []

    # Pull more than asked when filtering by asset — filter then trim.
    k = max(limit * 4, limit) if asset_id else limit
    rows = await db.execute_fetchall(
        "SELECT chunk_id, video_id, start_seconds, end_seconds, text, "
        "distance FROM embeddings WHERE vector MATCH ? AND k = ? "
        "ORDER BY distance",
        (sqlite_vec.serialize_float32(qv.tolist()), k),
    )
    out = [dict(r) for r in rows]
    if asset_id:
        out = [r for r in out if r["video_id"] == asset_id]
    return out[:limit]
