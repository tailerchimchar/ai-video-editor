"""Highlights builder.

Turns a ranked asset into an organized, human-browsable folder:

    WORKSPACE/highlights/<game>/<MM-DD-YYYY>_<champion|HHhMM>/
        01_kill_4m19s.mp4
        02_kill_2m01s.mp4
        ...
        index.md      (human: timestamps, scores, reasons)
        index.json    (machine: same data, for the API/MCP)

Folder naming is a pure function of the asset + its candidates, so the
GET endpoint can re-derive the path deterministically. ffmpeg work goes
through the shared `editing.trim_clip` helper.
"""

import json
import logging
import re
from datetime import date
from pathlib import Path

from .bucketing import prefilter_summary
from .candidates.probe import get_duration_seconds
from .config import settings
from .cut_picker import (
    CutPick,
    CutRunStats,
    pick_cuts_batch,
    prepare_clip_cut_job,
    shadow_log_line,
)
from .decisions import select_backend
from .editing import trim_clip

_log = logging.getLogger(__name__)

_FN_RE = re.compile(
    r"^(?P<game>.+?)_(?P<mo>\d{1,2})-(?P<d>\d{1,2})-(?P<y>\d{4})_"
    r"(?P<h>\d{1,2})-(?P<mi>\d{1,2})"
)
_BAD = re.compile(r'[<>:"/\\|?*]+')


def _safe(name: str) -> str:
    """Make a string safe as a Windows path segment."""
    return _BAD.sub("-", name).strip(" .") or "unknown"


def _champion(candidates: list[dict]) -> str | None:
    for c in candidates:
        if c.get("source") == "riot_api":
            champ = (c.get("metadata") or {}).get("champion")
            if champ:
                return champ
    return None


def _riot_correlation(candidates: list[dict]) -> dict | None:
    """Honesty signal from the matched Riot game (or None if no Riot data)."""
    for c in candidates:
        if c.get("source") == "riot_api":
            m = c.get("metadata") or {}
            return {
                "match_id": m.get("match_id"),
                "champion": m.get("champion"),
                "confidence": m.get("correlation_confidence"),
                "delta_start_seconds": m.get("delta_start_seconds"),
                "delta_duration_seconds": m.get("delta_duration_seconds"),
                "detected_offset_seconds": m.get("detected_offset_seconds"),
                "offset_quality": m.get("offset_quality"),
                "offset_source": m.get("offset_source"),
            }
    return None


def _dropped_entries(candidates: list[dict]) -> list[dict]:
    """Candidates the pre-filter dropped, for the audit trail in index.json.
    They are never cut as clips."""
    return [
        {
            "candidate_id": c["id"],
            "source": c.get("source"),
            "start_seconds": c.get("start_seconds"),
            "end_seconds": c.get("end_seconds"),
            "event_type": c.get("event_type"),
            "prefilter_probability": c["metadata"].get("prefilter_probability"),
        }
        for c in candidates
        if (c.get("metadata") or {}).get("prefilter_dropped")
    ]


def _mmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}m{s % 60:02d}s"


def _vlm_enabled() -> bool:
    """Feature flag for the VLM per-clip validation loop. Safe on any
    startup — never touches Ollama; just reads the config bit."""
    return bool(settings.vlm_enabled)


def _parse_name(asset: dict) -> tuple[str, str, str]:
    """(game, date, time_disc) parsed from the recording filename, with
    graceful fallbacks for unrecognized names."""
    filename = asset.get("filename", "")
    m = _FN_RE.match(filename)
    if m:
        return (
            m.group("game"),
            f"{m.group('mo')}-{m.group('d')}-{m.group('y')}",
            f"{m.group('h')}h{m.group('mi')}",
        )
    game = (asset.get("game") or "unknown").split("_")[0]
    return game, "unknown-date", (Path(filename).stem[:20] or "rec")


def relative_folder(asset: dict, candidates: list[dict]) -> Path:
    """Deterministic `highlights/<game>/<date>_<disc>` for this asset.

    Pure — no I/O — so the POST (writer) and GET (reader) agree.
    """
    game, date, time_disc = _parse_name(asset)
    corr = _riot_correlation(candidates)
    # Only let the champion name the folder when the match is trustworthy.
    # A low-confidence (likely wrong) match must not mislabel the folder
    # or collide with the real game's folder — fall back to record time.
    trusted = corr and corr.get("confidence") in ("high", "medium")
    disc = _safe(corr["champion"]) if trusted and corr.get("champion") else time_disc
    return Path("highlights") / _safe(game) / f"{date}_{disc}"


