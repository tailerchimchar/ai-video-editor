"""YouTube Shorts / TikTok / Reels pipeline.

Reads the highlights folder produced by `build_highlights` (per-clip
mp4 files + `index.json`), buckets clips by narrative intent
(laning phase / teamfight / objective steal / multikill / first blood /
outplay / etc.), and renders 9:16 blur-fill shorts ready for posting.

**Deterministic by design.** Same asset + same args -> byte-identical
output. No random sampling anywhere. Bucketing is rule-based (pure
functions of `anchor_seconds` + `event_type` + `reason` string). The
plan file `.claude/plans/cached-wibbling-hejlsberg.md` documents the
exact rules.

Two modes ride the same render path:

- `voiceover` — 1 clip per short, source audio ducked to
  `shorts_source_duck_volume`, index.md carries a suggested VO prompt
  per bucket. User dubs their own narration on top in their DAW.
- `montage` — adjacent clips within a bucket get packed into one short
  (up to `shorts_max_clips_per_short`), optional royalty-free music
  bed mixed under source audio.

Both modes finish with an existing-VLM coherence check
(`validator.validate_compilation`) so a broken bucket gets flagged in
the index.md, not silently shipped.

Public entry points:
- `categorize_clips(clips)` -> dict[bucket_slug, list[clip]]
- `plan_shorts(clips, mode, topic=None)` -> list[ShortPlan]
- `render_short(plan, source_paths, out_path, mode, music_path=None)`
- `build_shorts(asset, mode, topic=None, music_path=None)` — top-level
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import settings
from .editing import trim_clip
from .edits import (
    BLUR_FILL_9X16_LABELS,
    CROPPED_HUD_9X16_LABELS,
    _escape_drawtext,
    _escape_fontfile_path,
    blur_fill_9x16,
    cropped_hud_9x16,
)

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Bucket definitions (deterministic, rule-based)
# ---------------------------------------------------------------------

# Phase cutoffs (per the plan file: 0-15 laning, 15-25 mid, 25+ late).
_LANING_END_SECONDS = 900.0   # 15 min
_MID_END_SECONDS = 1500.0     # 25 min

# Adjacency rules for multikill / teamfight detection.
_MULTIKILL_ADJACENCY_SECONDS = 30.0
_TEAMFIGHT_WINDOW_SECONDS = 60.0
_TEAMFIGHT_MIN_ADJACENT = 3

_OBJECTIVE_EVENTS = frozenset({"baron", "dragon", "herald"})
_KILL_ISH_EVENTS = frozenset({"kill", "doublekill", "triplekill", "quadrakill", "pentakill"})

# Outplay heuristics — case-insensitive substring match against the
# ranker's `reason` field. See the plan file for the rationale.
_OUTPLAY_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"outnumbered",
        r"outplay",
        r"\b1v[2-5]\b",
        r"\b2v[3-5]\b",
        r"\b3v[4-5]\b",
        r"solo kill of two",
    )
)

# Voice-over priority tiers. Higher = surfaces earlier in the render
# list, so `short_01_*` is always the user's top-ranked category.
# User's stated ranking: laning-phase kill > 3+-kill multikill >
# teamfight-turnaround > everything else.
_PRIORITY_LANING_KILL = 300
_PRIORITY_MULTIKILL_3PLUS = 200
_PRIORITY_TEAMFIGHT_TURN = 100
_PRIORITY_DEFAULT = 0

# "Turned the game around" heuristic — deterministic substring match on
# the ranker's `reason` string. Zero new LLM calls; the ranker already
# emits these words naturally when the play is a comeback / clutch.
_TURNAROUND_PATTERN = re.compile(
    r"\b(comeback|clutch|turn(ed|ing|around)?|save[sd]?|1v[3-5]|outplayed)\b",
    re.IGNORECASE,
)


BUCKET_ORDER = (
    "first_blood",
    "outplay",
    "multikill",
    "teamfight",
    "objective_steal",
    "laning_phase",
    "mid_game",
    "late_game",
)


# ---------------------------------------------------------------------
# Text templates (see plan §6, §7 — deterministic per bucket)
# ---------------------------------------------------------------------


_TITLE_TEMPLATES: dict[str, str] = {
    "first_blood": "FIRST BLOOD",
    "laning_phase": "LANING KILL",
    "mid_game": "MID GAME KILL",
    "late_game": "LATE GAME KILL",
    "objective_steal": "OBJECTIVE STEAL",
    "teamfight": "TEAMFIGHT",
    "multikill": "MULTIKILL",
    "outplay": "OUTPLAY",
}

_MULTIKILL_TITLES: dict[int, str] = {
    2: "DOUBLE KILL",
    3: "TRIPLE KILL",
    4: "QUADRA KILL",
    5: "PENTA KILL",
}

_VO_PROMPTS: dict[str, str] = {
    "first_blood": "How I got first blood",
    "laning_phase": "How I set up this kill in laning phase",
    "mid_game": "The trade / positioning that got me this",
    "late_game": "How I closed out the game",
    "objective_steal": "How I stole this objective",
    "teamfight": "What I was thinking during this teamfight",
    "multikill": "Setting up the multikill",
    "outplay": "How I outplayed the enemy",
}


# ---------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class ShortClip:
    """One clip destined for a short. Immutable snapshot of the
    highlights-folder row + a resolved on-disk path."""

    file: str
    """Filename in the highlights folder (e.g. `01_kill_14m32s.mp4`)."""

    path: Path
    """Full on-disk path to the clip file."""

    event_type: str
    start_seconds: float
    end_seconds: float
    hype_score: float
    funny_score: float
    story_score: float
    reason: str

    @property
    def anchor_seconds(self) -> float:
        """Best proxy for the moment: midpoint of the clip window.
        (Anchor isn't in index.json today; we approximate.)"""
        return (self.start_seconds + self.end_seconds) / 2.0

    @property
    def mmss(self) -> str:
        s = int(self.anchor_seconds)
        return f"{s // 60}m{s % 60:02d}s"


@dataclass(frozen=True)
class ShortPlan:
    """One rendered short's plan — the clips + the bucket + labels."""

    bucket: str
    clips: tuple[ShortClip, ...]
    title: str
    """The overlay text (e.g. `TRIPLE KILL`)."""

    vo_prompt: str
    """Suggested voice-over prompt (goes into index.md)."""

    index: int = 0
    """1-based ordering across all shorts in this compile."""

    priority: int = 0
    """Higher = surfaces earlier in voice-over mode. See `_plan_priority`."""


@dataclass
class RenderResult:
    """Outcome of rendering one short."""

    plan: ShortPlan
    out_path: Path
    ok: bool
    error: str | None = None
    vlm_verdict: str | None = None
    vlm_why: str | None = None
    duration_seconds: float | None = None
    music_path: str | None = None
    extras: dict = field(default_factory=dict)


# ---------------------------------------------------------------------
# Bucketing — pure functions
# ---------------------------------------------------------------------


def _clip_from_index_entry(entry: dict, folder: Path) -> ShortClip | None:
    """Coerce an index.json clip entry into a `ShortClip`. Skips clips
    that never actually rendered (`ok=False`)."""
    if not entry.get("ok"):
        return None
    file = entry.get("file")
    if not file:
        return None
    return ShortClip(
        file=file,
        path=(folder / file),
        event_type=str(entry.get("event") or "clip"),
        start_seconds=float(entry.get("start_seconds") or 0),
        end_seconds=float(entry.get("end_seconds") or 0),
        hype_score=float(entry.get("hype_score") or 0),
        funny_score=float(entry.get("funny_score") or 0),
        story_score=float(entry.get("story_score") or 0),
        reason=str(entry.get("reason") or ""),
    )


def load_clips(folder: Path) -> list[ShortClip]:
    """Read the highlights folder's `index.json` -> list of ShortClip.

    Returns empty when the file is missing or unreadable (never raises).
    """
    idx = folder / "index.json"
    if not idx.is_file():
        return []
    try:
        data = json.loads(idx.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = data.get("clips") or []
    out: list[ShortClip] = []
    for e in entries:
        c = _clip_from_index_entry(e, folder)
        if c is not None:
            out.append(c)
    return out


def _phase_bucket(anchor_s: float) -> str:
    if anchor_s < _LANING_END_SECONDS:
        return "laning_phase"
    if anchor_s < _MID_END_SECONDS:
        return "mid_game"
    return "late_game"


def _is_kill_event(event: str) -> bool:
    return event in _KILL_ISH_EVENTS or event == "assist"


def _matches_outplay(reason: str) -> bool:
    return any(p.search(reason) for p in _OUTPLAY_PATTERNS)


def _find_adjacent_indices(
    sorted_clips: list[ShortClip],
    window_seconds: float,
) -> list[list[int]]:
    """Greedy-group adjacent clips whose anchors fall within
    `window_seconds` of the running cluster's last anchor. Returns list
    of index groups. Pure; deterministic given sort order."""
    if not sorted_clips:
        return []
    groups: list[list[int]] = [[0]]
    for i in range(1, len(sorted_clips)):
        prev_anchor = sorted_clips[groups[-1][-1]].anchor_seconds
        cur_anchor = sorted_clips[i].anchor_seconds
        if cur_anchor - prev_anchor <= window_seconds:
            groups[-1].append(i)
        else:
            groups.append([i])
    return groups


def categorize_clips(
    clips: list[ShortClip],
    *,
    first_blood_source: list[ShortClip] | None = None,
) -> dict[str, list[ShortClip]]:
    """Assign each clip to zero or more buckets. Pure. Deterministic.

    Rules per the plan file §2:
      first_blood     — the earliest kill event. When `first_blood_source`
                        is provided, it's computed against that fuller
                        list (so a threshold-filtered `clips` doesn't
                        mis-label the second kill as first blood). The
                        earliest-kill clip must also appear in `clips`
                        to be tagged (otherwise no first_blood short is
                        emitted, since below-threshold clips don't ship).
      laning_phase    — anchor_seconds < 900
      mid_game        — 900 <= anchor < 1500
      late_game       — anchor >= 1500
      objective_steal — event_type in {baron, dragon, herald}
      multikill       — an adjacency-within-30s kill/assist cluster (>=2 clips)
      teamfight       — 3+ adjacent clips within a 60s window
      outplay         — reason string matches outplay patterns

    A clip can appear in multiple buckets (e.g. late_game + teamfight).
    Buckets returned in `BUCKET_ORDER`. Missing buckets are omitted.
    """
    if not clips:
        return {}

    # Sort by anchor for deterministic downstream ordering
    by_anchor = sorted(clips, key=lambda c: (c.anchor_seconds, c.file))

    out: dict[str, list[ShortClip]] = {b: [] for b in BUCKET_ORDER}

    # first_blood — the very first kill event. Computed against the
    # FULL clip list when caller passes one, so a below-threshold first
    # kill can't be mis-attributed to a later above-threshold clip.
    fb_source = first_blood_source if first_blood_source is not None else clips
    fb_sorted = sorted(fb_source, key=lambda c: (c.anchor_seconds, c.file))
    first_kill = next((c for c in fb_sorted if _is_kill_event(c.event_type)), None)
    # Only tag as first_blood if the actual first kill made it past the
    # caller's filter (i.e. it's in `clips`).
    if first_kill is not None and any(c.file == first_kill.file for c in clips):
        out["first_blood"].append(first_kill)

    # Phase buckets — every clip lands in exactly one
    for c in by_anchor:
        out[_phase_bucket(c.anchor_seconds)].append(c)

    # objective_steal
    for c in by_anchor:
        if c.event_type in _OBJECTIVE_EVENTS:
            out["objective_steal"].append(c)

    # multikill — every clip in a >= 2-clip adjacency group where all
    # are kill-ish events. Groups computed once (deterministic).
    kill_ish = [c for c in by_anchor if _is_kill_event(c.event_type)]
    groups = _find_adjacent_indices(kill_ish, _MULTIKILL_ADJACENCY_SECONDS)
    for g in groups:
        if len(g) >= 2:
            out["multikill"].extend(kill_ish[i] for i in g)

    # teamfight — 3+ adjacent clips (any event) within a 60s window
    tf_groups = _find_adjacent_indices(by_anchor, _TEAMFIGHT_WINDOW_SECONDS)
    for g in tf_groups:
        if len(g) >= _TEAMFIGHT_MIN_ADJACENT:
            out["teamfight"].extend(by_anchor[i] for i in g)

    # outplay — reason text match
    for c in by_anchor:
        if _matches_outplay(c.reason):
            out["outplay"].append(c)

    # Drop empty buckets, preserve insertion order (which is BUCKET_ORDER)
    return {k: v for k, v in out.items() if v}


# ---------------------------------------------------------------------
# Planning — turn buckets into concrete ShortPlan objects
# ---------------------------------------------------------------------


def _title_for(bucket: str, clip_count: int) -> str:
    """Deterministic title for a short, per the plan §6."""
    if bucket == "multikill" and clip_count in _MULTIKILL_TITLES:
        return _MULTIKILL_TITLES[clip_count]
    return _TITLE_TEMPLATES.get(bucket, bucket.upper())


def _vo_prompt_for(bucket: str) -> str:
    return _VO_PROMPTS.get(bucket, "Voice-over the moment")


def _bucket_slug(bucket: str) -> str:
    """Filesystem-safe bucket slug for filenames."""
    return re.sub(r"[^a-z0-9_]+", "_", bucket.lower()).strip("_") or "clip"


def _plan_priority(
    bucket: str,
    clips: tuple[ShortClip, ...],
    *,
    multikill_group_size: int | None = None,
) -> int:
    """Score a plan against the user's stated ranking.

    Order (first match wins):
      1. laning_phase + kill event      → _PRIORITY_LANING_KILL
      2. multikill parent group >= 3     → _PRIORITY_MULTIKILL_3PLUS
      3. teamfight + turnaround signal   → _PRIORITY_TEAMFIGHT_TURN
      4. everything else                 → _PRIORITY_DEFAULT

    `multikill_group_size` is the size of the adjacency cluster this
    plan's clips came from. Callers computing per-clip VO plans MUST
    pass it (a triple-kill in VO mode is 3 separate 1-clip shorts, so
    `len(clips)` alone won't tell us the parent group is 3). Falls
    back to `len(clips)` for the montage case where the plan itself
    IS the group.

    Turnaround signal reads the ranker's `reason` string via
    `_TURNAROUND_PATTERN` — no new LLM call.

    Pure. Deterministic given the same clips.
    """
    if bucket == "laning_phase" and any(
        _is_kill_event(c.event_type) for c in clips
    ):
        return _PRIORITY_LANING_KILL
    if bucket == "multikill":
        size = multikill_group_size if multikill_group_size is not None else len(clips)
        if size >= 3:
            return _PRIORITY_MULTIKILL_3PLUS
    if bucket == "teamfight" and any(
        _TURNAROUND_PATTERN.search(c.reason) for c in clips
    ):
        return _PRIORITY_TEAMFIGHT_TURN
    return _PRIORITY_DEFAULT


def _multikill_group_sizes(
    bucket_clips: list[ShortClip],
) -> dict[str, int]:
    """Map each multikill clip's `file` → the size of its parent
    adjacency group. Reuses `_find_adjacent_indices` — same rule the
    bucketing itself uses."""
    kill_ish = [c for c in bucket_clips if _is_kill_event(c.event_type)]
    groups = _find_adjacent_indices(kill_ish, _MULTIKILL_ADJACENCY_SECONDS)
    out: dict[str, int] = {}
    for g in groups:
        size = len(g)
        for i in g:
            out[kill_ish[i].file] = size
    return out


def plan_shorts(
    clips: list[ShortClip],
    *,
    mode: str,
    topic: str | None = None,
    hype_threshold: float | None = None,
    adjacency_seconds: float | None = None,
    max_clips_per_short: int | None = None,
) -> list[ShortPlan]:
    """Turn a list of clips into a list of ShortPlan objects.

    - Filter clips below `hype_threshold` (default from settings)
    - Bucket the survivors
    - Filter by `topic` if given (case-insensitive substring match on
      bucket slugs, so `topic="kill"` matches `multikill`)
    - For each bucket, emit ShortPlan(s):
        * mode="voiceover": 1 clip per plan
        * mode="montage": adjacency-grouped, capped at max_clips_per_short

    Returns plans ordered chronologically by earliest clip's anchor.
    NN indexes assigned in that order.

    Pure; no I/O.
    """
    hype_threshold = (
        settings.shorts_hype_threshold if hype_threshold is None else hype_threshold
    )
    adjacency_seconds = (
        settings.shorts_adjacency_seconds
        if adjacency_seconds is None
        else adjacency_seconds
    )
    max_clips_per_short = (
        settings.shorts_max_clips_per_short
        if max_clips_per_short is None
        else max_clips_per_short
    )

    kept = [c for c in clips if c.hype_score >= hype_threshold]
    if not kept:
        return []

    # Pass the full clip list as `first_blood_source` so we don't
    # mislabel the second-earliest kill as first_blood when the actual
    # first kill fell below hype_threshold.
    buckets = categorize_clips(kept, first_blood_source=clips)
    if topic:
        topic_l = topic.lower()
        buckets = {b: cs for b, cs in buckets.items() if topic_l in b.lower()}

    plans: list[ShortPlan] = []
    for bucket, bucket_clips in buckets.items():
        bucket_clips = sorted(
            set(bucket_clips), key=lambda c: (c.anchor_seconds, c.file)
        )
        # For multikill VO plans, each single-clip plan needs to know the
        # size of its parent adjacency group. Precompute once per bucket.
        mk_sizes = (
            _multikill_group_sizes(bucket_clips) if bucket == "multikill" else {}
        )
        if mode == "voiceover":
            for c in bucket_clips:
                tup = (c,)
                plans.append(
                    ShortPlan(
                        bucket=bucket,
                        clips=tup,
                        title=_title_for(bucket, 1),
                        vo_prompt=_vo_prompt_for(bucket),
                        priority=_plan_priority(
                            bucket, tup, multikill_group_size=mk_sizes.get(c.file)
                        ),
                    )
                )
        elif mode == "montage":
            groups = _find_adjacent_indices(bucket_clips, adjacency_seconds)
            for g in groups:
                # Chunk oversized groups to respect the safety cap
                for start in range(0, len(g), max_clips_per_short):
                    chunk = g[start : start + max_clips_per_short]
                    tup = tuple(bucket_clips[i] for i in chunk)
                    plans.append(
                        ShortPlan(
                            bucket=bucket,
                            clips=tup,
                            title=_title_for(bucket, len(tup)),
                            vo_prompt=_vo_prompt_for(bucket),
                            priority=_plan_priority(bucket, tup),
                        )
                    )
        else:
            raise ValueError(f"unknown mode {mode!r}; expected voiceover|montage")

    # Voice-over sorts by priority DESC (user's ranking) then chronological.
    # Montage stays chronological — its output is per-adjacency-cluster
    # and reordering by priority would fragment the game's narrative flow.
    if mode == "voiceover":
        plans.sort(
            key=lambda p: (-p.priority, p.clips[0].anchor_seconds, p.clips[0].file)
        )
    else:
        plans.sort(key=lambda p: (p.clips[0].anchor_seconds, p.clips[0].file))

    return [
        ShortPlan(
            bucket=p.bucket,
            clips=p.clips,
            title=p.title,
            vo_prompt=p.vo_prompt,
            index=i + 1,
            priority=p.priority,
        )
        for i, p in enumerate(plans)
    ]


def short_filename(plan: ShortPlan) -> str:
    """Deterministic filename for a plan.

    `short_<NN>_[HIGH_]<bucket>_<mmss>.mp4` where mmss is the anchor of
    the first (earliest) clip. The `HIGH_` infix appears specifically
    for 3+-kill multikills — the user's "REALLY high priority" flag.
    Laning kills also outrank normal multikills in VO ordering, but
    they don't get HIGH_ because that flag is about kill-count intensity
    (2 vs 3+), not narrative priority.
    """
    mmss = plan.clips[0].mmss
    high = (
        "HIGH_"
        if plan.bucket == "multikill" and plan.priority >= _PRIORITY_MULTIKILL_3PLUS
        else ""
    )
    return f"short_{plan.index:02d}_{high}{_bucket_slug(plan.bucket)}_{mmss}.mp4"


# ---------------------------------------------------------------------
# Render — the ffmpeg driver
# ---------------------------------------------------------------------


def _title_drawtext(title: str) -> str:
    """`drawtext` filter fragment that overlays `title` for the first
    ~2.5 seconds of the short."""
    safe = _escape_drawtext(title)
    ff = _escape_fontfile_path(settings.caption_font_path)
    return (
        f"drawtext=fontfile='{ff}':text='{safe}':"
        f"fontsize=68:fontcolor=white:borderw=6:bordercolor=black:"
        f"x=(w-text_w)/2:y=140:enable='between(t,0.2,2.5)'"
    )


_LAYOUT_BUILDERS: dict[str, tuple[callable, tuple[str, ...]]] = {  # type: ignore[type-arg]
    "blur_fill": (blur_fill_9x16, BLUR_FILL_9X16_LABELS),
    "cropped_hud": (cropped_hud_9x16, CROPPED_HUD_9X16_LABELS),
}


def _indexed_layout_chain(layout: str, i: int) -> str:
    """Return the per-clip filter chain for `layout` with all internal
    labels suffixed by `i` so multiple clips can be concatenated.

    Raises `ValueError` for unknown layouts.
    """
    if layout not in _LAYOUT_BUILDERS:
        raise ValueError(
            f"unknown shorts layout {layout!r}; "
            f"expected one of {sorted(_LAYOUT_BUILDERS)}"
        )
    builder, labels = _LAYOUT_BUILDERS[layout]
    chain = builder().replace("[0:v]", f"[{i}:v]")
    for lbl in labels:
        chain = chain.replace(f"[{lbl}]", f"[{lbl}{i}]")
    return chain.replace("[out]", f"[v{i}]")


def _build_filter_complex(
    *,
    n_video_inputs: int,
    title: str,
    mode: str,
    music_input_index: int | None,
    duck_volume: float,
    music_volume: float,
    layout: str = "cropped_hud",
    beat_flash_filter: str | None = None,
) -> str:
    """Assemble the full `-filter_complex` string.

    Video path:
      For each source video, apply the layout chain (`blur_fill` or
      `cropped_hud`) yielding `[vN]`, concat all into `[vcat]`, optionally
      apply a beat-flash effect, then draw the title.
    Audio path:
      Concat source audio -> `[asrc]`. If VO mode: volume=duck.
      If montage + music present: amix with the music track.

    `beat_flash_filter` is an already-built ffmpeg filter fragment from
    `music.build_beat_effect_filter` reading `[vcat]` and writing
    `[vflashed]`. When None, the title overlays directly on `[vcat]`.

    Result: `[vout][aout]`.
    """
    parts: list[str] = []
    for i in range(n_video_inputs):
        parts.append(_indexed_layout_chain(layout, i))

    # Concat video streams (video-only concat, `n=N:v=1:a=0`)
    concat_v_ins = "".join(f"[v{i}]" for i in range(n_video_inputs))
    parts.append(f"{concat_v_ins}concat=n={n_video_inputs}:v=1:a=0[vcat]")

    # Optional beat-flash between concat and title. Reads [vcat],
    # writes [vflashed] which becomes the input to drawtext.
    video_pre_title = "vcat"
    if beat_flash_filter is not None:
        parts.append(beat_flash_filter)
        video_pre_title = "vflashed"

    # Title overlay on the (possibly flashed) concatenated video
    parts.append(f"[{video_pre_title}]{_title_drawtext(title)}[vout]")

    # Concat source audio
    concat_a_ins = "".join(f"[{i}:a]" for i in range(n_video_inputs))
    parts.append(f"{concat_a_ins}concat=n={n_video_inputs}:v=0:a=1[asrc]")

    if mode == "voiceover":
        parts.append(f"[asrc]volume={duck_volume:.2f}[aout]")
    elif mode == "montage":
        if music_input_index is not None:
            parts.append(
                f"[{music_input_index}:a]volume={music_volume:.2f}[amus];"
                f"[asrc][amus]amix=inputs=2:duration=first:"
                f"dropout_transition=0[aout]"
            )
        else:
            parts.append("[asrc]anull[aout]")
    else:
        raise ValueError(f"unknown mode {mode!r}")

    return ";".join(parts)


def _kill_times_in_montage(plan: ShortPlan) -> tuple[float, ...]:
    """Absolute times (from t=0 of the montage output) at which each
    clip's kill-moment plays. Assumes clips concat back-to-back and
    the kill sits at each clip's midpoint (matches how highlights are
    cut — anchor centered in the window)."""
    times: list[float] = []
    montage_t = 0.0
    for clip in plan.clips:
        duration = clip.end_seconds - clip.start_seconds
        times.append(montage_t + duration / 2.0)
        montage_t += duration
    return tuple(times)


_FLASH_SOURCES = frozenset({"kills", "downbeats", "beats"})


def _flash_times(
    plan: ShortPlan,
    music_path: str,
    source: str,
    max_t: float,
) -> tuple[float, ...]:
    """Pick the timestamps (in montage time) to fire flashes at.

    Sources:
      kills     -> once per clip at its kill midpoint (default, 3-5
                   flashes; tastefully rare)
      downbeats -> every 4th detected beat (~one per bar)
      beats     -> every detected beat (hyperactive)
    """
    if source not in _FLASH_SOURCES:
        raise ValueError(
            f"unknown beat_flash_source {source!r}; "
            f"expected one of {sorted(_FLASH_SOURCES)}"
        )
    if source == "kills":
        return tuple(t for t in _kill_times_in_montage(plan) if t <= max_t)
    # Beat-based sources need actual beat detection
    from .music import detect_beats

    track = detect_beats(music_path)
    if not track.beats:
        return ()
    beats = tuple(b for b in track.beats if b <= max_t)
    if source == "downbeats":
        return beats[::4]
    return beats  # source == "beats"


def _compute_beat_flash_filter(
    plan: ShortPlan,
    music_path: str | None,
    beat_flash_enabled: bool,
    source: str | None = None,
) -> str | None:
    """Build the flash filter fragment. Returns None if flag off or no
    flash times could be computed. Never aborts the render on failure."""
    if not beat_flash_enabled:
        return None
    resolved_source = source or settings.shorts_beat_flash_source
    # Beat-based sources need music; kills doesn't.
    if resolved_source != "kills" and not music_path:
        return None
    try:
        from .music import build_beat_effect_filter

        max_t = settings.shorts_max_duration_seconds
        times = _flash_times(plan, music_path or "", resolved_source, max_t)
        if not times:
            return None
        return build_beat_effect_filter(
            "flash",
            "vcat",
            "vflashed",
            times,
            intensity=settings.shorts_beat_flash_intensity,
            duration_seconds=settings.shorts_beat_flash_duration_seconds,
        )
    except Exception as exc:
        _log.warning(
            "beat-flash setup failed (non-fatal, rendering without flash): %s",
            exc,
        )
        return None


def render_short(
    plan: ShortPlan,
    out_path: Path,
    *,
    mode: str,
    music_path: str | None = None,
    layout: str | None = None,
    beat_flash: bool | None = None,
    beat_flash_source: str | None = None,
) -> tuple[bool, str | None]:
    """Render one short from its plan. Returns (ok, error_message_or_none).

    Uses ffmpeg with `-filter_complex`. Re-encodes (H.264 + AAC) because
    the blur-fill filter chain requires it. Bounded stderr keeps the
    error message readable.

    `beat_flash` (default: config `shorts_beat_flash_enabled`) overlays
    a subtle white pulse. `beat_flash_source` selects what to flash on:
    `kills` (default), `downbeats`, or `beats`. `kills` doesn't need
    music; the others do.
    """
    if not plan.clips:
        return False, "empty plan"

    out_path.parent.mkdir(parents=True, exist_ok=True)

    duck = settings.shorts_source_duck_volume
    music_vol = settings.shorts_montage_music_volume

    # Assemble input args: one -i per clip, plus optionally -i music
    input_args: list[str] = []
    for clip in plan.clips:
        input_args.extend(["-i", str(clip.path)])
    music_input_index: int | None = None
    if mode == "montage" and music_path:
        input_args.extend(["-i", music_path])
        music_input_index = len(plan.clips)

    beat_flash_resolved = (
        settings.shorts_beat_flash_enabled if beat_flash is None else beat_flash
    )
    beat_flash_filter = _compute_beat_flash_filter(
        plan,
        music_path if mode == "montage" else None,
        beat_flash_resolved,
        source=beat_flash_source,
    )

    filter_complex = _build_filter_complex(
        n_video_inputs=len(plan.clips),
        title=plan.title,
        mode=mode,
        music_input_index=music_input_index,
        duck_volume=duck,
        music_volume=music_vol,
        layout=layout or settings.shorts_layout,
        beat_flash_filter=beat_flash_filter,
    )

    cmd = [
        settings.ffmpeg_path,
        "-y",
        "-hide_banner",
        *input_args,
        "-filter_complex",
        filter_complex,
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-t",
        str(settings.shorts_max_duration_seconds),
        str(out_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, f"ffmpeg render failed: {exc}"
    if result.returncode != 0:
        return False, (result.stderr or "ffmpeg render failed")[-1500:]
    return True, None


# ---------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------


def _shorts_folder(asset: dict, mode: str) -> Path:
    """`<workspace>/shorts/<asset-stem>_<mode>/`."""
    stem = Path(asset.get("filename") or asset.get("id") or "unknown").stem
    return settings.workspace_dir / "shorts" / f"{stem}_{mode}"


def _highlights_folder_for_asset(asset: dict, candidates: list[dict]) -> Path | None:
    """Best-effort lookup of the highlights folder for an asset.

    Reuses `highlights.relative_folder` — no re-invention. Returns None
    if the folder doesn't exist on disk.
    """
    # Imported lazily so callers who don't need highlights (unit tests)
    # don't pay the transitive import cost.
    from .highlights import relative_folder

    try:
        rel = relative_folder(asset, candidates)
    except Exception as exc:
        _log.warning("relative_folder failed for asset %s: %s", asset.get("id"), exc)
        return None
    folder = settings.workspace_dir / rel
    return folder if folder.is_dir() else None


def _write_index_md(
    folder: Path,
    *,
    asset: dict,
    mode: str,
    topic: str | None,
    music_path: str | None,
    results: list[RenderResult],
) -> None:
    """Human-readable index.md per the plan §9."""
    lines: list[str] = []
    lines.append(f"# Shorts for {asset.get('filename', '?')} ({mode})")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append(
        f"Args: mode={mode}, topic={topic or 'all'}, "
        f"music_path={music_path or 'none'}, "
        f"threshold={settings.shorts_hype_threshold}"
    )
    lines.append("")
    for r in results:
        plan = r.plan
        status = "OK" if r.ok else "FAILED"
        lines.append(f"## {r.out_path.name}  [{status}]")
        lines.append(f"Bucket: {plan.bucket}")
        if plan.priority > 0:
            high = (
                " HIGH"
                if plan.bucket == "multikill"
                and plan.priority >= _PRIORITY_MULTIKILL_3PLUS
                else ""
            )
            lines.append(f"Priority: {plan.priority}{high}")
        clip_summary = ", ".join(
            f"{c.mmss} ({c.event_type})" for c in plan.clips
        )
        lines.append(f"Clips ({len(plan.clips)}): {clip_summary}")
        lines.append(f"Title overlay: {plan.title}")
        lines.append(f"VO prompt: {plan.vo_prompt}")
        if r.music_path:
            lines.append(f"Music: {r.music_path}")
        if r.vlm_verdict is not None:
            lines.append(f"VLM coherence: {r.vlm_verdict} — {r.vlm_why or ''}")
        # Refinement audit trail — makes it obvious when the auto-fix
        # loop actually rewrote a clip's window.
        refinements = (r.extras or {}).get("refinements_applied") or []
        iters = (r.extras or {}).get("refine_iterations", 0)
        stopped = (r.extras or {}).get("refine_stopped")
        if refinements:
            lines.append(f"Refinements ({iters} iter, stopped: {stopped or 'ok'}):")
            for entry in refinements:
                lines.append(f"  - {entry}")
        elif iters > 0 and stopped:
            lines.append(f"Refine loop: {iters} iter, stopped: {stopped}")
        if not r.ok:
            lines.append(f"Error: {r.error}")
        lines.append("")
    (folder / "index.md").write_text("\n".join(lines), encoding="utf-8")


def build_shorts(
    asset: dict,
    candidates: list[dict],
    *,
    mode: str,
    topic: str | None = None,
    music_path: str | None = None,
    layout: str | None = None,
    beat_flash: bool | None = None,
    beat_flash_source: str | None = None,
    run_vlm_check: bool = True,
) -> dict:
    """Top-level entry: bucket -> plan -> render -> VLM check -> index.md.

    Returns a summary dict shaped like `build_highlights` for consistency.
    Never raises for individual short-render failures — logs them in
    `index.md` and continues.
    """
    if mode not in ("voiceover", "montage"):
        raise ValueError(f"unknown mode {mode!r}; expected voiceover|montage")

    hl_folder = _highlights_folder_for_asset(asset, candidates)
    if hl_folder is None:
        return {
            "ok": False,
            "error": (
                "highlights folder not found for this asset — run "
                "analyze_asset(cut=True) first"
            ),
            "shorts": [],
        }

    clips = load_clips(hl_folder)
    if not clips:
        return {
            "ok": False,
            "error": f"no clips available in {hl_folder}",
            "shorts": [],
        }

    plans = plan_shorts(clips, mode=mode, topic=topic)
    if not plans:
        return {
            "ok": True,
            "shorts_written": 0,
            "reason": (
                f"no clips at or above hype_threshold={settings.shorts_hype_threshold}"
                + (f" for topic={topic}" if topic else "")
            ),
            "shorts": [],
        }

    # Resolve music path: explicit param > config default > none
    resolved_music: str | None = None
    if mode == "montage":
        resolved_music = music_path or settings.shorts_default_music_path or None
        if resolved_music and not Path(resolved_music).is_file():
            _log.warning(
                "shorts music path %r not found on disk — proceeding without music",
                resolved_music,
            )
            resolved_music = None

    out_folder = _shorts_folder(asset, mode)
    out_folder.mkdir(parents=True, exist_ok=True)
    # Clean prior renders so re-runs are deterministic on disk
    for old in out_folder.glob("*.mp4"):
        old.unlink(missing_ok=True)

    # Refine loop cap: 0 disables auto-fix entirely (single render + check
    # semantics preserved when config toggle is off).
    max_iter = (
        settings.shorts_max_review_iter
        if settings.shorts_auto_fix_enabled
        else 0
    )

    results: list[RenderResult] = []
    for plan in plans:
        out_path = out_folder / short_filename(plan)
        result = _render_and_refine(
            plan,
            out_path,
            mode=mode,
            asset=asset,
            music_path=resolved_music,
            run_vlm_check=run_vlm_check,
            max_iter=max_iter,
            layout=layout,
            beat_flash=beat_flash,
            beat_flash_source=beat_flash_source,
        )
        results.append(result)

    _write_index_md(
        out_folder,
        asset=asset,
        mode=mode,
        topic=topic,
        music_path=resolved_music,
        results=results,
    )

    written = sum(1 for r in results if r.ok)
    return {
        "ok": True,
        "folder": out_folder.as_posix(),
        "shorts_written": written,
        "shorts_total": len(results),
        "shorts": [
            {
                "file": r.out_path.name,
                "bucket": r.plan.bucket,
                "title": r.plan.title,
                "vo_prompt": r.plan.vo_prompt,
                "clip_count": len(r.plan.clips),
                "priority": r.plan.priority,
                "is_high_priority": (
                    r.plan.bucket == "multikill"
                    and r.plan.priority >= _PRIORITY_MULTIKILL_3PLUS
                ),
                "ok": r.ok,
                "error": r.error,
                "vlm_verdict": r.vlm_verdict,
                "vlm_why": r.vlm_why,
                "refine_iterations": (r.extras or {}).get("refine_iterations", 0),
                "refinements_applied": (r.extras or {}).get(
                    "refinements_applied"
                ) or [],
                "refine_stopped": (r.extras or {}).get("refine_stopped"),
            }
            for r in results
        ],
    }


# Which VLM `CompilationFix.fix` types this pipeline can act on. Other
# fixes (apply_zoom, apply_focus, remove_clip) require multi-clip
# awareness the MVP doesn't have — logged as unapplied when they arrive.
_WINDOW_FIX_TYPES = frozenset(
    {"extend_before", "extend_after", "trim_start", "trim_end"}
)


def _apply_window_fix_to_clip(
    clip: ShortClip,
    fix,
    asset_path: str,
    out_dir: Path,
    iteration: int,
) -> ShortClip | None:
    """Re-cut `clip` from `asset_path` with a shifted window per `fix`,
    write to `out_dir` under a fresh filename. Returns the new ShortClip,
    or None if the fix couldn't be applied (bad seconds, ffmpeg failure).

    Pure of side-effects beyond writing one mp4 file.
    """
    seconds = float(fix.fix_seconds or 0.0)
    new_start = clip.start_seconds
    new_end = clip.end_seconds
    if fix.fix == "extend_before":
        new_start = max(0.0, new_start - seconds)
    elif fix.fix == "extend_after":
        new_end = new_end + seconds
    elif fix.fix == "trim_start":
        new_start = min(new_end - 0.5, new_start + seconds)
    elif fix.fix == "trim_end":
        new_end = max(new_start + 0.5, new_end - seconds)
    else:
        return None

    if new_end - new_start < 0.5:
        _log.info(
            "shorts refine skipped: window %s→%s too small after %s(%.1fs)",
            new_start,
            new_end,
            fix.fix,
            seconds,
        )
        return None

    stem = Path(clip.file).stem
    out_name = f"{stem}_iter{iteration}.mp4"
    new_path = out_dir / out_name
    ok, err = trim_clip(asset_path, str(new_path), new_start, new_end)
    if not ok:
        _log.warning("shorts refine trim_clip failed: %s", err)
        return None

    return ShortClip(
        file=out_name,
        path=new_path,
        event_type=clip.event_type,
        start_seconds=round(new_start, 2),
        end_seconds=round(new_end, 2),
        hype_score=clip.hype_score,
        funny_score=clip.funny_score,
        story_score=clip.story_score,
        reason=clip.reason,
    )


def _resolve_clip_ref(clip_ref: str, clip_count: int) -> int:
    """Map VLM's `clip_ref` (typically '01' / '02' / an M:SS timestamp)
    to a 0-based index into the short's clips. Falls back to 0 when
    the ref doesn't parse — single-clip shorts are the common case."""
    ref = str(clip_ref).strip()
    if ref.isdigit():
        idx = int(ref) - 1
        if 0 <= idx < clip_count:
            return idx
    # M:SS / UUID / other — default to first clip so the fix still applies
    return 0


def _apply_window_fixes(
    clips: tuple[ShortClip, ...],
    fixes: list,
    *,
    asset_path: str,
    out_dir: Path,
    iteration: int,
) -> tuple[tuple[ShortClip, ...], int]:
    """Apply every window-shift fix to its target clip. Returns the new
    tuple of ShortClips + count of fixes actually applied.

    Non-window fixes (apply_zoom, remove_clip, ...) are silently skipped
    at the caller level; this function only sees `_WINDOW_FIX_TYPES`.
    """
    clip_list = list(clips)
    applied = 0
    for fix in fixes:
        if fix.fix not in _WINDOW_FIX_TYPES:
            continue
        idx = _resolve_clip_ref(fix.clip_ref, len(clip_list))
        new_clip = _apply_window_fix_to_clip(
            clip_list[idx], fix, asset_path, out_dir, iteration
        )
        if new_clip is None:
            continue
        clip_list[idx] = new_clip
        applied += 1
    return tuple(clip_list), applied


def _render_and_refine(
    plan: ShortPlan,
    out_path: Path,
    *,
    mode: str,
    asset: dict,
    music_path: str | None,
    run_vlm_check: bool,
    max_iter: int,
    layout: str | None = None,
    beat_flash: bool | None = None,
    beat_flash_source: str | None = None,
) -> RenderResult:
    """Render → VLM whole-comp review → refine loop.

    Loop terminates when any of these hit:
      1. VLM says `is_cohesive: True` (short looks good)
      2. VLM returns no actionable window fixes (verdict stands)
      3. `max_iter` iterations reached (cap hit, verdict recorded)
      4. `run_vlm_check` is False or `settings.vlm_enabled` is False

    Each iteration re-cuts the plan's underlying clips from the SOURCE
    asset with adjusted windows. Refined clip files land in a
    `_refined/` sidecar next to the short output (kept for debugging;
    ~few MB each and only for shorts that actually needed refinement).
    """
    current_plan = plan
    refine_dir = out_path.parent / "_refined"
    music_out = music_path if mode == "montage" else None
    last_verdict: str | None = None
    last_why: str | None = None
    refinements_applied: list[str] = []

    for iteration in range(max_iter + 1):
        ok, err = render_short(
            current_plan,
            out_path,
            mode=mode,
            music_path=music_path,
            layout=layout,
            beat_flash=beat_flash,
            beat_flash_source=beat_flash_source,
        )
        if not ok:
            return RenderResult(
                plan=current_plan,
                out_path=out_path,
                ok=False,
                error=err,
                vlm_verdict=last_verdict,
                vlm_why=last_why,
                music_path=music_out,
                extras={
                    "refine_iterations": iteration,
                    "refinements_applied": refinements_applied,
                },
            )

        if not run_vlm_check or not settings.vlm_enabled:
            return RenderResult(
                plan=current_plan,
                out_path=out_path,
                ok=True,
                music_path=music_out,
                extras={"refine_iterations": iteration},
            )

        review = _run_vlm_review(current_plan, out_path, asset.get("game"))
        if review is None:
            # VLM unreachable / crashed — accept current render
            return RenderResult(
                plan=current_plan,
                out_path=out_path,
                ok=True,
                music_path=music_out,
                extras={
                    "refine_iterations": iteration,
                    "refine_stopped": "vlm_unavailable",
                    "refinements_applied": refinements_applied,
                },
            )

        last_verdict = "pass" if review.is_cohesive else "needs_review"
        last_why = (
            "; ".join(f.issue for f in review.fixes[:2]) if review.fixes else None
        )

        if review.is_cohesive:
            return RenderResult(
                plan=current_plan,
                out_path=out_path,
                ok=True,
                vlm_verdict="pass",
                vlm_why=last_why,
                music_path=music_out,
                extras={
                    "refine_iterations": iteration,
                    "refinements_applied": refinements_applied,
                },
            )

        if iteration >= max_iter:
            return RenderResult(
                plan=current_plan,
                out_path=out_path,
                ok=True,
                vlm_verdict=last_verdict,
                vlm_why=last_why,
                music_path=music_out,
                extras={
                    "refine_iterations": iteration,
                    "refine_stopped": "cap_reached",
                    "refinements_applied": refinements_applied,
                },
            )

        window_fixes = [f for f in review.fixes if f.fix in _WINDOW_FIX_TYPES]
        if not window_fixes:
            return RenderResult(
                plan=current_plan,
                out_path=out_path,
                ok=True,
                vlm_verdict=last_verdict,
                vlm_why=last_why,
                music_path=music_out,
                extras={
                    "refine_iterations": iteration,
                    "refine_stopped": "no_window_fixes",
                    "refinements_applied": refinements_applied,
                    "unapplied_fix_types": sorted({f.fix for f in review.fixes}),
                },
            )

        refine_dir.mkdir(parents=True, exist_ok=True)
        new_clips, applied = _apply_window_fixes(
            current_plan.clips,
            window_fixes,
            asset_path=asset["path"],
            out_dir=refine_dir,
            iteration=iteration + 1,
        )
        if applied == 0:
            # Nothing landed on disk — give up rather than spin
            return RenderResult(
                plan=current_plan,
                out_path=out_path,
                ok=True,
                vlm_verdict=last_verdict,
                vlm_why=last_why,
                music_path=music_out,
                extras={
                    "refine_iterations": iteration,
                    "refine_stopped": "all_fixes_failed",
                    "refinements_applied": refinements_applied,
                },
            )
        refinements_applied.append(
            f"iter{iteration + 1}: "
            + ", ".join(
                f"{f.fix}({f.fix_seconds}s)@{f.clip_ref}" for f in window_fixes
            )
        )
        current_plan = ShortPlan(
            bucket=current_plan.bucket,
            clips=new_clips,
            title=current_plan.title,
            vo_prompt=current_plan.vo_prompt,
            index=current_plan.index,
            priority=current_plan.priority,
        )

    # Shouldn't reach here (loop always returns) — belt & suspenders.
    return RenderResult(
        plan=current_plan, out_path=out_path, ok=True, music_path=music_out
    )


def _run_vlm_review(plan: ShortPlan, short_path: Path, game: str | None):
    """Single VLM whole-comp review call. Returns None on any failure
    so the caller can degrade gracefully."""
    try:
        from .candidates.probe import get_duration_seconds
        from .vlm.validator import validate_compilation

        duration = get_duration_seconds(str(short_path))
        if duration <= 0:
            return None
        return validate_compilation(
            short_path,
            game=game,
            clip_count=len(plan.clips),
            total_duration_seconds=duration,
            n_frames=min(24, max(8, len(plan.clips) * 4)),
        )
    except Exception as exc:
        _log.info("shorts vlm review skipped: %s", exc)
        return None


# ---------------------------------------------------------------------
# Topic preview — for `list_shorts_topics` MCP tool
# ---------------------------------------------------------------------


def preview_buckets(asset: dict, candidates: list[dict]) -> dict:
    """Read the highlights folder for `asset` and return per-bucket
    clip counts. Zero shorts get rendered — this is preview-only."""
    hl_folder = _highlights_folder_for_asset(asset, candidates)
    if hl_folder is None:
        return {
            "ok": False,
            "reason": "highlights folder not found — run analyze_asset first",
            "buckets": {},
        }
    clips = load_clips(hl_folder)
    kept = [c for c in clips if c.hype_score >= settings.shorts_hype_threshold]
    buckets = categorize_clips(kept, first_blood_source=clips)
    return {
        "ok": True,
        "highlights_folder": hl_folder.as_posix(),
        "clip_count_total": len(clips),
        "clip_count_above_threshold": len(kept),
        "hype_threshold": settings.shorts_hype_threshold,
        "buckets": {
            b: {
                "count": len(cs),
                "clips": [
                    {
                        "file": c.file,
                        "event_type": c.event_type,
                        "hype_score": c.hype_score,
                        "anchor_mmss": c.mmss,
                    }
                    for c in sorted(cs, key=lambda x: x.anchor_seconds)
                ],
            }
            for b, cs in buckets.items()
        },
    }


# ---------------------------------------------------------------------
# Cross-asset "best of" montage — sweep every session folder for a game
# ---------------------------------------------------------------------


_SESSION_DATE_PREFIX = re.compile(r"^(\d{2})-(\d{2})-(\d{4})")

_CROSS_ASSET_WINDOWS: dict[str, int | None] = {
    "day": 1,        # today and yesterday
    "week": 7,       # last 7 days
    "all": None,     # no filter
}

_CROSS_ASSET_TITLES: dict[str, str] = {
    "day": "BEST OF TODAY",
    "week": "BEST OF THE WEEK",
    "all": "BEST OF",
}

_CROSS_ASSET_BUCKET = "cross_asset_multikill"


@dataclass(frozen=True)
class SessionRef:
    """One highlights session folder we might pull clips from."""

    folder: Path
    date: date
    game: str


def _parse_session_date(folder_name: str) -> date | None:
    """Extract the `MM-DD-YYYY` prefix from a session folder name.

    Handles both `05-15-2026_19h55` and `05-15-2026_Jhin`. Returns None
    for folders that don't start with a parseable date (they get
    skipped in the scan, not crashed).
    """
    m = _SESSION_DATE_PREFIX.match(folder_name)
    if not m:
        return None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _highlights_root(game: str) -> Path:
    """`WORKSPACE/highlights/<game>/` — same convention as
    `highlights.py`'s `relative_folder`."""
    from .highlights import _safe

    return settings.workspace_dir / "highlights" / _safe(game)


def _scan_sessions(
    game: str, window: str, *, today: date
) -> list[SessionRef]:
    """Walk `WORKSPACE/highlights/<game>/*/`, parse dates, filter by window.

    Windows:
      day  → date >= today - 1d (includes today + yesterday)
      week → date >= today - 7d
      all  → no filter

    Returns sorted by (date DESC, folder.name ASC) so most-recent
    sessions rank first when hype scores tie.
    """
    if window not in _CROSS_ASSET_WINDOWS:
        raise ValueError(
            f"unknown window {window!r}; expected one of "
            f"{sorted(_CROSS_ASSET_WINDOWS)}"
        )
    root = _highlights_root(game)
    if not root.is_dir():
        return []
    days = _CROSS_ASSET_WINDOWS[window]
    cutoff = today - timedelta(days=days) if days is not None else None
    sessions: list[SessionRef] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        d = _parse_session_date(entry.name)
        if d is None:
            continue
        if cutoff is not None and d < cutoff:
            continue
        sessions.append(SessionRef(folder=entry, date=d, game=game))
    sessions.sort(key=lambda s: (-s.date.toordinal(), s.folder.name))
    return sessions


@dataclass(frozen=True)
class _CrossAssetClip:
    """One multikill clip from one session, with the sidecar metadata
    needed to rank + label it in the montage."""

    clip: ShortClip
    session: SessionRef
    group_size: int
    """How many kills in the adjacency cluster this clip belongs to
    (2 = double, 3 = triple, ...). Drives the "HIGH priority" tag."""


def _gather_cross_asset_multikills(
    sessions: list[SessionRef],
) -> list[_CrossAssetClip]:
    """For each session, load clips, categorize, keep multikill bucket
    with per-clip group_size attached. Returns a flat list."""
    out: list[_CrossAssetClip] = []
    for session in sessions:
        clips = load_clips(session.folder)
        if not clips:
            continue
        kept = [c for c in clips if c.hype_score >= settings.shorts_hype_threshold]
        if not kept:
            continue
        # Compute group sizes on the same adjacency algorithm the
        # regular multikill bucket uses. Doing it directly (not via
        # categorize_clips) so we know the size of the group each clip
        # belongs to — the sidecar categorize_clips doesn't expose.
        by_anchor = sorted(kept, key=lambda c: (c.anchor_seconds, c.file))
        kill_ish = [c for c in by_anchor if _is_kill_event(c.event_type)]
        groups = _find_adjacent_indices(kill_ish, _MULTIKILL_ADJACENCY_SECONDS)
        for g in groups:
            if len(g) < 2:
                continue
            for idx in g:
                out.append(
                    _CrossAssetClip(
                        clip=kill_ish[idx],
                        session=session,
                        group_size=len(g),
                    )
                )
    return out


def _rank_cross_asset_clips(
    clips: list[_CrossAssetClip], max_clips: int
) -> list[_CrossAssetClip]:
    """Rank by (group_size DESC, hype_score DESC, session_date DESC,
    file ASC) and cap at `max_clips`. Fully deterministic."""
    ordered = sorted(
        clips,
        key=lambda x: (
            -x.group_size,
            -x.clip.hype_score,
            -x.session.date.toordinal(),
            x.session.folder.name,
            x.clip.file,
        ),
    )
    return ordered[:max_clips]


def _cross_asset_output_folder(game: str, window: str, today: date) -> Path:
    """`WORKSPACE/shorts/_cross_asset/<game>_<window>_<YYYY-MM-DD>/`"""
    from .highlights import _safe

    stem = f"{_safe(game)}_{window}_{today.isoformat()}"
    return settings.workspace_dir / "shorts" / "_cross_asset" / stem


def _write_cross_asset_index_md(
    folder: Path,
    *,
    game: str,
    window: str,
    today: date,
    sessions_scanned: list[SessionRef],
    ranked: list[_CrossAssetClip],
    result: RenderResult | None,
    music_path: str | None,
) -> None:
    """index.md for a cross-asset compile — lists sessions swept,
    ranked candidates, and the render outcome."""
    lines: list[str] = []
    lines.append(f"# Cross-asset best-of for {game} ({window})")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"Today: {today.isoformat()}")
    lines.append(f"Sessions scanned: {len(sessions_scanned)}")
    for s in sessions_scanned:
        lines.append(f"  - {s.date.isoformat()}  {s.folder.name}")
    lines.append("")
    lines.append(f"## Ranked clips ({len(ranked)})")
    for i, r in enumerate(ranked, 1):
        high = " HIGH" if r.group_size >= 3 else ""
        lines.append(
            f"{i:02d}. [{r.group_size}-kill{high}] "
            f"hype={r.clip.hype_score:.2f}  "
            f"{r.session.folder.name}/{r.clip.file}  "
            f"({r.clip.mmss} {r.clip.event_type})"
        )
        if r.clip.reason:
            lines.append(f"    reason: {r.clip.reason}")
    lines.append("")
    if result is not None:
        status = "OK" if result.ok else "FAILED"
        lines.append(f"## Render  [{status}]")
        lines.append(f"Output: {result.out_path.name}")
        if music_path:
            lines.append(f"Music: {music_path}")
        if result.vlm_verdict:
            lines.append(f"VLM: {result.vlm_verdict} — {result.vlm_why or ''}")
        if not result.ok:
            lines.append(f"Error: {result.error}")
    (folder / "index.md").write_text("\n".join(lines), encoding="utf-8")


def preview_cross_asset_montage(
    *,
    game: str,
    window: str = "week",
    today: date | None = None,
    max_clips: int | None = None,
) -> dict:
    """Dry-run: scan + rank cross-asset multikill candidates without
    rendering. Useful before spending 5 min on ffmpeg.

    Pure I/O (folder walk + JSON reads); no ffmpeg.
    """
    resolved_today = today or date.today()
    max_c = (
        settings.shorts_cross_asset_max_clips if max_clips is None else max_clips
    )
    try:
        sessions = _scan_sessions(game, window, today=resolved_today)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "candidates": []}
    all_clips = _gather_cross_asset_multikills(sessions)
    ranked = _rank_cross_asset_clips(all_clips, max_c)
    return {
        "ok": True,
        "game": game,
        "window": window,
        "today": resolved_today.isoformat(),
        "sessions_scanned": [
            {"date": s.date.isoformat(), "folder": s.folder.name}
            for s in sessions
        ],
        "sessions_scanned_count": len(sessions),
        "candidates_total": len(all_clips),
        "max_clips": max_c,
        "candidates": [
            {
                "session": r.session.folder.name,
                "session_date": r.session.date.isoformat(),
                "file": r.clip.file,
                "path": r.clip.path.as_posix(),
                "event_type": r.clip.event_type,
                "hype_score": r.clip.hype_score,
                "anchor_mmss": r.clip.mmss,
                "group_size": r.group_size,
                "is_high_priority": r.group_size >= 3,
                "reason": r.clip.reason,
            }
            for r in ranked
        ],
    }


def build_cross_asset_montage(
    *,
    game: str,
    window: str = "week",
    music_path: str | None = None,
    layout: str | None = None,
    beat_flash: bool | None = None,
    beat_flash_source: str | None = None,
    today: date | None = None,
    max_clips: int | None = None,
    run_vlm_check: bool = True,
) -> dict:
    """Sweep every session folder for `game`, pull the top multikill
    clips inside `window`, and render ONE "best of" montage short.

    Ranking: kill-group size DESC → hype_score DESC → session_date DESC
    → file ASC. Fully deterministic given the folder state on disk.

    Returns a summary dict shaped like `build_shorts`.
    """
    resolved_today = today or date.today()
    max_c = (
        settings.shorts_cross_asset_max_clips if max_clips is None else max_clips
    )

    try:
        sessions = _scan_sessions(game, window, today=resolved_today)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "shorts_written": 0, "shorts": []}

    if not sessions:
        return {
            "ok": True,
            "shorts_written": 0,
            "reason": f"no sessions in window={window} for game={game!r}",
            "sessions_scanned_count": 0,
            "shorts": [],
        }

    all_clips = _gather_cross_asset_multikills(sessions)
    ranked = _rank_cross_asset_clips(all_clips, max_c)
    if not ranked:
        # Write an index.md so the user can see WHICH sessions were
        # scanned (helps debug "why did I get nothing").
        out_folder = _cross_asset_output_folder(game, window, resolved_today)
        out_folder.mkdir(parents=True, exist_ok=True)
        _write_cross_asset_index_md(
            out_folder,
            game=game,
            window=window,
            today=resolved_today,
            sessions_scanned=sessions,
            ranked=[],
            result=None,
            music_path=None,
        )
        return {
            "ok": True,
            "shorts_written": 0,
            "reason": (
                f"no multikill clips (>=2 adjacent kills) in {len(sessions)} "
                f"session(s) for window={window}"
            ),
            "folder": out_folder.as_posix(),
            "sessions_scanned_count": len(sessions),
            "shorts": [],
        }

    # Resolve music path (same rule as build_shorts)
    resolved_music: str | None = (
        music_path or settings.shorts_default_music_path or None
    )
    if resolved_music and not Path(resolved_music).is_file():
        _log.warning(
            "cross-asset music path %r not found — proceeding without music",
            resolved_music,
        )
        resolved_music = None

    # Build the plan. Priority = MULTIKILL_3PLUS if any 3+ group is in it.
    tup = tuple(r.clip for r in ranked)
    max_group = max(r.group_size for r in ranked)
    plan_priority = (
        _PRIORITY_MULTIKILL_3PLUS if max_group >= 3 else _PRIORITY_DEFAULT
    )
    plan = ShortPlan(
        bucket=_CROSS_ASSET_BUCKET,
        clips=tup,
        title=_CROSS_ASSET_TITLES.get(window, "BEST OF"),
        vo_prompt="",
        index=1,
        priority=plan_priority,
    )

    out_folder = _cross_asset_output_folder(game, window, resolved_today)
    out_folder.mkdir(parents=True, exist_ok=True)
    # Clean prior renders so re-runs are deterministic
    for old in out_folder.glob("*.mp4"):
        old.unlink(missing_ok=True)

    high = "HIGH_" if plan_priority >= _PRIORITY_MULTIKILL_3PLUS else ""
    out_name = f"best_of_{high}{window}_{resolved_today.isoformat()}.mp4"
    out_path = out_folder / out_name

    # Synthesize a minimal `asset` dict for the refine loop. It only
    # needs `path` (for re-cutting) and `game` (for VLM prompts). Cross-
    # asset auto-fix would need per-clip asset resolution (different
    # sessions came from different recordings) — force max_iter=0 for
    # now. The whole-comp VLM coherence check still runs.
    fake_asset = {
        "path": ranked[0].session.folder.as_posix(),
        "game": game,
    }
    result = _render_and_refine(
        plan,
        out_path,
        mode="montage",
        asset=fake_asset,
        music_path=resolved_music,
        run_vlm_check=run_vlm_check,
        max_iter=0,
        layout=layout,
        beat_flash=beat_flash,
        beat_flash_source=beat_flash_source,
    )

    _write_cross_asset_index_md(
        out_folder,
        game=game,
        window=window,
        today=resolved_today,
        sessions_scanned=sessions,
        ranked=ranked,
        result=result,
        music_path=resolved_music,
    )

    return {
        "ok": True,
        "folder": out_folder.as_posix(),
        "shorts_written": 1 if result.ok else 0,
        "shorts_total": 1,
        "sessions_scanned_count": len(sessions),
        "candidates_total": len(all_clips),
        "shorts": [
            {
                "file": result.out_path.name,
                "bucket": _CROSS_ASSET_BUCKET,
                "title": plan.title,
                "clip_count": len(plan.clips),
                "priority": plan.priority,
                "is_high_priority": (
                    plan.priority >= _PRIORITY_MULTIKILL_3PLUS
                ),
                "ok": result.ok,
                "error": result.error,
                "vlm_verdict": result.vlm_verdict,
                "vlm_why": result.vlm_why,
                "sources": [
                    {
                        "session": r.session.folder.name,
                        "session_date": r.session.date.isoformat(),
                        "file": r.clip.file,
                        "group_size": r.group_size,
                        "hype_score": r.clip.hype_score,
                    }
                    for r in ranked
                ],
            }
        ],
    }


# ---------------------------------------------------------------------
# Outplayed direct compile — skip DB scan + analyzer; work straight
# off the Outplayed folder for games with clean auto-cut event clips
# (Valorant, no-Riot LoL, etc). Curation is Outplayed's; we just pick
# the freshest N and stitch them.
# ---------------------------------------------------------------------


_OUTPLAYED_BUCKET = "outplayed_best_of"


@dataclass(frozen=True)
class OutplayedClipRef:
    """One Outplayed auto-cut clip on disk, with mtime for ranking."""

    path: Path
    duration_seconds: float
    mtime_epoch: float


def _scan_outplayed_clips(
    game: str,
    *,
    max_duration_seconds: float | None = None,
    size_prefilter_mb: float = 500.0,
) -> list[OutplayedClipRef]:
    """Walk `OUTPLAYED_MEDIA_DIR/<game>/` recursively for `.mp4` files
    that look like Outplayed's auto-cut event clips.

    Two-stage filter (fast then precise):
      1. Size prefilter — files > `size_prefilter_mb` are session VODs,
         not clips. Skip without ffprobe (saves a lot of time on 100+
         file libraries).
      2. Duration check — clips must be > 0 and <=
         `max_duration_seconds` (default: `outplayed_clip_max_seconds`
         from config, currently 120s).

    Returns sorted by mtime DESC (most recent first).
    """
    from .highlights import _safe

    max_d = (
        max_duration_seconds
        if max_duration_seconds is not None
        else settings.outplayed_clip_max_seconds
    )
    root = settings.outplayed_media_dir / _safe(game)
    if not root.is_dir():
        return []

    from .candidates.probe import get_duration_seconds

    max_bytes = int(size_prefilter_mb * 1024 * 1024)
    candidates: list[OutplayedClipRef] = []
    for p in root.rglob("*.mp4"):
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size > max_bytes:
            continue
        try:
            duration = get_duration_seconds(str(p))
        except Exception:
            continue
        if not (0 < duration <= max_d):
            continue
        candidates.append(
            OutplayedClipRef(
                path=p, duration_seconds=duration, mtime_epoch=st.st_mtime
            )
        )
    candidates.sort(key=lambda c: (-c.mtime_epoch, c.path.as_posix()))
    return candidates


def _outplayed_clip_to_short_clip(ref: OutplayedClipRef) -> ShortClip:
    """Wrap an Outplayed clip as a `ShortClip`. Anchor at midpoint;
    hype=1.0 since Outplayed already curated it."""
    return ShortClip(
        file=ref.path.name,
        path=ref.path,
        event_type="outplayed_clip",
        start_seconds=0.0,
        end_seconds=ref.duration_seconds,
        hype_score=1.0,
        funny_score=0.5,
        story_score=0.5,
        reason="Outplayed auto-cut event clip (pre-curated)",
    )


def _outplayed_output_folder(game: str, today: date) -> Path:
    """`WORKSPACE/shorts/_outplayed/<game>_<YYYY-MM-DD>/`"""
    from .highlights import _safe

    stem = f"{_safe(game)}_{today.isoformat()}"
    return settings.workspace_dir / "shorts" / "_outplayed" / stem


def _write_outplayed_index_md(
    folder: Path,
    *,
    game: str,
    today: date,
    scanned: list[OutplayedClipRef],
    picked: list[OutplayedClipRef],
    since_days: int | None,
    result: RenderResult | None,
    music_path: str | None,
) -> None:
    lines: list[str] = []
    lines.append(f"# Outplayed best-of for {game}")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"Today: {today.isoformat()}")
    lines.append(
        f"Since: {'last ' + str(since_days) + ' days' if since_days else 'all-time'}"
    )
    lines.append(f"Clips scanned: {len(scanned)}")
    lines.append("")
    lines.append(f"## Picked clips ({len(picked)})")
    for i, ref in enumerate(picked, 1):
        mtime = datetime.fromtimestamp(ref.mtime_epoch).strftime("%Y-%m-%d %H:%M")
        session = ref.path.parent.name
        lines.append(
            f"{i:02d}. {mtime}  {ref.duration_seconds:.1f}s  "
            f"{session}/{ref.path.name}"
        )
    lines.append("")
    if result is not None:
        status = "OK" if result.ok else "FAILED"
        lines.append(f"## Render  [{status}]")
        lines.append(f"Output: {result.out_path.name}")
        if music_path:
            lines.append(f"Music: {music_path}")
        if result.vlm_verdict:
            lines.append(f"VLM: {result.vlm_verdict} — {result.vlm_why or ''}")
        if not result.ok:
            lines.append(f"Error: {result.error}")
    (folder / "index.md").write_text("\n".join(lines), encoding="utf-8")


