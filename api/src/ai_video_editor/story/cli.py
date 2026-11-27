"""`story-render` CLI: wire the panel round, Riot cache, lane and mic files
into `plan_story` + `render_story`.

    story-render --asset ID --round <panel folder name or path> [--no-cards] [--out DIR]
                 [--plays | --no-plays] [--no-early] [--no-broll] [--no-requests]
                 [--fresh-vision]

With `PLAYS_DRIVE_CUTS=true` and a frozen `WORKSPACE/plays/<asset>.json`,
chapters come from the plays by default; `--plays` forces that path (and
fails without the file), `--no-plays` forces the panel-beat path.

Inputs: `WORKSPACE/panel/<round>/gaming-expert.json` story beats (and
`clips.json` to place clip-relative beats), `WORKSPACE/riot/<asset>/`
{match,timeline,meta,lane}.json, `WORKSPACE/mic/<asset>.json`.
Output (default): `WORKSPACE/story/<round>/`. The owner's asks in its
`requests.json` (story/requests.py) are applied on top of the plan; the render
never writes that file.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from ..config import settings
from ..grokbot.voice import owner_rows
from ..league.early_game import EarlySegment, bar_times, early_segments, first_back_s
from ..league.hud_bars import BarSample, sample_bars
from ..league.items import ItemInfo, first_back_main_item, load_items, owner_purchases
from ..league.lane import lane_summary_path, parse_lane_frames
from ..league.lane_models import LaneMinute, LaneSummary
from ..league.play_models import GameModel
from ..league.riot_timeline import RiotTimeline, _from_cache, cache_dir_for, load_cached
from ..refs import normalize_clip_ref
from .broll import BRollInsert, first_back_insert, place_broll, plan_broll, with_first_back
from .early_cache import CACHE_FILENAME, early_cache_key, read_early_cache, write_early_cache
from .planner import Beat, plan_story, plan_story_from_plays
from .render import StoryRenderError, render_story
from .requests import apply_requests, load_requests

_log = logging.getLogger(__name__)

# The gaming-expert's whole-game summary entry, not a clip.
GAME_STORY_REF = "GAME_STORY"
MAX_BEAT_S = 60.0


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _ref_seconds(ref: str) -> float | None:
    norm = normalize_clip_ref(ref)
    m, _, rest = norm.partition("m")
    if not (m.isdigit() and rest.endswith("s") and rest[:-1].isdigit()):
        return None
    return float(int(m) * 60 + int(rest[:-1]))


def _clip_windows(round_path: Path) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    """Clip windows keyed by normalised `clip_ref` and by normalised file stem
    (either may be missing; a null file is never keyed as "None")."""
    for c in _read_json(round_path / "clips.json") or []:
        if not isinstance(c, dict) or c.get("start_seconds") is None:
            continue
        window = (float(c["start_seconds"]), float(c.get("end_seconds") or c["start_seconds"]))
        if c.get("clip_ref"):
            out[normalize_clip_ref(str(c["clip_ref"]))] = window
        if c.get("file"):
            out[normalize_clip_ref(Path(str(c["file"])).stem)] = window
    return out


def _entry_beats(entry: dict, clips: dict[str, tuple[float, float]]) -> list[Beat]:
    ref = normalize_clip_ref(str(entry.get("clip_ref", "")))
    clip = clips.get(ref)
    point = _ref_seconds(ref)
    # Unknown clip (e.g. clips.json has "file": null): the ref's own time
    # stands in for the clip start.
    clip_start = clip[0] if clip else point
    out: list[Beat] = []
    for raw in entry.get("story_beats") or []:
        text = raw.get("beat") if isinstance(raw, dict) else raw
        if not isinstance(text, str) or not text.strip():
            continue
        if isinstance(raw, dict) and raw.get("t_start") is not None:
            start = float(raw["t_start"])
            end = float(raw.get("t_end", start))
            if clip is None:
                _log.warning(
                    "story beat %r: clip %r unknown in clips.json; placing by ref time %s",
                    text,
                    ref,
                    point,
                )
            # Clip-relative when below the clip start, unless shifting would land
            # past the clip's end (then it was already in recording seconds).
            past_end = clip is not None and start + clip_start > clip[1]
            if clip_start is not None and start < clip_start and not past_end:
                start, end = start + clip_start, end + clip_start
        elif clip:
            start, end = clip
        elif point is not None:
            start = end = point
        else:
            _log.warning("story beat %r has no time and clip %r is unknown", text, ref)
            continue
        if end - start > MAX_BEAT_S:
            span = end - start
            _log.warning("story beat %r spans %.0fs > %.0fs; skipped", text, span, MAX_BEAT_S)
            continue
        try:
            out.append(Beat(t_start=start, t_end=end, beat=text.strip()))
        except ValidationError as e:
            _log.warning("story beat %r skipped: %s", text, e)
    return out


def load_beats(round_path: Path) -> list[Beat]:
    """Gaming-expert story beats in recording seconds. Beats with `t_start`
    below their clip's start are clip-relative and shifted by it (unless the
    shift would land past the clip's end: then they are absolute); beats
    without times take the clip window (or the ref's time as a point)."""
    raw = _read_json(round_path / "gaming-expert.json")
    entries = raw.get("clips") if isinstance(raw, dict) else raw
    clips = _clip_windows(round_path)
    beats: list[Beat] = []
    for entry in entries or []:
        if isinstance(entry, dict) and str(entry.get("clip_ref", "")).strip() != GAME_STORY_REF:
            beats += _entry_beats(entry, clips)
    return beats


def load_timeline(asset_id: str) -> RiotTimeline | None:
    cached = load_cached(cache_dir_for(asset_id))
    return _from_cache(cached) if cached is not None else None


def _early_backend(asset_id: str):
    from ..league.understand import default_backend

    return default_backend(asset_id, cap_usd=settings.game_understanding_budget_usd)


def is_team_game(asset_id: str) -> bool:
    """A custom game (tournaments such as ACL): Riot's `gameType` CUSTOM_GAME.
    Ranked / normal queues are solo-queue content (owner 2026-11-26)."""
    match = _read_json(cache_dir_for(asset_id) / "match.json")
    info = match.get("info", {}) if isinstance(match, dict) else {}
    return info.get("gameType") == "CUSTOM_GAME"


def _hud_bars(
    src: str, timeline: RiotTimeline, raw: dict
) -> tuple[list[BarSample] | None, list[BarSample] | None]:
    """HUD bar samples for the quiet-opening check and the lane phase's heavy
    trades, in one pass. (None, None) without the video or on any failure:
    the opening then starts at 0:20 and no lane trade is added."""
    if not src or not Path(src).is_file():
        return None, None
    try:
        opening_t, lane_t = bar_times(timeline, raw)
        samples = sample_bars(src, opening_t + lane_t)
    except Exception as e:  # optional enrichment: never fatal
        _log.warning("story: HUD bars unavailable: %s", e)
        return None, None
    n = len(opening_t)
    return samples[:n], samples[n:]


def load_early(
    asset_id: str, timeline: RiotTimeline, src: str, *, fresh_vision: bool = False
) -> list[EarlySegment] | None:
    """Early-game segments from the cached raw timeline and the HUD bars read
    off `src` (quiet-opening check, heavy trades), refined by the vision
    check (Task B) when `story_early_confirm` and that module exists. The
    confirmed result is cached per asset (`early_cache`) so re-renders agree;
    `fresh_vision` bypasses and refreshes it. None when there is no raw
    timeline or no owner."""
    raw = _read_json(cache_dir_for(asset_id) / "timeline.json")
    if not isinstance(raw, dict) or timeline.owner_pid is None:
        return None
    opening_bars, lane_bars = _hud_bars(src, timeline, raw)
    try:
        segments = early_segments(
            timeline,
            raw,
            team_game=is_team_game(asset_id),
            opening_bars=opening_bars,
            lane_bars=lane_bars,
        )
    except (KeyError, ValueError, TypeError) as e:
        _log.warning("early-game segments unavailable for %s: %s", asset_id, e)
        return None
    if not (settings.story_early_confirm and segments):
        return segments
    try:
        from ..vlm.early_confirm import confirm_early
    except ImportError:
        _log.info("story: early-game confirmer not available; using unconfirmed windows")
        return segments
    back = first_back_s(raw, timeline.owner_pid, timeline.offset_s)
    backend = _early_backend(asset_id)
    backend_id = f"{getattr(backend, 'name', '')}:{getattr(backend, 'model', '')}"
    key = early_cache_key(segments, back, backend_id, timeline.offset_s)
    cache_path = cache_dir_for(asset_id) / CACHE_FILENAME
    if not fresh_vision:
        hit = read_early_cache(cache_path, key)
        if hit is not None:
            return hit
    try:
        confirmed = confirm_early(
            src,
            segments,
            first_back_s=back,
            backend=backend,
            start_s=max(0.0, 45.0 + timeline.offset_s),  # recording seconds
        )
    except Exception as e:
        _log.warning("early-game confirm failed for %s: %s", asset_id, e)
        return segments
    if any(seg.confirmed for seg in confirmed):  # confirm_early swallows failures: don't pin those
        write_early_cache(cache_path, key, confirmed)
    return confirmed


def load_broll(
    asset_id: str, timeline: RiotTimeline
) -> tuple[list[BRollInsert], dict[int, ItemInfo]]:
    """Item cutaways from the owner's mic lines and purchases. Empty (never
    fatal) without a raw timeline, an owner, purchases, mic rows or items.
    Mic text is only matched against item names, never kept or logged."""
    raw = _read_json(cache_dir_for(asset_id) / "timeline.json")
    if not isinstance(raw, dict) or timeline.owner_pid is None:
        return [], {}
    try:
        purchases = owner_purchases(raw, timeline.owner_pid, timeline.offset_s)
    except (KeyError, ValueError, TypeError) as e:
        _log.warning("story broll: purchases unavailable for %s: %s", asset_id, e)
        return [], {}
    if not purchases:
        return [], {}
    try:
        rows = owner_rows(Path(settings.database_path), asset_id)
    except sqlite3.Error as e:
        _log.warning("story broll: mic rows unavailable for %s: %s", asset_id, e)
        return [], {}
    if not rows:
        return [], {}
    items = load_items()
    if not items:
        return [], {}
    inserts = plan_broll(rows, purchases, items)
    _log.info("story broll: %d item inserts for %s", len(inserts), asset_id)
    return inserts, items


def load_first_back_broll(
    asset_id: str, timeline: RiotTimeline, items: dict[int, ItemInfo] | None = None
) -> tuple[list[BRollInsert], dict[int, ItemInfo]]:
    """The first back's main item over its purchase footage (owner 2026-11-26:
    "once we recall get the item i bought"). Needs no mic transcript. Empty,
    with a log line and never fatal, without a raw timeline, an owner, a
    first back, item data or the item's icon."""
    raw = _read_json(cache_dir_for(asset_id) / "timeline.json")
    if not isinstance(raw, dict) or timeline.owner_pid is None:
        return [], {}
    try:
        back = first_back_s(raw, timeline.owner_pid, timeline.offset_s)
        purchases = owner_purchases(raw, timeline.owner_pid, timeline.offset_s)
    except (KeyError, ValueError, TypeError) as e:
        _log.warning("story broll: first back unavailable for %s: %s", asset_id, e)
        return [], {}
    if back is None:
        return [], {}
    items = items or load_items()
    if not items:
        _log.info("story broll: no item data; first-back item skipped for %s", asset_id)
        return [], {}
    main = first_back_main_item(purchases, items, first_back_rec_s=back)
    if main is None:
        _log.info("story broll: no main item at the first back for %s", asset_id)
        return [], {}
    info = items[main.item_id]
    if not info.icon_path.is_file():
        _log.info("story broll: no icon for item %d; first-back item skipped", info.id)
        return [], {}
    _log.info("story broll: first-back item %d at %.1fs for %s", info.id, main.t_rec_s, asset_id)
    return [first_back_insert(main, info)], items


def load_lane(asset_id: str) -> LaneSummary | None:
    """`lane.json` when present, else parsed from the cached match + timeline."""
    data = _read_json(lane_summary_path(asset_id))
    if isinstance(data, dict):
        try:
            minutes = [LaneMinute(**m) for m in data.get("minutes") or []]
            return LaneSummary(**{**data, "minutes": minutes})
        except (TypeError, ValueError) as e:  # dataclass fields; ValidationError is a ValueError
            _log.warning("malformed lane.json for %s: %s", asset_id, e)
            return None
    cached = load_cached(cache_dir_for(asset_id))
    if cached is None:
        return None
    match, timeline, meta = cached
    try:
        return parse_lane_frames(
            match,
            timeline,
            owner_puuid=meta["owner_puuid"],
            offset_s=float(meta.get("offset_s") or 0.0),
        )
    except (ValueError, KeyError) as e:
        _log.warning("lane summary unavailable for %s: %s", asset_id, e)
        return None


def load_mic_stream(asset_id: str) -> int | None:
    """Audio-relative mic stream index (`0:a:<idx>`), None when unknown or
    when the mic pass recorded an `error` (its pick was never transcribed)."""
    data = _read_json(Path(settings.workspace_dir) / "mic" / f"{asset_id}.json")
    if not isinstance(data, dict) or data.get("error"):
        return None
    idx = data.get("stream_index")
    return int(idx) if isinstance(idx, int) else None


def asset_path(asset_id: str) -> str | None:
    conn = sqlite3.connect(settings.database_path)
    try:
        row = conn.execute("SELECT path FROM assets WHERE id = ?", (asset_id,)).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        conn.close()
    return row[0] if row else None


def round_dir(round_arg: str) -> Path:
    p = Path(round_arg)
    return p if p.is_dir() else Path(settings.workspace_dir) / "panel" / round_arg


class StoryInputError(Exception):
    """A missing input (timeline, asset, plays file, bad requests.json)."""


def render_round(
    asset_id: str,
    round: str,
    *,
    plays: bool | None = None,
    no_cards: bool = False,
    no_early: bool = False,
    no_broll: bool = False,
    no_requests: bool = False,
    fresh_vision: bool = False,
    out: str | None = None,
    source: str | None = None,
    review: bool = False,
    say: Callable[[str], None] = _log.info,
) -> Path:
    """Plan and render one round's story; shared by the CLI and the API job.

    `plays`: True forces the frozen plays file (error without it), False forces
    the panel beats, None follows `PLAYS_DRIVE_CUTS`. Raises `StoryInputError`
    for missing inputs and `StoryRenderError` for a failed render."""
    rnd = round_dir(round)
    timeline = load_timeline(asset_id)
    if timeline is None:
        raise StoryInputError(f"no usable Riot timeline cached for asset {asset_id}")
    src = source or asset_path(asset_id)
    if not src:
        raise StoryInputError(f"asset {asset_id} not found; pass --source")
    plays_file = Path(settings.workspace_dir) / "plays" / f"{asset_id}.json"
    use_plays = bool(plays) or (
        plays is None and settings.plays_drive_cuts and plays_file.is_file()
    )
    if use_plays:
        if not plays_file.is_file():
            raise StoryInputError(f"no plays file for asset {asset_id}: {plays_file}")
        game = GameModel.model_validate_json(plays_file.read_text(encoding="utf-8"))
        early = (
            load_early(asset_id, timeline, src, fresh_vision=fresh_vision)
            if settings.story_early_game and not no_early
            else None
        )
        plan = plan_story_from_plays(
            game,
            timeline,
            mic_stream=load_mic_stream(asset_id),
            max_s=360.0,
            asset_id=asset_id,
            early=early,
        )
    else:
        plan = plan_story(
            timeline,
            load_beats(rnd),
            load_lane(asset_id),
            mic_stream=load_mic_stream(asset_id),
            card_s=0.0 if no_cards else settings.story_card_seconds,
            asset_id=asset_id,
        )
    items: dict[int, ItemInfo] = {}
    if settings.story_broll and not no_broll:
        inserts, items = load_broll(asset_id, timeline)
        first_back, fb_items = load_first_back_broll(asset_id, timeline, items)
        items = items or fb_items
        inserts = with_first_back(plan, first_back, inserts)
        if inserts:
            plan = place_broll(plan, inserts, items)
    out_dir = Path(out) if out else Path(settings.workspace_dir) / "story" / rnd.name
    if not no_requests:
        try:
            plan, warnings = apply_requests(plan, load_requests(out_dir), timeline)
        except ValueError as e:
            raise StoryInputError(f"story requests: {e}") from e
        for w in warnings:
            say(f"story requests: {w}")
    story = render_story(
        src,
        plan,
        out_dir,
        cards=not no_cards and not use_plays,
        review=review,
        items=items,
    )
    budget = ", over_budget: protected chapters alone exceed the max" if plan.over_budget else ""
    say(f"{story} ({len(plan.chapters)} chapters, {plan.total_s:.0f}s, audio={plan.audio}{budget})")
    return Path(story)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="story-render", description=__doc__.splitlines()[0])
    ap.add_argument("--asset", required=True, help="asset id")
    ap.add_argument("--round", required=True, help="panel round folder name or path")
    ap.add_argument("--no-cards", action="store_true", help="skip chapter title cards")
    which = ap.add_mutually_exclusive_group()
    which.add_argument("--plays", action="store_true", help="chapters from the frozen plays file")
    which.add_argument("--no-plays", action="store_true", help="chapters from the panel beats")
    ap.add_argument("--no-early", action="store_true", help="plays path: skip early-game chapters")
    ap.add_argument("--no-broll", action="store_true", help="skip item purchase cutaways")
    ap.add_argument("--no-requests", action="store_true", help="ignore the owner's requests.json")
    ap.add_argument(
        "--fresh-vision", action="store_true", help="re-run the early-game vision check"
    )
    ap.add_argument("--out", help="output dir (default WORKSPACE/story/<round>/)")
    ap.add_argument("--source", help="source video path (default: the asset's path)")
    ap.add_argument("--review", action="store_true", help="0.25 fps critic, $5 story budget")
    args = ap.parse_args(argv)
    try:
        render_round(
            args.asset,
            args.round,
            plays=True if args.plays else (False if args.no_plays else None),
            no_cards=args.no_cards,
            no_early=args.no_early,
            no_broll=args.no_broll,
            no_requests=args.no_requests,
            fresh_vision=args.fresh_vision,
            out=args.out,
            source=args.source,
            review=args.review,
            say=print,
        )
    except StoryInputError as e:
        print(e, file=sys.stderr)
        return 2
    except StoryRenderError as e:
        print(f"story render failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