# --- clip windowing strategies (extendable, per source) ---
#
# A strategy maps (candidate, ranking, duration) -> (start, end). To add
# behavior for a new source, write a `_window_*` and register it in
# `_STRATEGIES` — nothing else changes (open/closed). Any candidate that
# carries a precise `metadata.anchor_seconds` uses the anchor strategy
# regardless of source, so future precise sources work for free.


def _clamp(start: float, end: float, duration: float) -> tuple[float, float]:
    start = max(0.0, start)
    end = min(duration, end) if duration > 0 else end
    if end <= start:  # degenerate — guarantee a non-empty clip
        end = start + 1.0
    return round(start, 2), round(end, 2)


def _event_override(event_type: str | None) -> tuple[float, float] | None:
    """Per-event-type (pre, post) seconds override, or None if no override.

    Anchor sources fall through to the global highlight_pre/post defaults
    when this returns None; fallback sources apply NO padding when None
    (preserves pre-tuning behavior for un-event-tagged data).
    """
    if not event_type:
        return None
    return settings.event_window_overrides.get(event_type)


def _window_anchor(cand: dict, r: dict, duration: float) -> tuple[float, float]:
    """Center on the exact event moment ± per-event (or global) pre/post,
    then extend to whatever wider window the ranker suggested.

    Anchor = explicit `metadata.anchor_seconds` (Riot kill) or, for older
    candidates without it, the symmetric midpoint of the candidate window.

    Rampage fix: when the ranker clustered multiple kills into one
    candidate (e.g. an assist→kill→kill→double-kill burst), its
    `suggested_start/end` span the whole sequence. The anchor rule
    alone would truncate the payoff after `anchor + post`. Taking the
    UNION of both windows preserves the per-event pre/post as a
    minimum while letting the ranker's smarter cluster window expand
    outward. Capped at `highlight_max_seconds` so a pathological cluster
    can't produce a 5-minute clip.
    """
    meta = cand.get("metadata") or {}
    anchor = meta.get("anchor_seconds")
    if anchor is None:
        anchor = (cand.get("start_seconds", 0) + cand.get("end_seconds", 0)) / 2
    override = _event_override(cand.get("event_type"))
    pre, post = override or (settings.highlight_pre_seconds, settings.highlight_post_seconds)
    start, end = anchor - pre, anchor + post

    sug_start = r.get("suggested_start_seconds")
    sug_end = r.get("suggested_end_seconds")
    if sug_start is not None:
        start = min(start, float(sug_start))
    if sug_end is not None:
        end = max(end, float(sug_end))

    cap = float(settings.highlight_max_seconds)
    if end - start > cap:
        end = start + cap

    return _clamp(start, end, duration)


def _window_full(cand: dict, r: dict, duration: float) -> tuple[float, float]:
    """Outplayed already cut this as an event clip — keep the whole file."""
    return _clamp(0.0, duration, duration)


def _window_fallback(cand: dict, r: dict, duration: float) -> tuple[float, float]:
    """Fuzzy sources (audio peak, transcript): trust the LLM-tightened
    window, then EXTEND it by per-event-type padding for breathing room.

    Without an event-type override, behavior is unchanged from pre-tuning:
    use the ranker's suggested window verbatim. This keeps existing tests
    + un-tagged historical data stable.
    """
    start = float(r.get("suggested_start_seconds", cand.get("start_seconds", 0.0)))
    end = float(r.get("suggested_end_seconds", cand.get("end_seconds", 0.0)))
    override = _event_override(cand.get("event_type"))
    if override:
        pre, post = override
        start -= pre
        end += post
    return _clamp(start, end, duration)


_STRATEGIES = {
    "riot_api": _window_anchor,
    "outplayed_clip": _window_full,
}


def cut_window(cand: dict, ranking: dict, duration: float) -> tuple[float, float]:
    """Resolve the (start, end) to actually cut for one kept suggestion."""
    if (cand.get("metadata") or {}).get("anchor_seconds") is not None:
        return _window_anchor(cand, ranking, duration)
    strategy = _STRATEGIES.get(cand.get("source"), _window_fallback)
    return strategy(cand, ranking, duration)