def preview_outplayed_montage(
    *,
    game: str,
    limit: int | None = None,
    since_days: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Dry-run: scan Outplayed clips + return what would be picked,
    without rendering. Cheap (ffprobe only; no ffmpeg)."""
    lim = (
        settings.shorts_cross_asset_max_clips if limit is None else limit
    )
    scanned = _scan_outplayed_clips(game)
    filtered = _filter_by_since(scanned, since_days, now)
    picked = filtered[:lim]
    return {
        "ok": True,
        "game": game,
        "since_days": since_days,
        "clips_scanned": len(scanned),
        "clips_after_since_filter": len(filtered),
        "limit": lim,
        "picks": [
            {
                "path": ref.path.as_posix(),
                "session": ref.path.parent.name,
                "duration_seconds": ref.duration_seconds,
                "mtime": datetime.fromtimestamp(
                    ref.mtime_epoch
                ).isoformat(),
            }
            for ref in picked
        ],
    }


def _filter_by_since(
    clips: list[OutplayedClipRef],
    since_days: int | None,
    now: datetime | None,
) -> list[OutplayedClipRef]:
    if since_days is None:
        return clips
    reference = now or datetime.now()
    cutoff = reference.timestamp() - since_days * 86400
    return [c for c in clips if c.mtime_epoch >= cutoff]


def build_outplayed_montage(
    *,
    game: str,
    limit: int | None = None,
    since_days: int | None = None,
    music_path: str | None = None,
    layout: str | None = None,
    beat_flash: bool | None = None,
    beat_flash_source: str | None = None,
    now: datetime | None = None,
    today: date | None = None,
    run_vlm_check: bool = True,
) -> dict:
    """Compile ONE montage directly from Outplayed's auto-cut clips
    for `game`.

    Bypasses the whole analyzer/ranker pipeline. Outplayed already
    curated each clip (every file is a detected event moment) so
    ranking would just be a paid no-op.

    Ordering: most recent first (by file mtime). `since_days`
    optionally restricts to the last N days (e.g. `since_days=1` for
    today's session).

    Output: `WORKSPACE/shorts/_outplayed/<game>_<YYYY-MM-DD>/
    best_of_<YYYY-MM-DD>.mp4` + `index.md`.
    """
    resolved_today = today or date.today()
    lim = (
        settings.shorts_cross_asset_max_clips if limit is None else limit
    )

    scanned = _scan_outplayed_clips(game)
    if not scanned:
        return {
            "ok": True,
            "shorts_written": 0,
            "reason": (
                f"no Outplayed clips (<= {settings.outplayed_clip_max_seconds}s) "
                f"found for game={game!r} at {settings.outplayed_media_dir}"
            ),
            "clips_scanned": 0,
            "shorts": [],
        }

    filtered = _filter_by_since(scanned, since_days, now)
    picked = filtered[:lim]
    if not picked:
        out_folder = _outplayed_output_folder(game, resolved_today)
        out_folder.mkdir(parents=True, exist_ok=True)
        _write_outplayed_index_md(
            out_folder,
            game=game,
            today=resolved_today,
            scanned=scanned,
            picked=[],
            since_days=since_days,
            result=None,
            music_path=None,
        )
        return {
            "ok": True,
            "shorts_written": 0,
            "reason": (
                f"no clips in since_days={since_days} window "
                f"({len(scanned)} scanned total)"
            ),
            "folder": out_folder.as_posix(),
            "clips_scanned": len(scanned),
            "shorts": [],
        }

    # Resolve music (same rule as build_shorts)
    resolved_music: str | None = (
        music_path or settings.shorts_default_music_path or None
    )
    if resolved_music and not Path(resolved_music).is_file():
        _log.warning(
            "outplayed music path %r not found — proceeding without music",
            resolved_music,
        )
        resolved_music = None

    tup = tuple(_outplayed_clip_to_short_clip(ref) for ref in picked)
    plan = ShortPlan(
        bucket=_OUTPLAYED_BUCKET,
        clips=tup,
        title="BEST OF",
        vo_prompt="",
        index=1,
    )

    out_folder = _outplayed_output_folder(game, resolved_today)
    out_folder.mkdir(parents=True, exist_ok=True)
    for old in out_folder.glob("*.mp4"):
        old.unlink(missing_ok=True)

    out_name = f"best_of_{resolved_today.isoformat()}.mp4"
    out_path = out_folder / out_name

    fake_asset = {"path": picked[0].path.as_posix(), "game": game}
    result = _render_and_refine(
        plan,
        out_path,
        mode="montage",
        asset=fake_asset,
        music_path=resolved_music,
        run_vlm_check=run_vlm_check,
        max_iter=0,
        layout=layout,
        beat_flash=beat_flash,
        beat_flash_source=beat_flash_source,
    )

    _write_outplayed_index_md(
        out_folder,
        game=game,
        today=resolved_today,
        scanned=scanned,
        picked=picked,
        since_days=since_days,
        result=result,
        music_path=resolved_music,
    )

    return {
        "ok": True,
        "folder": out_folder.as_posix(),
        "shorts_written": 1 if result.ok else 0,
        "shorts_total": 1,
        "clips_scanned": len(scanned),
        "clips_picked": len(picked),
        "shorts": [
            {
                "file": result.out_path.name,
                "bucket": _OUTPLAYED_BUCKET,
                "title": plan.title,
                "clip_count": len(plan.clips),
                "ok": result.ok,
                "error": result.error,
                "vlm_verdict": result.vlm_verdict,
                "vlm_why": result.vlm_why,
                "sources": [
                    {
                        "path": ref.path.as_posix(),
                        "session": ref.path.parent.name,
                        "duration_seconds": ref.duration_seconds,
                    }
                    for ref in picked
                ],
            }
        ],
    }


def list_outplayed_shorts(game: str | None = None) -> dict:
    """Enumerate Outplayed best-of shorts under
    `WORKSPACE/shorts/_outplayed/`. Read-only."""
    from .highlights import _safe

    root = settings.workspace_dir / "shorts" / "_outplayed"
    if not root.is_dir():
        return {"folders": []}
    prefix = _safe(game) + "_" if game else None
    folders: list[dict] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if prefix and not entry.name.startswith(prefix):
            continue
        shorts = sorted(p.name for p in entry.glob("*.mp4"))
        folders.append(
            {
                "folder": entry.as_posix(),
                "name": entry.name,
                "shorts": shorts,
                "index_md_present": (entry / "index.md").is_file(),
            }
        )
    return {"folders": folders}


def list_cross_asset_shorts(game: str | None = None) -> dict:
    """Enumerate cross-asset shorts on disk under
    `WORKSPACE/shorts/_cross_asset/`. Read-only. Optionally filter by
    game (substring match on the folder prefix)."""
    from .highlights import _safe

    root = settings.workspace_dir / "shorts" / "_cross_asset"
    if not root.is_dir():
        return {"folders": []}
    prefix = _safe(game) + "_" if game else None
    folders: list[dict] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if prefix and not entry.name.startswith(prefix):
            continue
        shorts = sorted(p.name for p in entry.glob("*.mp4"))
        index_md = entry / "index.md"
        folders.append(
            {
                "folder": entry.as_posix(),
                "name": entry.name,
                "shorts": shorts,
                "index_md_present": index_md.is_file(),
            }
        )
    return {"folders": folders}


# `tempfile` is imported for future concat-list file usage; keep to
# avoid ruff removing an import needed by callers under active dev.
_ = tempfile
