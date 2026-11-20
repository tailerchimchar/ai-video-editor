"""Names-free digest of a panel round, safe to hand to a cloud editor bot.

The digest is built only from panel outputs (clips.json and the expert
JSONs). `transcript.json` is never opened by `build_digest`; it is read
only by `digest_leaks`, the guard that proves no transcript text slipped
through.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..bucketing import _FORMAT_QUESTION, _INCLUDE_QUESTION
from ..decisions import Answer, ask_many

_REF_RE = re.compile(r"(\d+m\d+s)")


class DigestClip(BaseModel):
    ref: str
    event: str
    start_s: float
    end_s: float
    panel: dict[str, Any] = Field(default_factory=dict)
    critic: str | None = None
    voice: str = ""
    # `copy` shadows BaseModel.copy deliberately: it is the bots' JSON contract.
    copy: dict | None = None
    drive: str | None = None


class Digest(BaseModel):
    game: str
    round: str
    clips: list[DigestClip]
    built_at: str


def _load(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _ref_from_file(file: str) -> str:
    match = _REF_RE.search(Path(file).stem)
    return match.group(1) if match else Path(file).stem


def build_digest(round_dir: Path, *, game: str) -> Digest:
    clips = _load(round_dir / "clips.json") or []
    gaming = {e["clip_ref"]: e for e in (_load(round_dir / "gaming-expert.json") or [])}
    viewer_raw = _load(round_dir / "viewer-expert.json") or {}
    viewer = {e["clip_ref"]: e for e in viewer_raw.get("clips", [])}

    out: list[DigestClip] = []
    for clip in clips:
        ref = _ref_from_file(clip["file"])
        g = gaming.get(ref) or {}
        v = viewer.get(ref) or {}
        beats = g.get("story_beats") or []
        out.append(
            DigestClip(
                ref=ref,
                event=clip.get("event", ""),
                start_s=clip["start_seconds"],
                end_s=clip["end_seconds"],
                panel={
                    "gaming_quality": g.get("quality"),
                    "hook_verdict": v.get("hook_verdict"),
                    "format": None,
                },
                critic=clip.get("vlm_verdict"),
                voice=beats[0] if beats else "",
                copy=v.get("copy"),
            )
        )
    return Digest(
        game=game,
        round=round_dir.name,
        clips=out,
        built_at=datetime.now(timezone.utc).isoformat(),
    )


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [_norm(value)]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _strings(v)]
    if isinstance(value, list | tuple):
        return [t for v in value for t in _strings(v)]
    return []


def digest_leaks(digest: Digest, transcript_path: Path, *, min_len: int = 12) -> list[str]:
    """Transcript fragments (>= min_len chars) found in any digest string.

    Compares decoded, normalised (casefolded, whitespace-collapsed) text, so
    quotes, backslashes and newlines cannot hide a match. Every
    `min_len`-char window of each transcript line is checked; overlapping
    matching windows are merged into one reported fragment.
    """
    haystacks = _strings(digest.model_dump())
    leaks: list[str] = []
    for line in json.loads(transcript_path.read_text(encoding="utf-8")):
        text = _norm(line.get("text", ""))
        span: list[int] | None = None
        for i in range(len(text) - min_len + 1):
            hit = any(text[i : i + min_len] in h for h in haystacks)
            if hit and span and i <= span[1] - min_len + 1:
                span[1] = i + min_len
            elif hit:
                if span:
                    leaks.append(text[span[0] : span[1]])
                span = [i, i + min_len]
        if span:
            leaks.append(text[span[0] : span[1]])
    return leaks


def annotate_with_jev(digest: Digest, ask_many: Callable[..., list[Answer]] = ask_many) -> Digest:
    """Add `format` and `include_p` to each clip's panel via Jev."""
    states = [{"clip": c.model_dump()} for c in digest.clips]
    formats = ask_many([(_FORMAT_QUESTION, s) for s in states])
    includes = ask_many([(_INCLUDE_QUESTION, s) for s in states])
    for clip, fmt, inc in zip(digest.clips, formats, includes, strict=True):
        clip.panel["format"] = str(fmt.value) if fmt.value is not None else None
        clip.panel["include_p"] = inc.probability
    return digest