def event_time_for_cut(cand: dict) -> float | None:
    """Real event timestamp for the Jev cut path, or None to skip.

    Ground-truth / OCR sources store the event in ``metadata.anchor_seconds``:
    - ``overwolf_game_event`` (incl. multi-kill clusters; also
      ``first_kill_seconds`` / ``last_kill_seconds`` — we use the cluster
      ``anchor_seconds`` midpoint of the burst, which is still event-derived)
    - ``riot_api`` (VOD-mapped kill/objective; ``riot_event_ms`` is the raw
      game-clock ms — ``anchor_seconds`` already includes sync offset)
    - ``cv_kda`` (scoreboard OCR transition)

    ``anchoring.EventAnchor.anchor_seconds`` is the same Overwolf field.

    Fuzzy sources have no event time — ``audio_peak``, ``transcript_keyword``,
    ``outplayed_clip``. Never invent a midpoint from the legacy window.
    """
    meta = cand.get("metadata") or {}
    anchor = meta.get("anchor_seconds")
    if anchor is None:
        return None
    return float(anchor)


def _jev_clip_cut_active() -> bool:
    return bool(settings.grokbot_jev_clip_cut_enabled or settings.grokbot_jev_clip_cut_shadow)


def _apply_pick_to_legacy(
    *,
    event: str,
    legacy_start: float,
    legacy_end: float,
    pick: CutPick | None,
    stats: CutRunStats,
) -> tuple[float, float]:
    """Merge one batch pick into stats / shadow logs; return the cut to use."""
    if pick is None:
        return legacy_start, legacy_end

    stats.clips += 1
    stats.jev_ms += pick.jev_ms
    stats.input_tokens += pick.input_tokens
    if pick.flagged:
        stats.flagged += 1
        stats.fallback += 1

    if settings.grokbot_jev_clip_cut_shadow:
        line = shadow_log_line(
            event_type=event,
            legacy_start=legacy_start,
            legacy_end=legacy_end,
            pick=pick,
        )
        stats.shadow_lines.append(line)
        _log.info("%s", line)

    if settings.grokbot_jev_clip_cut_enabled:
        return pick.start, pick.end
    return legacy_start, legacy_end


def _batch_jev_clip_cuts(
    *,
    path: str,
    duration: float,
    plan: list[dict],
    stats: CutRunStats,
    ask_many=None,
    runner=None,
) -> None:
    """Prepare signals/options for anchored clips, one (chunked) Jev batch.

    Mutates each ``plan`` entry's ``start``/``end`` when enabled. Fail-safe:
    batch ask errors leave every prepared clip on its legacy window.
    """
    jobs: list[dict] = []
    job_plan_indices: list[int] = []

    for i, entry in enumerate(plan):
        event_time = entry.get("event_time")
        if event_time is None:
            stats.clips += 1
            stats.skipped_no_anchor += 1
            continue
        try:
            job, ffmpeg_ms, _signals = prepare_clip_cut_job(
                path,
                event_time,
                entry["event"],
                duration,
                runner=runner,
            )
        except Exception as exc:
            _log.warning("jev_clip_cut prepare failed (%s); keeping legacy", exc)
            stats.clips += 1
            stats.fallback += 1
            stats.flagged += 1
            continue
        stats.ffmpeg_ms += ffmpeg_ms
        jobs.append(job)
        job_plan_indices.append(i)

    if not jobs:
        return

    try:
        picks, n_requests = pick_cuts_batch(jobs, ask_many=ask_many)
    except Exception as exc:
        _log.warning("jev_clip_cut batch failed (%s); keeping legacy windows", exc)
        stats.clips += len(jobs)
        stats.fallback += len(jobs)
        stats.flagged += len(jobs)
        return

    stats.jev_requests += n_requests
    for plan_i, pick in zip(job_plan_indices, picks, strict=True):
        entry = plan[plan_i]
        start, end = _apply_pick_to_legacy(
            event=entry["event"],
            legacy_start=entry["legacy_start"],
            legacy_end=entry["legacy_end"],
            pick=pick,
            stats=stats,
        )
        entry["start"] = start
        entry["end"] = end


