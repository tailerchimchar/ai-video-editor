"""Names-free digest of a panel round, safe to hand to a cloud editor bot.

The digest is built only from panel outputs (clips.json and the expert
JSONs). `transcript.json` is never opened by `build_digest`; it is read
only by `digest_leaks`, the guard that proves no transcript text slipped
through.

Optional inputs (all injected, all default off): a Riot timeline adds
per-clip events, the owner's game state and a match confidence; a
transcript slicer plus a paraphraser fill `voice` with one model-written
sentence; an energy callable labels the clip's mood.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args

from pydantic import BaseModel, Field

from ..bucketing import _FORMAT_QUESTION, _INCLUDE_QUESTION
from ..commentary import commentary_candidates
from ..decisions import Answer, ask_many
from ..league.lane_models import LaneSummary
from ..refs import normalize_clip_ref

if TYPE_CHECKING:
    from ..league.riot_timeline import OwnerFrame, RiotTimeline

_REF_RE = re.compile(r"(\d+m\d+s)")
logger = logging.getLogger(__name__)

Energy = Literal["hype_rising", "laughter", "panic", "calm", "unknown"]
MatchConfidence = Literal["high", "medium", "low", "none"]
_SYNC_ONLY = "sync_only"  # = riot_timeline.SYNC_ONLY; not imported so league stays optional
_VOICE_LEAK_MIN_LEN = 8  # R8: a paraphrase may not repeat 8+ chars of its own slice


class DigestEvent(BaseModel):
    type: str
    t_in_clip: float
    actors: list[str]


class GameState(BaseModel):
    game_time_s: float
    gold_start: int | None
    gold_end: int | None
    cs_start: int | None
    cs_end: int | None
    level: int | None
    game_ended_in_clip: bool


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
    events: list[DigestEvent] = Field(default_factory=list)
    game_state: GameState | None = None
    energy: Energy = "unknown"
    match_confidence: MatchConfidence = "none"


class CommentaryItem(BaseModel):
    """An owner voice-only moment: story material, never a short on its own."""

    ref: str
    start_s: float
    end_s: float
    voice: str = ""
    energy: Energy = "unknown"
    attached_beat: str | None = None


class Digest(BaseModel):
    game: str
    round: str
    clips: list[DigestClip]
    commentary: list[CommentaryItem] = Field(default_factory=list)
    built_at: str
    compilation_id: str | None = None
    compilation_version: str | None = None
    asset_id: str | None = None
    lane: LaneSummary | None = None


def _load(path: Path) -> Any:
    """Parsed JSON, or None when missing or malformed (one bad expert file
    must not stop the digest)."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("digest: ignoring unreadable %s: %s", path.name, e)
        return None


def _by_ref(entries: Any) -> dict[str, dict]:
    if not isinstance(entries, list):
        return {}
    return {
        normalize_clip_ref(e["clip_ref"]): e
        for e in entries
        if isinstance(e, dict) and isinstance(e.get("clip_ref"), str)
    }


def _ref_from_file(file: str) -> str:
    match = _REF_RE.search(Path(file).stem)
    if match:
        return match.group(1)
    logger.warning("digest: %s has no NmNNs time; using the file stem as its ref", file)
    return Path(file).stem


def _match_confidence(timeline: RiotTimeline | None) -> str:
    if timeline is None:
        return "none"
    if timeline.offset_source == _SYNC_ONLY and timeline.confidence == "high":
        return "medium"  # uncalibrated time basis: never claim high
    return timeline.confidence


def _events(timeline: RiotTimeline, start_s: float, end_s: float) -> list[DigestEvent]:
    return [
        DigestEvent(type=e.kind, t_in_clip=round(e.t_rec_s - start_s, 3), actors=list(e.actors))
        for e in timeline.events
        if e.t_rec_s is not None and start_s <= e.t_rec_s <= end_s
    ]


def _nearest_frame(frames: list[OwnerFrame], game_s: float) -> OwnerFrame | None:
    return min(frames, key=lambda f: abs(f.minute * 60 - game_s), default=None)


