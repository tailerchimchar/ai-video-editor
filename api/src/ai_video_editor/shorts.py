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
from datetime import datetime, timezone
from pathlib import Path

from .config import settings
from .editing import trim_clip
from .edits import _escape_drawtext, _escape_fontfile_path, blur_fill_9x16

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
        if mode == "voiceover":
            for c in bucket_clips:
                plans.append(
                    ShortPlan(
                        bucket=bucket,
                        clips=(c,),
                        title=_title_for(bucket, 1),
                        vo_prompt=_vo_prompt_for(bucket),
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
                        )
                    )
        else:
            raise ValueError(f"unknown mode {mode!r}; expected voiceover|montage")

    plans.sort(key=lambda p: (p.clips[0].anchor_seconds, p.clips[0].file))
    return [
        ShortPlan(
            bucket=p.bucket,
            clips=p.clips,
            title=p.title,
            vo_prompt=p.vo_prompt,
            index=i + 1,
        )
        for i, p in enumerate(plans)
    ]


def short_filename(plan: ShortPlan) -> str:
    """Deterministic filename for a plan.

    `short_<NN>_<bucket>_<mmss>.mp4` where mmss is the anchor of the
    first (earliest) clip in the short.
    """
    mmss = plan.clips[0].mmss
    return f"short_{plan.index:02d}_{_bucket_slug(plan.bucket)}_{mmss}.mp4"


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


def _build_filter_complex(
    *,
    n_video_inputs: int,
    title: str,
    mode: str,
    music_input_index: int | None,
    duck_volume: float,
    music_volume: float,
) -> str:
    """Assemble the full `-filter_complex` string.

    Video path:
      For each source video, apply the blur-fill (yields `[vN]`), then
      concat all into `[vall]`, then draw the title.
    Audio path:
      Concat source audio -> `[asrc]`. If VO mode: volume=duck.
      If montage + music present: amix with the music track.

    Result: `[vout][aout]`.
    """
    parts: list[str] = []

    # Per-video blur-fill: label each as [v0], [v1], ...
    for i in range(n_video_inputs):
        # Use the shared blur_fill_9x16 but rewrite [0:v]→[i:v] and
        # [out]→[vi] so we can concat.
        chain = (
            blur_fill_9x16()
            .replace("[0:v]", f"[{i}:v]")
            .replace("[fga]", f"[fga{i}]")
            .replace("[bga]", f"[bga{i}]")
            .replace("[fg]", f"[fg{i}]")
            .replace("[bg]", f"[bg{i}]")
            .replace("[out]", f"[v{i}]")
        )
        parts.append(chain)

    # Concat video streams (video-only concat, `n=N:v=1:a=0`)
    concat_v_ins = "".join(f"[v{i}]" for i in range(n_video_inputs))
    parts.append(f"{concat_v_ins}concat=n={n_video_inputs}:v=1:a=0[vcat]")

    # Title overlay on the concatenated video
    parts.append(f"[vcat]{_title_drawtext(title)}[vout]")

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


def render_short(
    plan: ShortPlan,
    out_path: Path,
    *,
    mode: str,
    music_path: str | None = None,
) -> tuple[bool, str | None]:
    """Render one short from its plan. Returns (ok, error_message_or_none).

    Uses ffmpeg with `-filter_complex`. Re-encodes (H.264 + AAC) because
    the blur-fill filter chain requires it. Bounded stderr keeps the
    error message readable.
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

    filter_complex = _build_filter_complex(
        n_video_inputs=len(plan.clips),
        title=plan.title,
        mode=mode,
        music_input_index=music_input_index,
        duck_volume=duck,
        music_volume=music_vol,
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
            current_plan, out_path, mode=mode, music_path=music_path
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


# `tempfile` is imported for future concat-list file usage; keep to
# avoid ruff removing an import needed by callers under active dev.
_ = tempfile