def build_highlights(asset: dict, rankings: list[dict], candidates: list[dict]) -> dict:
    """Cut every kept suggestion into the organized folder + write indexes.

    Returns a summary dict (also persisted as index.json)."""
    cand_by_id = {c["id"]: c for c in candidates}
    kept = [r for r in rankings if r.get("keep")]
    # Chronological: the highlights folder is a log of the game as it
    # happened. Ranker decides WHICH to keep via hype_score / keep flag;
    # we just present survivors in game-time order so filename indexes
    # (01_..., 02_...) match the play sequence and the preview reel
    # feels like a coherent recap instead of a hype-shuffled montage.
    # For a hype-first compile, use compile_highlights(order="hook"|
    # "hype") — that's the layer where ordering choices belong.
    kept.sort(key=lambda r: float(r.get("suggested_start_seconds", 0)))

    rel = relative_folder(asset, candidates)
    dest = settings.workspace_dir / rel
    dest.mkdir(parents=True, exist_ok=True)
    # Idempotent regen: clear prior clips/indexes so re-cutting (e.g.
    # after tuning the window) doesn't leave stale files behind.
    for old in (*dest.glob("*.mp4"), dest / "index.md", dest / "index.json"):
        old.unlink(missing_ok=True)

    duration = get_duration_seconds(asset["path"])
    champ = _champion(candidates)
    game_slug, _, _ = _parse_name(asset)
    clips: list[dict] = []
    cut_stats = CutRunStats()
    # Legacy windows first; optional Jev path batches one ask for all
    # anchored clips (see _batch_jev_clip_cuts).
    plan: list[dict] = []
    for r in kept:
        cand = cand_by_id.get(r.get("candidate_id"), {})
        event = cand.get("event_type") or "clip"
        legacy_start, legacy_end = cut_window(cand, r, duration)
        plan.append(
            {
                "ranking": r,
                "cand": cand,
                "event": event,
                "legacy_start": legacy_start,
                "legacy_end": legacy_end,
                "event_time": event_time_for_cut(cand) if _jev_clip_cut_active() else None,
                "start": legacy_start,
                "end": legacy_end,
            }
        )
    if _jev_clip_cut_active():
        _batch_jev_clip_cuts(
            path=asset["path"],
            duration=duration,
            plan=plan,
            stats=cut_stats,
        )

    # VLM per-clip validation loop is opt-in via VLM_ENABLED. When it's
    # off (or the backend is unreachable), we fall back to the direct
    # trim_clip path — same behavior as before this loop existed.
    use_vlm = _vlm_enabled()
    for i, entry in enumerate(plan, start=1):
        r = entry["ranking"]
        cand = entry["cand"]
        event = entry["event"]
        start, end = entry["start"], entry["end"]
        fname = f"{i:02d}_{_safe(event)}_{_mmss(start)}.mp4"
        out_path = dest / fname

        if use_vlm:
            from .vlm.loops import validate_and_cut

            meta = cand.get("metadata") or {}
            loop_result = validate_and_cut(
                source_path=asset["path"],
                out_path=out_path,
                start=start,
                end=end,
                game=(game_slug or "").lower() or None,
                event_type=event,
                source=cand.get("source"),
                anchor_seconds=meta.get("anchor_seconds"),
            )
            clip_entry: dict = {
                "file": fname if loop_result.ok else None,
                "event": event,
                "start_seconds": round(loop_result.start_seconds, 2),
                "end_seconds": round(loop_result.end_seconds, 2),
                "hype_score": r.get("hype_score"),
                "funny_score": r.get("funny_score"),
                "story_score": r.get("story_score"),
                "reason": r.get("reason"),
                "ok": loop_result.ok,
                "error": None if loop_result.ok else loop_result.final_verdict.why,
                "vlm_verdict": loop_result.final_verdict.verdict,
                "vlm_why": loop_result.final_verdict.why,
                "vlm_iterations": loop_result.iterations,
            }
            clips.append(clip_entry)
        else:
            ok, err = trim_clip(asset["path"], out_path.as_posix(), start, end)
            clips.append(
                {
                    "file": fname,
                    "event": event,
                    "start_seconds": round(start, 2),
                    "end_seconds": round(end, 2),
                    "hype_score": r.get("hype_score"),
                    "funny_score": r.get("funny_score"),
                    "story_score": r.get("story_score"),
                    "reason": r.get("reason"),
                    "ok": ok,
                    "error": err,
                }
            )

    written = sum(1 for c in clips if c["ok"])
    if cut_stats.clips or cut_stats.skipped_no_anchor:
        cut_stats.log_summary()
    game, _, _ = _parse_name(asset)
    dropped = _dropped_entries(candidates)
    summary = {
        "game": game,
        "champion": champ,
        "source_recording": asset.get("filename"),
        "folder": dest.as_posix(),
        "riot_correlation": _riot_correlation(candidates),
        "clips_written": written,
        "clips_total": len(clips),
        "clips": clips,
        "dropped": dropped,
        "prefilter": prefilter_summary(len(dropped), select_backend()),
    }

    (dest / "index.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (dest / "index.md").write_text(_render_md(summary), encoding="utf-8")
    return summary


def build_clip_batch(game: str, assets: list[dict]) -> dict:
    """Pool a game's short Outplayed clips into one organized folder.

    No LLM: Outplayed already curated these (each file *is* a detected
    event), and an outplayed_clip candidate carries no signal to rank on,
    so a rank call would be a paid no-op. We just copy each whole clip,
    newest first, into highlights/<game>/clips_<date>/ with an index.
    """
    assets = sorted(assets, key=lambda a: a.get("created_at", ""), reverse=True)
    dest = settings.workspace_dir / "highlights" / _safe(game) / f"clips_{date.today()}"
    dest.mkdir(parents=True, exist_ok=True)
    for old in (*dest.glob("*.mp4"), dest / "index.md", dest / "index.json"):
        old.unlink(missing_ok=True)

    clips: list[dict] = []
    for i, asset in enumerate(assets, start=1):
        duration = get_duration_seconds(asset["path"])
        stem = Path(asset["filename"]).stem
        fname = f"{i:02d}_{_safe(stem)[:50]}.mp4"
        ok, err = trim_clip(asset["path"], (dest / fname).as_posix(), 0.0, duration)
        clips.append(
            {
                "file": fname,
                "source_file": asset["filename"],
                "duration_seconds": round(duration, 2),
                "created_at": asset.get("created_at"),
                "ok": ok,
                "error": err,
            }
        )

    written = sum(1 for c in clips if c["ok"])
    summary = {
        "game": game,
        "source_recording": f"{len(assets)} Outplayed clips (organize-only, no LLM)",
        "folder": dest.as_posix(),
        "clips_written": written,
        "clips_total": len(clips),
        "clips": clips,
    }
    (dest / "index.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (dest / "index.md").write_text(_render_batch_md(summary), encoding="utf-8")
    return summary


def _render_batch_md(s: dict) -> str:
    lines = [
        f"# {s['game']} — Outplayed clips",
        "",
        f"- {s['clips_written']}/{s['clips_total']} clips copied (newest first)",
        "- Pre-curated by Outplayed; no AI ranking applied.",
        "",
        "| # | File | Length | Source |",
        "|---|------|--------|--------|",
    ]
    for i, c in enumerate(s.get("clips", []), start=1):
        name = c["file"] if c["ok"] else f"~~{c['file']}~~ ({c['error']})"
        lines.append(f"| {i} | {name} | {_mmss(c['duration_seconds'])} | {c['source_file']} |")
    return "\n".join(lines) + "\n"


def _render_md(s: dict) -> str:
    head = s.get("champion") or s.get("game") or "Highlights"
    corr = s.get("riot_correlation")
    lines = [
        f"# Highlights — {head}",
        "",
        f"- Source recording: `{s.get('source_recording')}`",
        f"- Game: {s.get('game')}",
        f"- Champion: {s.get('champion') or 'n/a'}",
        f"- Clips: {s.get('clips_written')}/{s.get('clips_total')} written",
    ]
    if s.get("prefilter"):
        lines.append(f"- Pre-filter: {s['prefilter']}")
    if corr:
        conf = (corr.get("confidence") or "?").upper()
        warn = (
            "  ⚠️ **LOW — this is likely the WRONG game; champion/kills may not match the video.**"
            if corr.get("confidence") == "low"
            else ""
        )
        lines += [
            "",
            f"- **Riot match correlation: {conf}**{warn}",
            f"  - matched match: `{corr.get('match_id')}` (champion: {corr.get('champion')})",
            f"  - Δ start vs filename time: {corr.get('delta_start_seconds')}s "
            f"| Δ duration: {corr.get('delta_duration_seconds')}s",
            f"  - clip offset: {corr.get('detected_offset_seconds')}s "
            f"(source: {corr.get('offset_source')}, "
            f"quality: {corr.get('offset_quality')})",
            "  - High = trust it. Medium = spot-check. Low = discard.",
        ]
    lines += [
        "",
        "| # | File | Event | Time | Hype | Funny | Story | Why |",
        "|---|------|-------|------|------|-------|-------|-----|",
    ]
    for i, c in enumerate(s.get("clips", []), start=1):
        status = c["file"] if c["ok"] else f"~~{c['file']}~~ ({c['error']})"
        lines.append(
            f"| {i} | {status} | {c['event']} | "
            f"{_mmss(c['start_seconds'])} | {c['hype_score']} | "
            f"{c['funny_score']} | {c['story_score']} | {c['reason']} |"
        )
    dropped = s.get("dropped") or []
    if dropped:
        lines += ["", f"## Dropped by pre-filter ({len(dropped)})", ""]
        for d in dropped:
            lines.append(
                f"- {d['event_type']} ({d['source']}) at {_mmss(d['start_seconds'])}: "
                f"include-probability {d['prefilter_probability']}"
            )
    return "\n".join(lines) + "\n"