def _game_state(timeline: RiotTimeline, start_s: float, end_s: float) -> GameState | None:
    if timeline.confidence == "low":
        return None  # R4: a possibly wrong match must not state the owner's numbers
    first = _nearest_frame(timeline.owner_frames, start_s - timeline.offset_s)
    last = _nearest_frame(timeline.owner_frames, end_s - timeline.offset_s)
    end = timeline.game_end_s
    return GameState(
        game_time_s=round(start_s - timeline.offset_s, 3),
        gold_start=first.gold if first else None,
        gold_end=last.gold if last else None,
        cs_start=first.cs if first else None,
        cs_end=last.cs if last else None,
        level=last.level if last else None,
        game_ended_in_clip=end is not None and start_s <= end <= end_s,
    )


def _beat_text(beats: Any) -> str:
    """First gaming-expert story beat as text: `beat` of a dict, or the string."""
    first = beats[0] if isinstance(beats, list) and beats else None
    if isinstance(first, dict):
        beat = first.get("beat")
        return beat if isinstance(beat, str) else ""
    return first if isinstance(first, str) else ""


def _voice(
    beats: Any,
    start_s: float,
    end_s: float,
    transcript_slices: Callable[[float, float], str] | None,
    paraphrase: Callable[[str], str] | None,
) -> str:
    """Paraphrase of the clip's speech when available, else the story beat.

    A paraphrase that repeats an 8-char fragment of its own slice is
    dropped here, so the digest never depends on a round-dir transcript
    being present for the leak check to see it.
    """
    if transcript_slices is not None and paraphrase is not None:
        speech = transcript_slices(start_s, end_s)
        if speech:
            said = paraphrase(speech)
            if said and not _leaks([speech], [_norm(said)], _VOICE_LEAK_MIN_LEN):
                return said
            if said:
                logger.warning("digest: paraphrase repeated its transcript; using the beat")
    return _beat_text(beats)


def _energy(energy: Callable[[float, float], str] | None, start_s: float, end_s: float) -> str:
    if energy is None:
        return "unknown"
    label = energy(start_s, end_s)
    return label if label in get_args(Energy) else "unknown"


def _mm_ss(seconds: float) -> str:
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


def _commentary(
    owner_rows: list[dict],
    beats: list[tuple[float, float, str]],
    transcript_slices: Callable[[float, float], str] | None,
    paraphrase: Callable[[str], str] | None,
    energy: Callable[[float, float], str] | None,
) -> list[CommentaryItem]:
    """Up to 5 owner voice moments outside the kept clips. `voice` is a
    paraphrase when one is injected (and does not repeat its own text), else
    "<energy>: <nearest clip's beat>", else the energy alone."""
    items: list[CommentaryItem] = []
    for u in commentary_candidates(owner_rows, [(s, e) for s, e, _ in beats]):
        start_s, end_s = u["start_s"], u["end_s"]
        label = _energy(energy, start_s, end_s)
        near = min(beats, key=lambda b: abs(b[0] - start_s), default=None)
        beat = near[2] or None if near else None
        voice = ""
        if paraphrase is not None:
            said = paraphrase(u["text"])
            if said and not _leaks([u["text"]], [_norm(said)], _VOICE_LEAK_MIN_LEN):
                voice = said
        if not voice:
            voice = f"{label}: {beat}" if beat else label
        items.append(
            CommentaryItem(
                ref=_mm_ss(start_s),
                start_s=start_s,
                end_s=end_s,
                voice=voice,
                energy=label,
                attached_beat=beat,
            )
        )
    return items


