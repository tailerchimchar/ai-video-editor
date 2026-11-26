"""On-disk cache for the early-game vision check (AVE-73).

Pure file/hash logic, no vision or story imports beyond the segment model: a
re-render with the same inputs reuses the confirmed segments, so narration
takes stay aligned with the chapters. Any unreadable cache is a miss.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from ..league.early_game import EarlySegment

_log = logging.getLogger(__name__)

CACHE_FILENAME = "early_confirm.json"
_SEGMENTS = TypeAdapter(list[EarlySegment])


def early_cache_key(
    segments: list[EarlySegment],
    first_back_s: float | None,
    backend_id: str,
    offset_s: float,
) -> str:
    """Hash of everything the vision check's output depends on."""
    payload = {
        "segments": _SEGMENTS.dump_python(segments, mode="json"),
        "first_back_s": first_back_s,
        "backend": backend_id,
        "offset_s": offset_s,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def read_early_cache(path: Path, key: str) -> list[EarlySegment] | None:
    """Cached segments when `path` holds this `key`; None on any miss."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("key") != key:
            return None
        return _SEGMENTS.validate_python(data["segments"])
    except (OSError, ValueError, KeyError, ValidationError):
        return None


def write_early_cache(path: Path, key: str, segments: list[EarlySegment]) -> None:
    """Best-effort write (a cache failure never fails a render)."""
    body = {"key": key, "segments": _SEGMENTS.dump_python(segments, mode="json")}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    except OSError as e:
        _log.warning("could not write early-game cache %s: %s", path, e)
