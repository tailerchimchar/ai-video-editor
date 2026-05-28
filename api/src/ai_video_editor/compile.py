"""Compile kept ranked highlights into one polished video (Phase 3, B+).

The pipeline is split into two layers so the same machinery supports
both initial compile AND iterative editing:

1. **Build a spec** (`spec_from_rankings`, pure). The spec is a JSON
   describing every clip and its effects — the source of truth.
2. **Render the spec** (`render_spec`, ffmpeg). Per-clip render with
   `effects → captions → fade → aspect tail`, then concat, then optional
   music mix. Supports partial re-renders (`dirty_clip_ids`) so editing
   one clip is fast.

Layout:
    WORKSPACE/compilations/<asset-stem>_<ts>/
        spec.json              <-- mutable source of truth
        compilation.mp4
        index.json             <-- summary of last render
        _parts/part_<id>.mp4   <-- per-clip cached renders
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .compile_cleanup import safe_cleanup_for_render
from .config import settings
from .edits import aspect_filter, caption_filters, resolve_roi
from .thumbnail import (
    cleanup_orphan_clip_thumbnails,
    safe_extract_clip_thumbnails,
    safe_extract_thumbnail,
)


# --- pure: pick + order the clips ---
def plan_clips(
    rankings: list[dict],
    *,
    order: Literal["chronological", "hype", "hook", "narrative"] = "hook",
    limit: int | None = None,
) -> list[dict]:
    """Filter to kept rankings, order, optionally limit. Pure.

    Order modes:
      - ``"hook"`` *(default)* — highest-hype clip FIRST, the rest in
        chronological order. Maximises the first-3-seconds retention
        signal that drives the algorithm.
      - ``"hype"`` — all clips sorted by hype score, descending.
        Best-clip-first but every following clip is also hype-ordered;
        often creates a "peaked early, then trails off" arc.
      - ``"chronological"`` — story order, what happened first plays
        first. Better when context matters more than retention.
      - ``"narrative"`` — three sections in recording order: intro
        (warmup/greeting from first N seconds) → main (chronological
        body) → outro (post-game from last N seconds). Each section
        keeps its top-by-hype, then sorts by time. See
        ``_plan_narrative`` + settings.narrative_*.
    """
    kept = [r for r in rankings if r.get("keep")]
    if not kept:
        return []
    if order == "hype":
        kept.sort(key=lambda r: r.get("hype_score", 0), reverse=True)
    elif order == "hook":
        # Highest-hype first, then chronological for the rest.
        chrono = sorted(kept, key=lambda r: float(r.get("suggested_start_seconds", 0)))
        hottest = max(chrono, key=lambda r: float(r.get("hype_score", 0)))
        chrono.remove(hottest)
        kept = [hottest, *chrono]
    elif order == "narrative":
        return _plan_narrative(kept, limit)
    else:  # chronological
        kept.sort(key=lambda r: float(r.get("suggested_start_seconds", 0)))
    if limit is not None and limit > 0:
        kept = kept[:limit]
    return kept


def _plan_narrative(kept: list[dict], limit: int | None) -> list[dict]:
    """Three-section reel: intro → main → outro, in recording order.

    Pure: derives the recording end from the rankings' max suggested_end
    (works because we only need to know which clips are "near the end").

    - Intro: clips with ``suggested_start < narrative_intro_seconds``.
      Take top ``narrative_intro_max_clips`` by hype_score, time-sorted.
    - Outro: clips with ``suggested_start > rec_end - narrative_outro_seconds``.
      Take top ``narrative_outro_max_clips`` by hype_score, time-sorted.
    - Main: everything else, chronological.

    When ``limit`` is set, intro + outro are preserved and ``limit`` is
    applied to main (so the structural sections always survive).
    """
    intro_window = settings.narrative_intro_seconds
    outro_window = settings.narrative_outro_seconds
    intro_cap = settings.narrative_intro_max_clips
    outro_cap = settings.narrative_outro_max_clips

    def t(r: dict) -> float:
        return float(r.get("suggested_start_seconds", 0))

    def h(r: dict) -> float:
        return float(r.get("hype_score", 0))

    rec_end = max((float(r.get("suggested_end_seconds", 0)) for r in kept), default=0.0)
    outro_threshold = rec_end - outro_window

    # Bucketize. A clip that falls in BOTH (very short recording) is
    # treated as intro to keep the opening; the rest get main/outro.
    intro_pool: list[dict] = []
    outro_pool: list[dict] = []
    main_pool: list[dict] = []
    for r in kept:
        start = t(r)
        if start < intro_window:
            intro_pool.append(r)
        elif start > outro_threshold:
            outro_pool.append(r)
        else:
            main_pool.append(r)

    # Per-section: top-by-hype, then sort chronologically.
    intro = sorted(sorted(intro_pool, key=h, reverse=True)[:intro_cap], key=t)
    outro = sorted(sorted(outro_pool, key=h, reverse=True)[:outro_cap], key=t)
    main = sorted(main_pool, key=t)

    if limit is not None and limit > 0:
        main_budget = max(0, limit - len(intro) - len(outro))
        main = main[:main_budget]

    return [*intro, *main, *outro]


def cluster_ranked_candidates(
    rankings: list[dict],
    candidates: list[dict] | None = None,
    *,
    gap_seconds: float = 30.0,
) -> list[dict]:
    """Merge kept rankings whose windows overlap or sit within `gap_seconds`.

    Post-rank: operates on the ranker's keep/score decisions and produces
    one fused ranking per cluster. The anchor — whose `candidate_id` and
    `reason` carry through — is the `riot_api` source if any is present
    in the cluster (highest-confidence ground truth), else the cluster's
    highest-hype ranking.

    Outputs preserve the ranking dict shape so `plan_clips` /
    `spec_from_rankings` consume them unchanged. Rejected rankings
    (`keep=False`) are dropped — they were never going to make the reel.
    `gap_seconds <= 0` disables clustering (kept items pass through).
    """
    by_id = {c["id"]: c for c in (candidates or []) if c.get("id")}

    def _with_event_type(r: dict) -> dict:
        """Copy `event_type` from the candidate onto the ranking row so
        downstream code (spec_from_rankings → per-event window override)
        doesn't need to re-look-up the candidate. Idempotent: if the
        ranking already has it, keep it."""
        out = dict(r)
        if "event_type" not in out:
            c = by_id.get(out.get("candidate_id"))
            if c and c.get("event_type"):
                out["event_type"] = c["event_type"]
        return out

    if gap_seconds <= 0:
        return [_with_event_type(r) for r in rankings if r.get("keep")]

    kept = sorted(
        (r for r in rankings if r.get("keep")),
        key=lambda r: float(r.get("suggested_start_seconds", 0)),
    )
    if not kept:
        return []

    clusters: list[list[dict]] = [[kept[0]]]
    for r in kept[1:]:
        prev_end = max(float(x.get("suggested_end_seconds", 0)) for x in clusters[-1])
        cur_start = float(r.get("suggested_start_seconds", 0))
        if cur_start - prev_end <= gap_seconds:
            clusters[-1].append(r)
        else:
            clusters.append([r])

    merged: list[dict] = []
    for cluster in clusters:
        if len(cluster) == 1:
            merged.append(_with_event_type(cluster[0]))
            continue

        # Anchor: prefer the highest-confidence riot_api kill in the
        # cluster (ground truth); fall back to the highest-hype member.
        anchor = None
        best_riot_conf = -1.0
        for r in cluster:
            c = by_id.get(r.get("candidate_id"))
            if c and c.get("source") == "riot_api":
                conf = float(c.get("confidence") or 0)
                if conf > best_riot_conf:
                    best_riot_conf = conf
                    anchor = r
        if anchor is None:
            anchor = max(cluster, key=lambda r: float(r.get("hype_score", 0)))

        start = min(float(r.get("suggested_start_seconds", 0)) for r in cluster)
        end = max(float(r.get("suggested_end_seconds", 0)) for r in cluster)
        merged.append(
            {
                **_with_event_type(anchor),
                "candidate_id": anchor["candidate_id"],
                "keep": True,
                "suggested_start_seconds": round(start, 2),
                "suggested_end_seconds": round(end, 2),
                "funny_score": max(float(r.get("funny_score", 0)) for r in cluster),
                "hype_score": max(float(r.get("hype_score", 0)) for r in cluster),
                "story_score": max(float(r.get("story_score", 0)) for r in cluster),
                "reason": f"[merged {len(cluster)}x] {anchor.get('reason', '')}".strip(),
            }
        )

    return merged


def _mmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}m{s % 60:02d}s"


def _segments_in_window(segments: list[dict], start: float, end: float) -> list[dict]:
    return [
        s
        for s in segments
        if float(s.get("end_seconds", 0)) >= start and float(s.get("start_seconds", 0)) <= end
    ]


def _run(cmd: list[str]) -> tuple[bool, str | None]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return False, "ffmpeg not found"
    if r.returncode != 0:
        # ffmpeg writes its ~2KB banner FIRST and the real error LAST,
        # so we keep the tail rather than truncate at the head.
        return False, r.stderr[-1500:]
    return True, None


def _label_filter(number: int) -> str:
    """drawtext for a top-left '#NN' label — shown during iteration so
    you can say 'zoom #04' instead of tracking reel timestamps."""
    from .edits import _escape_fontfile_path  # local import to avoid cycle

    ff = _escape_fontfile_path(settings.caption_font_path)
    return (
        "drawtext="
        f"fontfile='{ff}'"
        f":text='#{number:02d}'"
        ":fontsize=72:fontcolor=yellow"
        ":borderw=6:bordercolor=black"
        ":x=30:y=30"
    )


def _build_clip_filterchain(
    clip: dict, aspect: str, fade_seconds: float, label_number: int | None = None
) -> str:
    """Compose the per-clip ffmpeg filtergraph.

    Order matters: **effects → captions → fade → aspect tail → label.**
    Zoom crops the gameplay first; captions are drawn at displayed size
    on top of the zoomed content; the fade applies to the visible frame;
    aspect-conversion (9:16) is last so layout sees the final canvas;
    the iteration label is painted on top so it survives everything.
    """
    duration = max(0.0, clip["end_seconds"] - clip["start_seconds"])
    chain: list[str] = []

    # 1. Effects (zoom, focus). 'caption' effects are merged in step 2.
    for eff in clip.get("effects", []) or []:
        kind = eff.get("kind")
        if kind == "zoom":
            w, h, x, y = resolve_roi(eff.get("roi", "center"), eff.get("factor", 2.0))
            chain.append(f"crop={w}:{h}:{x}:{y}")
            chain.append("scale=iw*2:ih*2")  # upscale post-crop
        elif kind == "focus":
            cx = f"iw*{eff.get('x', 0.5)}"
            cy = f"ih*{eff.get('y', 0.5)}"
            rr = f"(min(iw,ih)*{eff.get('radius', 0.2)})"
            dim = eff.get("dim", 0.3)
            mask = f"if(lt(hypot(X-{cx},Y-{cy}),{rr}),lum(X,Y),lum(X,Y)*{dim})"
            chain.append(f"geq=lum='{mask}':cb=cb:cr=cr")

    # 2. Captions — ONE unified renderer reads each segment's optional
    # `style` field (preset + overrides). Segments with no style use the
    # default visual. "TikTok mode" is now just a data shape: many
    # short word-segments tagged with `style.preset = "tiktok"`.
    #
    # Legacy migration: comps with `caption_mode == "tiktok"` predate the
    # new model — explode + style at READ TIME so they keep rendering the
    # same way without touching the persisted spec.
    segs = list(clip.get("caption_segments", []) or [])
    legacy_tiktok = (
        clip.get("caption_mode", "").lower() == "tiktok"
        and not any(s.get("style") for s in segs)
    )
    if legacy_tiktok:
        from .edits import explode_segments_to_words

        segs_to_render = explode_segments_to_words(segs, style_preset="tiktok")
    else:
        segs_to_render = segs

    overlay_segs = []
    for eff in clip.get("effects", []) or []:
        if eff.get("kind") == "caption" and eff.get("text"):
            overlay_segs.append(
                {
                    "start_seconds": clip["start_seconds"],
                    "end_seconds": clip["end_seconds"],
                    "text": eff["text"],
                }
            )
    cap = caption_filters(segs_to_render, clip_start_offset=clip["start_seconds"])
    if cap:
        chain.append(cap)
    if overlay_segs:
        # Overlay captions always render segment-style regardless of mode.
        ov = caption_filters(overlay_segs, clip_start_offset=clip["start_seconds"])
        if ov:
            chain.append(ov)

    # 3. Fades (in/out)
    if fade_seconds > 0:
        chain.append(f"fade=t=in:st=0:d={fade_seconds:.2f}")
        chain.append(f"fade=t=out:st={max(0.0, duration - fade_seconds):.2f}:d={fade_seconds:.2f}")

    # 4. Aspect tail (9:16 only)
    tail = aspect_filter(aspect)
    if tail:
        chain.append(tail)

    # 5. Iteration label (last — sits on top of everything, including
    # the aspect-cropped canvas so it stays in the corner in vertical).
    if label_number is not None:
        chain.append(_label_filter(label_number))

    return ",".join(p for p in chain if p) or "null"


def _render_clip_part(
    clip: dict,
    parts_dir: Path,
    aspect: str,
    fade_seconds: float,
    label_number: int | None = None,
) -> tuple[Path, bool, str | None]:
    """Encode one clip from the spec to a part file named by clip-id.

    The part filename embeds whether labels were on, so toggling
    `show_clip_numbers` produces a different cached part and we don't
    serve a stale labeled/unlabeled version.
    """
    parts_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_n{label_number}" if label_number is not None else ""
    out = parts_dir / f"part_{clip['id'][:8]}{suffix}.mp4"
    duration = max(0.0, clip["end_seconds"] - clip["start_seconds"])
    codec_opts = settings.ffmpeg_video_codec_opts.split()
    cmd = [
        settings.ffmpeg_path,
        "-y",
        # Tolerate OBS-source B-frame quirks at seek points.
        "-fflags",
        "+discardcorrupt",
        "-err_detect",
        "ignore_err",
        "-ss",
        str(clip["start_seconds"]),
        "-i",
        clip["asset_path"],
        "-t",
        str(duration),
        "-vf",
        _build_clip_filterchain(clip, aspect, fade_seconds, label_number),
        "-c:v",
        settings.ffmpeg_video_codec,
        *codec_opts,
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        out.as_posix(),
    ]
    ok, err = _run(cmd)
    return out, ok, err


def _concat(parts: list[str], out_path: str) -> tuple[bool, str | None]:
    """ffmpeg concat demuxer: stream-copy join (no re-encode, fast).
    Requires identical codecs/timebases — guaranteed by _render_part."""
    if not parts:
        return False, "no parts to concat"
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt", encoding="utf-8") as f:
        for p in parts:
            # ffmpeg concat is picky about quoting; backslashes -> forward slashes
            f.write(f"file '{Path(p).as_posix()}'\n")
        list_path = f.name
    try:
        cmd = [
            settings.ffmpeg_path,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            list_path,
            "-c",
            "copy",
            out_path,
        ]
        return _run(cmd)
    finally:
        Path(list_path).unlink(missing_ok=True)


def _mix_music(
    video_path: str, music_path: str, out_path: str, music_volume: float
) -> tuple[bool, str | None]:
    """Layer background music under the source audio (default vol 0.25).
    Both streams are mixed; output duration matches the video."""
    cmd = [
        settings.ffmpeg_path,
        "-y",
        "-i",
        video_path,
        "-i",
        music_path,
        "-filter_complex",
        f"[1:a]volume={music_volume:.2f}[m];"
        f"[0:a][m]amix=inputs=2:duration=first:dropout_transition=0[aout]",
        "-map",
        "0:v",
        "-map",
        "[aout]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        out_path,
    ]
    return _run(cmd)


def spec_from_rankings(
    asset: dict,
    rankings: list[dict],
    segments: list[dict],
    *,
    aspect: str = "16:9",
    order: Literal["chronological", "hype", "hook", "narrative"] = "hook",
    limit: int | None = None,
    fade_seconds: float = 0.3,
    music_path: str | None = None,
    music_volume: float = 0.25,
    show_clip_numbers: bool = False,
) -> dict:
    """Build a compilation spec from this asset's kept rankings — pure.

    The spec is the source of truth for the compilation: every clip has
    a stable UUID, its source range, its caption segments, and a
    mutable `effects` list. `render_spec` turns this into video.
    """
    plan = plan_clips(rankings, order=order, limit=limit)
    clips: list[dict] = []
    for r in plan:
        event_type = (r.get("event_type") or "clip").replace("/", "-")
        raw_start = float(r["suggested_start_seconds"])
        raw_end = float(r["suggested_end_seconds"])
        # Per-event-type widening: matches highlights._window_fallback so
        # the compile pipeline gets the same breathing room as cut_highlights.
        # Falls back to no padding for unknown event types (preserves prior
        # behavior). Start is clamped to >= 0; end is left unclamped — ffmpeg
        # tolerates an end past EOF (just yields a shorter clip).
        override = settings.event_window_overrides.get(event_type)
        if override:
            pre, post = override
            raw_start = max(0.0, raw_start - pre)
            raw_end = raw_end + post
        start = round(raw_start, 2)
        end = round(raw_end, 2)
        clips.append(
            {
                "id": str(uuid.uuid4()),
                "asset_id": asset.get("id"),
                "asset_path": asset["path"],
                "asset_filename": asset.get("filename"),
                "start_seconds": start,
                "end_seconds": end,
                "event_type": event_type,
                "caption_segments": _segments_in_window(segments, start, end),
                "effects": [],
                # Carried onto the clip so downstream tools (thumbnail
                # extractor, future planner) can pick the hottest clip
                # without re-loading rankings.json.
                "hype_score": float(r.get("hype_score", 0)),
                # "segment" = bottom-center one-line-per-Whisper-segment (default).
                # Flip to "tiktok" via set_caption_mode for word-by-word karaoke
                # captions on a specific clip.
                "caption_mode": "segment",
            }
        )
    return {
        "id": str(uuid.uuid4()),
        "asset_id": asset.get("id"),
        "asset_filename": asset.get("filename"),
        "aspect": aspect,
        "fade_seconds": fade_seconds,
        "music_path": music_path,
        "music_volume": music_volume,
        # When True, every clip is rendered with a corner "#NN" label.
        # Lets the caller say "zoom #04" by position. Toggle off when
        # finalizing — see `set_clip_numbers` / the labels endpoint.
        "show_clip_numbers": bool(show_clip_numbers),
        "clips": clips,
    }


def render_spec(
    spec: dict,
    folder: Path,
    dirty_clip_ids: set[str] | None = None,
    *,
    cleanup: bool = True,
) -> dict:
    """Render (or re-render) a compilation spec into `folder/compilation.mp4`.

    Per-clip parts are cached by clip-id. Passing `dirty_clip_ids` skips
    re-encoding clips not in the set whose part file already exists —
    that's what makes iterative editing fast (one clip re-encoded, the
    others just re-concatenated as stream-copy).

    `cleanup=True` (default) prunes orphan cached parts from
    `_parts/` after a successful concat — keeps the workspace from
    growing as edits remove/insert clips. Tests that inspect raw
    cache state pass `cleanup=False`.
    """
    folder = Path(folder)
    parts_dir = folder / "_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)

    aspect = spec.get("aspect", "16:9")
    fade_seconds = float(spec.get("fade_seconds", 0.3))
    show_numbers = bool(spec.get("show_clip_numbers", False))
    parts: list[dict] = []
    for idx, clip in enumerate(spec["clips"], start=1):
        # Cached part filename embeds the label state — toggling labels
        # picks a different cache entry rather than serving a stale file.
        label_n = idx if show_numbers else None
        suffix = f"_n{label_n}" if label_n is not None else ""
        part_path = parts_dir / f"part_{clip['id'][:8]}{suffix}.mp4"
        needs_render = (
            dirty_clip_ids is None or clip["id"] in dirty_clip_ids or not part_path.exists()
        )
        if needs_render:
            _, ok, err = _render_clip_part(
                clip, parts_dir, aspect, fade_seconds, label_number=label_n
            )
        else:
            ok, err = True, None
        parts.append(
            {
                "clip_id": clip["id"],
                "file": part_path.name,
                "start_seconds": clip["start_seconds"],
                "end_seconds": clip["end_seconds"],
                "event": clip.get("event_type", "clip"),
                "caption_segments": len(clip.get("caption_segments") or []),
                "effects": list(clip.get("effects") or []),
                "label": f"#{label_n:02d}" if label_n is not None else None,
                "ok": ok,
                "error": err,
            }
        )

    successes = [(parts_dir / p["file"]).as_posix() for p in parts if p["ok"]]
    final_path = folder / "compilation.mp4"
    concat_ok, concat_err = (False, "no parts rendered successfully")
    music_used = None
    if successes:
        if spec.get("music_path"):
            intermediate = folder / "concat_audio_only.mp4"
            concat_ok, concat_err = _concat(successes, intermediate.as_posix())
            if concat_ok:
                concat_ok, concat_err = _mix_music(
                    intermediate.as_posix(),
                    spec["music_path"],
                    final_path.as_posix(),
                    float(spec.get("music_volume", 0.25)),
                )
                music_used = spec["music_path"]
                intermediate.unlink(missing_ok=True)
        else:
            concat_ok, concat_err = _concat(successes, final_path.as_posix())

    # Prune orphans only after a successful concat — a failed render
    # might be retried with the same parts. Cleanup is best-effort:
    # `safe_cleanup_for_render` swallows all errors so a permission
    # blip on one stale file can't undo a finished render.
    cleanup_summary: dict | None = None
    thumbnail_summary: dict | None = None
    clip_thumbnails_summary: dict | None = None
    if cleanup and concat_ok:
        cleanup_summary = safe_cleanup_for_render(folder, spec)
        # Sweep orphan per-clip thumbnails alongside orphan parts so the
        # filmstrip in the webapp doesn't show stale tiles after removes.
        import contextlib

        with contextlib.suppress(Exception):
            cleanup_orphan_clip_thumbnails(folder, spec)
    if concat_ok:
        # Extract a thumbnail from the freshly-rendered reel. Best-effort:
        # a failure here never breaks the render. Skipped on failed
        # renders since there's no video to seek into.
        thumbnail_summary = safe_extract_thumbnail(folder, spec, final_path)
        # Per-clip thumbnails for the webapp's filmstrip — only generated
        # for clips whose tile doesn't already have one, so this is cheap
        # on incremental edits (just the newly-added or never-thumbnailed
        # clips re-extract).
        clip_thumbnails_summary = safe_extract_clip_thumbnails(folder, spec)

    return {
        "output": final_path.as_posix() if concat_ok else None,
        "aspect": aspect,
        "fade_seconds": fade_seconds,
        "music": music_used,
        "music_volume": spec.get("music_volume") if music_used else None,
        "kept_total": len(spec["clips"]),
        "parts_rendered": sum(1 for p in parts if p["ok"]),
        "parts": parts,
        "compiled": concat_ok,
        "error": concat_err if not concat_ok else None,
        "cleanup": cleanup_summary,
        "thumbnail": thumbnail_summary,
        "clip_thumbnails": clip_thumbnails_summary,
    }


def build_compilation(
    asset: dict,
    rankings: list[dict],
    segments: list[dict],
    *,
    candidates: list[dict] | None = None,
    cluster_gap_seconds: float | None = None,
    aspect: str = "16:9",
    order: Literal["chronological", "hype", "hook", "narrative"] = "hook",
    limit: int | None = None,
    fade_seconds: float = 0.3,
    music_path: str | None = None,
    music_volume: float = 0.25,
    show_clip_numbers: bool = True,
) -> dict:
    """Initial compile: build the spec, persist it, render it fresh.

    `show_clip_numbers` defaults to True so the first render is labeled
    for iteration ('zoom #04'). Flip it off via the labels endpoint
    when you're ready to finalize the reel.

    Kept rankings whose windows fall within `cluster_gap_seconds` (default
    `settings.cluster_gap_seconds`) are fused into one clip per cluster
    before planning — kills the "10 short clips of one teamfight" problem.
    Pass `candidates` so the cluster anchor can prefer `riot_api` kills.
    """
    gap = settings.cluster_gap_seconds if cluster_gap_seconds is None else cluster_gap_seconds
    rankings = cluster_ranked_candidates(rankings, candidates, gap_seconds=gap)

    spec = spec_from_rankings(
        asset,
        rankings,
        segments,
        aspect=aspect,
        order=order,
        limit=limit,
        fade_seconds=fade_seconds,
        music_path=music_path,
        music_volume=music_volume,
        show_clip_numbers=show_clip_numbers,
    )

    asset_stem = Path(asset["filename"]).stem
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    folder = settings.workspace_dir / "compilations" / f"{asset_stem}_{ts}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "spec.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")

    summary = render_spec(spec, folder)
    summary.update(
        {
            "spec_id": spec["id"],
            "asset_id": asset.get("id"),
            "source_recording": asset.get("filename"),
            "folder": folder.as_posix(),
            "spec_path": (folder / "spec.json").as_posix(),
            "order": order,
        }
    )
    (folder / "index.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


# --- spec persistence & iterative-editing helpers (pure unless noted) ---


def load_spec(folder: Path) -> dict:
    """Read the spec.json next to a rendered compilation."""
    return json.loads((Path(folder) / "spec.json").read_text(encoding="utf-8"))


def save_spec(folder: Path, spec: dict) -> None:
    """Persist a mutated spec back to disk."""
    (Path(folder) / "spec.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")


def reel_positions(spec: dict) -> list[tuple[float, float]]:
    """Cumulative reel timestamps for each clip (running sum of durations).

    Returns [(reel_start, reel_end), …] aligned with `spec['clips']`.
    Pure; used by `resolve_clip_ref` to map "the clip at 0:32" to an
    index without re-reading the rendered .mp4.
    """
    out: list[tuple[float, float]] = []
    t = 0.0
    for c in spec.get("clips", []):
        dur = max(0.0, float(c["end_seconds"]) - float(c["start_seconds"]))
        out.append((t, t + dur))
        t += dur
    return out


def _parse_time(s: str) -> float | None:
    """Parse 'M:SS' / 'H:MM:SS' / plain seconds. None if unparseable."""
    s = s.strip()
    if not s:
        return None
    if ":" in s:
        parts = s.split(":")
        if not all(p.replace(".", "", 1).isdigit() for p in parts):
            return None
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        return None
    try:
        return float(s)
    except ValueError:
        return None


def resolve_clip_ref(spec: dict, ref: str | int) -> int:
    """Map a clip reference to its 0-based index in spec['clips'].

    Accepts (in priority order):
      - an int or numeric string → 1-based clip index (`"2"` is clip #2)
      - a UUID prefix matching a clip's id
      - a "M:SS" or seconds value → first try REEL time, then SOURCE time

    Raises KeyError when nothing matches. The caller should surface that
    as a 400/404 so the user can re-phrase.
    """
    clips = spec.get("clips") or []
    if not clips:
        raise KeyError("compilation has no clips")

    # 1) int / pure-numeric string → 1-based index
    if isinstance(ref, int):
        idx = ref - 1
        if 0 <= idx < len(clips):
            return idx
        raise KeyError(f"clip index {ref} out of range 1..{len(clips)}")
    s = str(ref).strip()
    if s.isdigit():
        idx = int(s) - 1
        if 0 <= idx < len(clips):
            return idx
        raise KeyError(f"clip index {ref} out of range 1..{len(clips)}")

    # 2) UUID prefix
    for i, c in enumerate(clips):
        if c.get("id", "").startswith(s) and len(s) >= 4:
            return i

    # 3) time → reel first, then source
    t = _parse_time(s)
    if t is not None:
        for i, (rs, re_) in enumerate(reel_positions(spec)):
            if rs <= t < re_:
                return i
        for i, c in enumerate(clips):
            if float(c["start_seconds"]) <= t < float(c["end_seconds"]):
                return i

    raise KeyError(f"no clip matches ref {ref!r}")


# --- pure spec mutators (return new spec + the set of dirty clip ids) ---


def add_effect(spec: dict, clip_idx: int, effect: dict) -> tuple[dict, set[str]]:
    """Append an effect to a clip's effects list. Returns the dirty set."""
    spec = json.loads(json.dumps(spec))  # deep-copy so caller's spec stays untouched
    clip = spec["clips"][clip_idx]
    clip.setdefault("effects", []).append(effect)
    return spec, {clip["id"]}


def extend_clip(spec: dict, clip_idx: int, before: float, after: float) -> tuple[dict, set[str]]:
    """Grow a clip's source window. Caller should clamp to asset duration."""
    spec = json.loads(json.dumps(spec))
    c = spec["clips"][clip_idx]
    c["start_seconds"] = round(max(0.0, float(c["start_seconds"]) - float(before)), 2)
    c["end_seconds"] = round(float(c["end_seconds"]) + float(after), 2)
    return spec, {c["id"]}


def remove_clip(spec: dict, clip_idx: int) -> tuple[dict, set[str]]:
    """Drop a clip from the reel. No clips become dirty (we just re-concat)."""
    spec = json.loads(json.dumps(spec))
    del spec["clips"][clip_idx]
    return spec, set()


def insert_clip(
    spec: dict,
    *,
    asset_id: str,
    asset_path: str,
    asset_filename: str | None,
    start_seconds: float,
    end_seconds: float,
    caption_segments: list[dict] | None = None,
    event_type: str = "manual",
    position: int | None = None,
) -> tuple[dict, set[str]]:
    """Add a brand-new clip from an arbitrary source range into the reel.

    `position` is 1-based; omit to insert chronologically by source time
    within the same asset (the common case — one recording per reel).
    Mixed-asset reels with `position=None` fall through to appending at
    the end (the user should pass an explicit position there).

    The returned dirty set contains just the new clip's id. Clips after
    the insertion point that *would* shift their label number when
    `show_clip_numbers` is on are re-rendered automatically — their
    cached part files use a label-aware filename suffix, so the new
    label index simply misses the cache and triggers a re-render.
    """
    spec = json.loads(json.dumps(spec))
    clip = {
        "id": str(uuid.uuid4()),
        "asset_id": asset_id,
        "asset_path": asset_path,
        "asset_filename": asset_filename,
        "start_seconds": round(float(start_seconds), 2),
        "end_seconds": round(float(end_seconds), 2),
        "event_type": event_type or "manual",
        "caption_segments": list(caption_segments or []),
        "effects": [],
        "caption_mode": "segment",
    }
    clips = spec["clips"]
    if position is not None:
        idx = max(1, min(int(position), len(clips) + 1)) - 1
    else:
        idx = len(clips)
        for i, c in enumerate(clips):
            if c.get("asset_id") == asset_id and float(c["start_seconds"]) > float(start_seconds):
                idx = i
                break
    clips.insert(idx, clip)
    return spec, {clip["id"]}


def set_intro_clip(
    spec: dict,
    *,
    intro_name: str,
    intro_path: str,
    duration: float,
) -> tuple[dict, set[str]]:
    """Prepend (or replace) a branded intro clip at position 1.

    Intros are *not* sourced from an indexed asset — they're pre-rendered
    mp4s living under `WORKSPACE/intros/<name>/`. We carry the full
    `intro_path` directly on the clip and set `asset_id=None`, plus
    `event_type="intro"` so this clip is distinguishable from regular
    rangewise clips.

    Behavior:
      - If a clip with `event_type=="intro"` already exists at position 0,
        it's REPLACED (same idx, new id) — applying a different intro
        doesn't double-stack.
      - Otherwise the new intro is inserted at index 0.

    Returns the new spec + dirty set containing the new intro's id so
    only that one clip re-renders. Source recordings stay untouched —
    `_render_clip_part` only reads `clip["asset_path"]`, not `asset_id`.
    """
    spec = json.loads(json.dumps(spec))
    clip = {
        "id": str(uuid.uuid4()),
        "asset_id": None,
        "asset_path": intro_path,
        "asset_filename": f"intro:{intro_name}",
        "start_seconds": 0.0,
        "end_seconds": round(float(duration), 2),
        "event_type": "intro",
        "caption_segments": [],
        "effects": [],
        "caption_mode": "segment",
        # Carry the source intro name so a later "what intro is on this
        # reel?" lookup doesn't need to introspect asset_filename.
        "intro_name": intro_name,
    }
    clips = spec.setdefault("clips", [])
    if clips and clips[0].get("event_type") == "intro":
        clips[0] = clip
    else:
        clips.insert(0, clip)
    return spec, {clip["id"]}


def insert_intro_at_position(
    spec: dict,
    *,
    intro_name: str,
    intro_path: str,
    duration: float,
    position: int,
) -> tuple[dict, set[str]]:
    """Insert a branded intro at an arbitrary 1-based reel position.

    Companion to `set_intro_clip` for the *prepend* case. The
    difference:
      - `set_intro_clip` targets position 1 and REPLACES any existing
        intro there (you can't stack two openers).
      - `insert_intro_at_position` adds a NEW intro clip at any
        position WITHOUT replacing — use for chapter cards or
        transitions between gameplay segments.

    `position` is clamped to `[1, len(clips) + 1]` so callers that
    pass an out-of-range value still get a sensible result instead
    of an exception.

    Returns the new spec + dirty set containing the new intro's id.
    """
    spec = json.loads(json.dumps(spec))
    clip = {
        "id": str(uuid.uuid4()),
        "asset_id": None,
        "asset_path": intro_path,
        "asset_filename": f"intro:{intro_name}",
        "start_seconds": 0.0,
        "end_seconds": round(float(duration), 2),
        "event_type": "intro",
        "caption_segments": [],
        "effects": [],
        "caption_mode": "segment",
        "intro_name": intro_name,
    }
    clips = spec.setdefault("clips", [])
    idx = max(1, min(int(position), len(clips) + 1)) - 1
    clips.insert(idx, clip)
    return spec, {clip["id"]}


def clear_intro_clip(spec: dict) -> tuple[dict, set[str]]:
    """Remove the intro clip if one is present. No-op otherwise.

    Returns the new spec + empty dirty set (concat-only re-stitch)."""
    spec = json.loads(json.dumps(spec))
    clips = spec.get("clips") or []
    if clips and clips[0].get("event_type") == "intro":
        del clips[0]
    return spec, set()


_VALID_REORDER_MODES = ("chronological", "hype", "hook", "funny", "story")


def reorder_clips_explicit(spec: dict, clip_ids: list[str]) -> tuple[dict, set[str]]:
    """Reorder clips to match an EXPLICIT list of clip ids.

    Unlike `reorder_clips` (which takes a sort MODE and preserves intro
    positions), this mutator is for user-driven drag-and-drop where the
    user picked the exact order including intro placement.

    Validation:
      - Every existing clip's id MUST appear exactly once in `clip_ids`.
      - Any unknown id raises ValueError.
      - Missing ids raise ValueError.

    Returns the new spec + ALL clip ids as dirty (label re-encode if
    show_clip_numbers is on; cached parts otherwise reused since the
    per-clip filterchain hasn't changed).
    """
    spec = json.loads(json.dumps(spec))
    clips = spec.get("clips") or []
    if not clips:
        return spec, set()

    # Check duplicates BEFORE set-based set comparison — duplicates would
    # otherwise look like "missing" because the set collapses them.
    if len(clip_ids) != len(set(clip_ids)):
        raise ValueError("reorder_clips_explicit: duplicate ids in requested order")
    by_id = {c["id"]: c for c in clips if c.get("id")}
    existing = set(by_id.keys())
    requested = set(clip_ids)
    if existing != requested:
        missing = existing - requested
        extra = requested - existing
        parts = []
        if missing:
            parts.append(f"missing: {sorted(missing)}")
        if extra:
            parts.append(f"unknown: {sorted(extra)}")
        raise ValueError(
            f"reorder_clips_explicit: id sets don't match. {' '.join(parts)}"
        )

    spec["clips"] = [by_id[cid] for cid in clip_ids]
    return spec, set(clip_ids)


def reorder_clips(spec: dict, mode: str) -> tuple[dict, set[str]]:
    """Reorder NON-INTRO clips by a scoring mode. Intros stay put.

    Why intros stay put: chapter-card intros at position N are
    positional markers, not gameplay clips. Sorting them by hype
    would scatter them across the reel. Treat them as fixed slots
    and reorder only what flows between them.

    Modes:
      - ``chronological`` — by `start_seconds` ascending (story order)
      - ``hype`` — by `hype_score` descending
      - ``funny`` — by `funny_score` descending
      - ``story`` — by `story_score` descending
      - ``hook`` — highest hype FIRST, rest chronological after

    Returns the new spec + ALL non-intro clip ids as dirty. Although
    the per-clip filterchain doesn't change (cached parts stay valid),
    if `show_clip_numbers` is on, every clip's #NN label is now
    different because positions shifted. Marking dirty forces label
    re-encoding so the numbers match the new order. If labels are off,
    cached parts will be reused — only concat order changes.
    """
    if mode not in _VALID_REORDER_MODES:
        raise ValueError(
            f"unknown reorder mode {mode!r}; expected one of {_VALID_REORDER_MODES}"
        )

    spec = json.loads(json.dumps(spec))
    clips = spec.get("clips") or []
    if not clips:
        return spec, set()

    # Split into intros (kept in place) and gameplay (to be sorted).
    intro_positions: dict[int, dict] = {}
    gameplay: list[dict] = []
    for idx, clip in enumerate(clips):
        if clip.get("event_type") == "intro":
            intro_positions[idx] = clip
        else:
            gameplay.append(clip)

    if not gameplay:
        return spec, set()

    if mode == "chronological":
        gameplay.sort(key=lambda c: float(c.get("start_seconds", 0)))
    elif mode == "hype":
        gameplay.sort(key=lambda c: float(c.get("hype_score", 0)), reverse=True)
    elif mode == "funny":
        gameplay.sort(key=lambda c: float(c.get("funny_score", 0)), reverse=True)
    elif mode == "story":
        gameplay.sort(key=lambda c: float(c.get("story_score", 0)), reverse=True)
    elif mode == "hook":
        gameplay.sort(key=lambda c: float(c.get("start_seconds", 0)))
        hottest_idx = max(
            range(len(gameplay)),
            key=lambda i: float(gameplay[i].get("hype_score", 0)),
        )
        hottest = gameplay.pop(hottest_idx)
        gameplay.insert(0, hottest)

    # Walk original positions: keep intros where they were, fill the
    # non-intro slots from the sorted gameplay queue in order.
    new_clips: list[dict] = []
    gameplay_iter = iter(gameplay)
    for idx in range(len(clips)):
        if idx in intro_positions:
            new_clips.append(intro_positions[idx])
        else:
            new_clips.append(next(gameplay_iter))
    spec["clips"] = new_clips
    dirty = {c["id"] for c in gameplay if c.get("id")}
    return spec, dirty


def set_clip_numbers(spec: dict, on: bool) -> tuple[dict, set[str]]:
    """Toggle the iteration "#NN" label overlay on every clip.

    Returns ALL clip ids as dirty — the cached part files were rendered
    for the opposite label state and need to be re-encoded. (Cached
    files for the *current* state are picked up automatically on the
    next render via the labelled-vs-unlabelled filename suffix.)
    """
    spec = json.loads(json.dumps(spec))
    spec["show_clip_numbers"] = bool(on)
    return spec, {c["id"] for c in spec["clips"]}


def add_caption_segment(
    spec: dict,
    clip_idx: int,
    *,
    start_seconds: float,
    end_seconds: float,
    text: str,
    style: dict | None = None,
) -> tuple[dict, set[str]]:
    """Add a single caption segment to a clip. Sorted by start time.

    The new segment appears in the clip's `caption_segments` array;
    the renderer picks it up on the next render and burns it in.
    `style` is the per-segment visual recipe — preset name plus any
    field overrides. Omit for the default look.
    """
    spec = json.loads(json.dumps(spec))
    clip = spec["clips"][clip_idx]
    segments = clip.setdefault("caption_segments", [])
    new_seg: dict = {
        "start_seconds": float(start_seconds),
        "end_seconds": float(end_seconds),
        "text": str(text),
    }
    if style:
        new_seg["style"] = style
    segments.append(new_seg)
    # Keep segments sorted by start time so the editor reads naturally
    # and the renderer's burn order is left-to-right.
    segments.sort(key=lambda s: float(s.get("start_seconds", 0)))
    return spec, {clip["id"]}


def remove_caption_segment(
    spec: dict, clip_idx: int, segment_idx: int
) -> tuple[dict, set[str]]:
    """Drop a single caption segment from a clip by its 0-based index.

    Idempotent on out-of-range — silently no-ops rather than raising,
    so a stale UI index doesn't crash the edit. Returns the clip's id
    as dirty so the render re-emits without the removed segment.
    """
    spec = json.loads(json.dumps(spec))
    clip = spec["clips"][clip_idx]
    segments = clip.get("caption_segments") or []
    if 0 <= segment_idx < len(segments):
        del segments[segment_idx]
    return spec, {clip["id"]}


def tiktokify_clip(spec: dict, clip_idx: int) -> tuple[dict, set[str]]:
    """Transform a clip's captions into TikTok-style word-segments.

    THIS IS the implementation of "switch to TikTok mode" — instead of
    flipping a `caption_mode` flag, we explode each existing segment
    into per-word segments and tag each with `style.preset = "tiktok"`.
    The renderer treats them like any other styled segments; no
    dual code path.

    Uses Whisper `words[]` when available, falls back to even-split
    over each segment's duration when not. Clears the legacy
    `caption_mode` field since per-segment style now drives rendering.
    """
    from .edits import explode_segments_to_words

    spec = json.loads(json.dumps(spec))
    clip = spec["clips"][clip_idx]
    original = clip.get("caption_segments") or []
    clip["caption_segments"] = explode_segments_to_words(original, style_preset="tiktok")
    # Clear legacy mode so the new model is the only source of truth
    # going forward. The renderer's migration path no longer applies.
    clip.pop("caption_mode", None)
    return spec, {clip["id"]}


def set_clip_captions(
    spec: dict, clip_idx: int, segments: list[dict]
) -> tuple[dict, set[str]]:
    """Replace a clip's caption_segments with user-edited content.

    Each segment is `{start_seconds, end_seconds, text}` plus optionally
    a `words` array. When the editor changes a segment's text, the old
    word boundaries no longer match — the renderer's
    `_segment_words_with_fallback` will even-split the new text across
    the segment duration, which is acceptable for v1.

    For segments whose text DIDN'T change, the caller can preserve the
    original `words` array to keep accurate per-word timings in TikTok
    mode. This mutator doesn't try to detect that — it stores exactly
    what's passed.

    Returns the new spec + the clip's id as dirty so re-render picks up
    the new captions in the burnt-in drawtext filter chain.
    """
    spec = json.loads(json.dumps(spec))
    clip = spec["clips"][clip_idx]
    # Normalise: ensure each segment has required fields, drop nullish
    # `words` (let the renderer fall back to even-split).
    normalised: list[dict] = []
    for seg in segments:
        normalised_seg = {
            "start_seconds": float(seg.get("start_seconds", 0)),
            "end_seconds": float(seg.get("end_seconds", 0)),
            "text": str(seg.get("text", "")),
        }
        if seg.get("words"):
            normalised_seg["words"] = seg["words"]
        normalised.append(normalised_seg)
    clip["caption_segments"] = normalised
    return spec, {clip["id"]}


_VALID_CAPTION_MODES = ("segment", "tiktok")


def set_caption_mode(spec: dict, clip_idx: int, mode: str) -> tuple[dict, set[str]]:
    """Set a clip's caption_mode ("segment" | "tiktok"). Pure.

    Marks the clip dirty so the next render re-encodes it with the
    new caption renderer. Other clips keep their cached parts.
    """
    if mode not in _VALID_CAPTION_MODES:
        raise ValueError(f"unknown caption_mode {mode!r}; expected one of {_VALID_CAPTION_MODES}")
    spec = json.loads(json.dumps(spec))
    clip = spec["clips"][clip_idx]
    clip["caption_mode"] = mode
    return spec, {clip["id"]}