def build_digest(
    round_dir: Path,
    *,
    game: str,
    timeline: RiotTimeline | None = None,
    transcript_slices: Callable[[float, float], str] | None = None,
    paraphrase: Callable[[str], str] | None = None,
    energy: Callable[[float, float], str] | None = None,
    lane: LaneSummary | None = None,
    owner_rows: list[dict] | None = None,
) -> Digest:
    clips = _load(round_dir / "clips.json")
    clips = clips if isinstance(clips, list) else []
    gaming = _by_ref(_load(round_dir / "gaming-expert.json"))
    viewer_raw = _load(round_dir / "viewer-expert.json")
    viewer = _by_ref(viewer_raw.get("clips") if isinstance(viewer_raw, dict) else None)

    out: list[DigestClip] = []
    beats: list[tuple[float, float, str]] = []  # (start, end, beat) per kept clip
    for clip in clips:
        if (
            not isinstance(clip, dict)
            or not isinstance(clip.get("file"), str)
            or not all(
                isinstance(clip.get(k), int | float) for k in ("start_seconds", "end_seconds")
            )
        ):
            logger.warning("digest: skipping a clips.json entry without file/start/end")
            continue
        ref = _ref_from_file(clip["file"])
        key = normalize_clip_ref(ref)
        g = gaming.get(key) or {}
        v = viewer.get(key) or {}
        start_s, end_s = clip["start_seconds"], clip["end_seconds"]
        beats.append((start_s, end_s, _beat_text(g.get("story_beats"))))
        out.append(
            DigestClip(
                ref=ref,
                event=clip.get("event", ""),
                start_s=start_s,
                end_s=end_s,
                panel={
                    "gaming_quality": g.get("quality"),
                    "hook_verdict": v.get("hook_verdict"),
                    "format": None,
                },
                critic=clip.get("vlm_verdict"),
                voice=_voice(g.get("story_beats"), start_s, end_s, transcript_slices, paraphrase),
                copy=v.get("copy"),
                # R4: a low-confidence match may be another game; ship none of its events.
                events=(
                    _events(timeline, start_s, end_s)
                    if timeline and timeline.confidence != "low"
                    else []
                ),
                game_state=_game_state(timeline, start_s, end_s) if timeline else None,
                energy=_energy(energy, start_s, end_s),
                match_confidence=_match_confidence(timeline),
            )
        )
    return Digest(
        game=game,
        round=round_dir.name,
        clips=out,
        commentary=_commentary(owner_rows or [], beats, transcript_slices, paraphrase, energy),
        built_at=datetime.now(timezone.utc).isoformat(),
        lane=lane,
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


def _leaks(lines: list[str], haystacks: list[str], min_len: int) -> list[str]:
    """Fragments (>= min_len chars) of each line found in any normalised haystack.

    Every `min_len`-char window of each normalised line is checked;
    overlapping matching windows are merged into one reported fragment.
    """
    leaks: list[str] = []
    for line in lines:
        text = _norm(line)
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


def digest_leaks(digest: Digest, transcript_path: Path, *, min_len: int = 12) -> list[str]:
    """Transcript fragments (>= min_len chars) found in any digest string.

    Compares decoded, normalised (casefolded, whitespace-collapsed) text, so
    quotes, backslashes and newlines cannot hide a match. `voice` is held
    to the same `min_len` as every other field: an 8-char window against
    the whole round would refuse common phrases. The stricter R8 guard is
    per clip, in `build_digest`, against the clip's own transcript slice.
    """
    lines = [
        line.get("text", "") for line in json.loads(transcript_path.read_text(encoding="utf-8"))
    ]
    return _leaks(lines, _strings(digest.model_dump()), min_len)


def annotate_with_jev(digest: Digest, ask_many: Callable[..., list[Answer]] = ask_many) -> Digest:
    """Add `format` and `include_p` to each clip's panel via Jev."""
    states = [{"clip": c.model_dump()} for c in digest.clips]
    formats = ask_many([(_FORMAT_QUESTION, s) for s in states])
    includes = ask_many([(_INCLUDE_QUESTION, s) for s in states])
    for clip, fmt, inc in zip(digest.clips, formats, includes, strict=True):
        clip.panel["format"] = str(fmt.value) if fmt.value is not None else None
        clip.panel["include_p"] = inc.probability
    return digest
