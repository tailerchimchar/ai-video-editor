"""story-join: one video from several games' story.mp4, a card per game.

Also writes `<out>.join.json`, the map of the joined video (see join_map.py),
and `<out>.chapters.txt`, the YouTube description chapters. The like &
subscribe overlay (WORKSPACE/assets/subscribe_animation/subscribe_alpha.mov)
plays once, SUBSCRIBE_LEAD_S into the game after game `subscribe_after`. The
default is 1: the midpoint of a 2-game video, the 1/3 point of a 3-game one
(owner 2026-11-26).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from ..config import settings
from .join_map import SUBSCRIBE_LEAD_S, build_join_map, youtube_chapters
from .planner import StoryPlan
from .render import StoryRenderError, _concat, _encode_opts, _render_card, _run_ffmpeg

_ROUND_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_\d{4}_([^_]+)_(\d+)-(\d+)-(\d+)$")

SUBSCRIBE_ASSET = ("assets", "subscribe_animation", "subscribe_alpha.mov")
SUBSCRIBE_WIDTH_FRAC = 0.30  # of the frame width
SUBSCRIBE_MARGIN_FRAC = 0.03  # top-left: clear of the HUD, minimap and kill feed


def round_label(round_name: str) -> str:
    """`2026-11-23_2056_zeri_8-7-12` -> `Zeri 8/7/12`; other shapes unchanged."""
    m = _ROUND_RE.match(round_name)
    if not m:
        return round_name
    _, champ, k, d, a = m.groups()
    return f"{champ.capitalize()} {k}/{d}/{a}"


def story_path(round_name: str) -> Path:
    return Path(settings.workspace_dir) / "story" / round_name / "story.mp4"


def default_out(rounds: list[str]) -> Path:
    m = _ROUND_RE.match(rounds[0])
    date = m.group(1) if m else rounds[0]
    champs = []
    for r in rounds:
        mm = _ROUND_RE.match(r)
        champs.append(mm.group(2) if mm else r)
    name = f"joined_{date}_{'-'.join(champs)}.mp4"
    return Path(settings.workspace_dir) / "story" / name


def subscribe_asset() -> Path:
    return Path(settings.workspace_dir).joinpath(*SUBSCRIBE_ASSET)


def subscribe_overlay_cmd(story: str, asset: str, out: str, at_s: float) -> list[str]:
    """The story with the transparent subscribe overlay at `at_s`, top-left,
    SUBSCRIBE_WIDTH_FRAC of the frame wide. Audio is copied; the video is
    re-encoded with the story settings so the result still concat-copies."""
    w, m = SUBSCRIBE_WIDTH_FRAC, SUBSCRIBE_MARGIN_FRAC
    graph = (
        f"[1:v]setpts=PTS-STARTPTS+{at_s}/TB[s0];"
        f"[s0][0:v]scale2ref=w=main_w*{w}:h=ow/mdar[s][base];"
        f"[base][s]overlay=x=main_w*{m}:y=main_h*{m}:eof_action=pass,format=yuv420p[v]"
    )
    return [
        settings.ffmpeg_path,
        "-y",
        "-i",
        story,
        "-i",
        asset,
        "-filter_complex",
        graph,
        "-map",
        "[v]",
        "-map",
        "0:a?",
        "-c:a",
        "copy",
        *_encode_opts(),
        out,
    ]


def _duration(path: Path) -> float:
    r = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            Path(path).as_posix(),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return float(r.stdout.strip())


def _asset_duration(path: Path) -> float:
    try:
        return round(_duration(path), 2)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 4.0


def _offset(plan: StoryPlan) -> float:
    from .cli import load_timeline

    tl = load_timeline(plan.asset_id) if plan.asset_id else None
    return tl.offset_s if tl is not None else 0.0


def join_stories(
    rounds: list[str],
    out: Path,
    *,
    cards: bool = True,
    subscribe_after: int | None = 1,
    title: str | None = None,
) -> Path:
    """Concatenate each round's story.mp4, with a `Game n · label` card before
    each. Overlay the subscribe animation in the game after `subscribe_after`
    (None, an out-of-range value or a missing asset: no overlay). Write the
    join map and chapter list next to the mp4."""
    out = Path(out)
    stories = [story_path(r) for r in rounds]
    for r, s in zip(rounds, stories, strict=True):
        if not s.is_file():
            raise StoryRenderError(f"story video missing for round {r}: {s}")
    out.parent.mkdir(parents=True, exist_ok=True)
    asset = subscribe_asset()
    use_sub = subscribe_after is not None and 1 <= subscribe_after < len(rounds) and asset.is_file()
    card_s = 0.0
    with tempfile.TemporaryDirectory(prefix="story_join_") as tmp:
        parts: list[str] = []
        for n, (r, s) in enumerate(zip(rounds, stories, strict=True), start=1):
            if cards:
                card = Path(tmp) / f"card_{n}.mp4"
                _render_card(f"Game {n} · {round_label(r)}", card)
                card_s = _duration(card)
                parts.append(card.as_posix())
            if use_sub and subscribe_after is not None and n == subscribe_after + 1:
                with_sub = Path(tmp) / f"story_{n}_subscribe.mp4"
                cmd = subscribe_overlay_cmd(
                    s.as_posix(), asset.as_posix(), with_sub.as_posix(), SUBSCRIBE_LEAD_S
                )
                ok, err = _run_ffmpeg(cmd, 60 + 3 * _duration(s))
                if not ok:
                    raise StoryRenderError(f"subscribe overlay failed: {err}")
                parts.append(with_sub.as_posix())
            else:
                parts.append(s.as_posix())
        ok, err = _concat(parts, out.as_posix())
    if not ok:
        raise StoryRenderError(f"join concat failed: {err}")
    sub = (subscribe_after, _asset_duration(asset), asset) if use_sub else None
    write_join_files(rounds, out, card_s=card_s, title=title, subscribe=sub)
    return out


def write_join_files(
    rounds: list[str],
    out: Path,
    *,
    card_s: float,
    title: str | None = None,
    subscribe: tuple[int, float, Path] | None = None,
) -> Path:
    """`<out>.join.json` and `<out>.chapters.txt` next to the joined mp4."""
    plans = [
        StoryPlan.model_validate_json(
            (story_path(r).parent / "story.json").read_text(encoding="utf-8")
        )
        for r in rounds
    ]
    digests = [Path(settings.workspace_dir) / "panel" / r / "digest.json" for r in rounds]
    jm = build_join_map(
        video=out,
        title=title or " · ".join(round_label(r) for r in rounds),
        rounds=rounds,
        labels=[round_label(r) for r in rounds],
        plans=plans,
        offsets_s=[_offset(p) for p in plans],
        story_durations_s=[_duration(story_path(r)) for r in rounds],
        card_s=card_s,
        story_card_s=settings.story_card_seconds,
        story_jsons=[story_path(r).parent / "story.json" for r in rounds],
        digest_jsons=[d if d.is_file() else None for d in digests],
        subscribe=subscribe,
    )
    map_path = out.with_suffix(".join.json")
    map_path.write_text(jm.model_dump_json(indent=2), encoding="utf-8")
    out.with_suffix(".chapters.txt").write_text(youtube_chapters(jm), encoding="utf-8")
    return map_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="story-join", description=__doc__.splitlines()[0])
    ap.add_argument("rounds", nargs="+", help="round names, in game order (at least 2)")
    ap.add_argument("--out", help="output mp4 (default WORKSPACE/story/joined_...)")
    ap.add_argument("--no-cards", action="store_true", help="skip the game cards")
    ap.add_argument("--title", help="video title for the join map")
    ap.add_argument(
        "--subscribe-after",
        type=int,
        default=1,
        help="play the subscribe overlay in the game after this one (default 1; 0 = none)",
    )
    args = ap.parse_args(argv)
    if len(args.rounds) < 2:
        ap.error("need at least 2 rounds")
    out = Path(args.out) if args.out else default_out(args.rounds)
    try:
        res = join_stories(
            args.rounds,
            out,
            cards=not args.no_cards,
            subscribe_after=args.subscribe_after or None,
            title=args.title,
        )
    except StoryRenderError as e:
        print(f"story-join: {e}", file=sys.stderr)
        return 1
    print(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
