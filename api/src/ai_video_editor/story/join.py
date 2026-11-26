"""story-join: one video from several games' story.mp4, a swipe between games.

Each game after the first slides in from the right over the previous one
(SWIPE_S, xfade slideleft) with a whoosh (WORKSPACE/sfx/whoosh.wav, made by
tools/brand/make_whoosh.py). No title cards (owner 2026-11-26). The like &
subscribe overlay (WORKSPACE/assets/subscribe_animation/subscribe_alpha.mov)
plays once, bottom centre just above the ability bar, SUBSCRIBE_LEAD_S into
the game after game `subscribe_after`. The default is 1: the midpoint of a
2-game video, the 1/3 point of a 3-game one. Everything is one ffmpeg pass.

Also writes `<out>.join.json`, the map of the joined video (see join_map.py),
and `<out>.chapters.txt`, the YouTube description chapters.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from ..config import settings
from .join_map import SUBSCRIBE_LEAD_S, build_join_map, youtube_chapters
from .planner import StoryPlan
from .render import FPS, StoryRenderError, _encode_opts, _run_ffmpeg

_ROUND_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_\d{4}_([^_]+)_(\d+)-(\d+)-(\d+)$")
# Round folders use plain ASCII champion names; show the real spelling.
_CHAMP_DISPLAY = {
    "kaisa": "Kai'sa",
    "khazix": "Kha'Zix",
    "chogath": "Cho'Gath",
    "velkoz": "Vel'Koz",
    "kogmaw": "Kog'Maw",
    "reksai": "Rek'Sai",
    "belveth": "Bel'Veth",
    "ksante": "K'Sante",
    "missfortune": "Miss Fortune",
    "drmundo": "Dr. Mundo",
    "masteryi": "Master Yi",
}

SUBSCRIBE_ASSET = ("assets", "subscribe_animation", "subscribe_alpha.mov")
SUBSCRIBE_WIDTH_FRAC = 0.30  # of the frame width
# Bottom centre, its bottom edge just above the ability / summoner bar (the
# HUD panel starts about 86% down at 16:9). Owner 2026-11-26.
SUBSCRIBE_BOTTOM_FRAC = 0.84
WHOOSH_ASSET = ("sfx", "whoosh.wav")
WHOOSH_DEFAULT_PEAK_S = 0.3
SWIPE_S = 0.5


def round_label(round_name: str) -> str:
    """`2026-11-23_2056_zeri_8-7-12` -> `Zeri 8/7/12`; other shapes unchanged."""
    m = _ROUND_RE.match(round_name)
    if not m:
        return round_name
    _, champ, k, d, a = m.groups()
    name = _CHAMP_DISPLAY.get(champ.lower(), champ.capitalize())
    return f"{name} {k}/{d}/{a}"


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


def whoosh_asset() -> Path:
    return Path(settings.workspace_dir).joinpath(*WHOOSH_ASSET)


def _whoosh_peak(asset: Path) -> float:
    try:
        return float(json.loads(asset.with_suffix(".json").read_text())["peak_s"])
    except (OSError, ValueError, KeyError, TypeError):
        return WHOOSH_DEFAULT_PEAK_S


def swipe_offsets(durations: list[float], swipe_s: float = SWIPE_S) -> list[float]:
    """Joined-video time where each swipe starts (one per game after the first):
    every game after the first starts `swipe_s` before the previous one ends."""
    out: list[float] = []
    t = 0.0
    for d in durations[:-1]:
        t += d - swipe_s
        out.append(round(t, 3))
    return out


def join_cmd(
    stories: list[str],
    durations: list[float],
    out: str,
    *,
    swipe_s: float = SWIPE_S,
    whoosh: tuple[str, float] | None = None,
    subscribe: tuple[str, float] | None = None,
) -> list[str]:
    """One ffmpeg pass: games chained with xfade slideleft + acrossfade;
    `whoosh` (path, peak_s) mixed so its peak lands mid-swipe; `subscribe`
    (path, at_s) overlaid bottom centre. Every input is normalised to the
    story settings so xfade accepts it."""
    n = len(stories)
    offs = swipe_offsets(durations, swipe_s)
    inputs: list[str] = []
    for s in stories:
        inputs += ["-i", s]
    g: list[str] = []
    for i in range(n):
        g.append(f"[{i}:v]fps={FPS},settb=AVTB,format=yuv420p,setsar=1[v{i}]")
        g.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
    v, a = "[v0]", "[a0]"
    for i in range(1, n):
        g.append(
            f"{v}[v{i}]xfade=transition=slideleft:duration={swipe_s}:offset={offs[i - 1]}[x{i}]"
        )
        g.append(f"{a}[a{i}]acrossfade=d={swipe_s}[c{i}]")
        v, a = f"[x{i}]", f"[c{i}]"
    nxt = n
    if whoosh is not None and n > 1:
        path, peak = whoosh
        inputs += ["-i", path]
        w, nxt = nxt, nxt + 1
        labels = "".join(f"[w{i}]" for i in range(n - 1))
        g.append(f"[{w}:a]aresample=48000,aformat=channel_layouts=stereo,asplit={n - 1}{labels}")
        for i, o in enumerate(offs):
            ms = max(0, round((o + swipe_s / 2 - peak) * 1000))
            g.append(f"[w{i}]adelay={ms}|{ms}[wd{i}]")
        mix = "".join(f"[wd{i}]" for i in range(n - 1))
        g.append(f"{a}{mix}amix=inputs={n}:duration=first:normalize=0[am]")
        a = "[am]"
    if subscribe is not None:
        path, at = subscribe
        inputs += ["-i", path]
        sidx = nxt
        wf, bf = SUBSCRIBE_WIDTH_FRAC, SUBSCRIBE_BOTTOM_FRAC
        g.append(f"[{sidx}:v]setpts=PTS-STARTPTS+{at}/TB[s0]")
        g.append(f"[s0]{v}scale2ref=w=main_w*{wf}:h=ow/mdar[s][base]")
        g.append(
            f"[base][s]overlay=x=(main_w-overlay_w)/2:y=main_h*{bf}-overlay_h"
            f":eof_action=pass,format=yuv420p[vs]"
        )
        v = "[vs]"
    return [
        settings.ffmpeg_path,
        "-y",
        *inputs,
        "-filter_complex",
        ";".join(g),
        "-map",
        v,
        "-map",
        a,
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
        return 5.0


def _offset(plan: StoryPlan) -> float:
    from .cli import load_timeline

    tl = load_timeline(plan.asset_id) if plan.asset_id else None
    return tl.offset_s if tl is not None else 0.0


def join_stories(
    rounds: list[str],
    out: Path,
    *,
    subscribe_after: int | None = 1,
    title: str | None = None,
    swipe_s: float = SWIPE_S,
) -> Path:
    """Join each round's story.mp4 with a right-to-left swipe and a whoosh
    (when WORKSPACE/sfx/whoosh.wav exists) between games. Overlay the
    subscribe animation in the game after `subscribe_after` (None, an
    out-of-range value or a missing asset: no overlay). Write the join map
    and chapter list next to the mp4."""
    out = Path(out)
    stories = [story_path(r) for r in rounds]
    for r, s in zip(rounds, stories, strict=True):
        if not s.is_file():
            raise StoryRenderError(f"story video missing for round {r}: {s}")
    out.parent.mkdir(parents=True, exist_ok=True)
    durations = [_duration(s) for s in stories]
    asset = subscribe_asset()
    sub_at: float | None = None
    if subscribe_after is not None and 1 <= subscribe_after < len(rounds) and asset.is_file():
        game_start = sum(durations[:subscribe_after]) - subscribe_after * swipe_s
        sub_at = round(game_start + SUBSCRIBE_LEAD_S, 3)
    wh = whoosh_asset()
    cmd = join_cmd(
        [s.as_posix() for s in stories],
        durations,
        out.as_posix(),
        swipe_s=swipe_s,
        whoosh=(wh.as_posix(), _whoosh_peak(wh)) if wh.is_file() else None,
        subscribe=(asset.as_posix(), sub_at) if sub_at is not None else None,
    )
    ok, err = _run_ffmpeg(cmd, 120 + 3 * sum(durations))
    if not ok:
        raise StoryRenderError(f"join failed: {err}")
    sub = (subscribe_after, _asset_duration(asset), asset) if sub_at is not None else None
    write_join_files(rounds, out, transition_s=swipe_s, title=title, subscribe=sub)
    return out


def write_join_files(
    rounds: list[str],
    out: Path,
    *,
    transition_s: float = SWIPE_S,
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
        card_s=0.0,
        story_card_s=settings.story_card_seconds,
        story_jsons=[story_path(r).parent / "story.json" for r in rounds],
        digest_jsons=[d if d.is_file() else None for d in digests],
        subscribe=subscribe,
        transition_s=transition_s,
    )
    map_path = out.with_suffix(".join.json")
    map_path.write_text(jm.model_dump_json(indent=2), encoding="utf-8")
    out.with_suffix(".chapters.txt").write_text(youtube_chapters(jm), encoding="utf-8")
    return map_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="story-join", description=__doc__.splitlines()[0])
    ap.add_argument("rounds", nargs="+", help="round names, in game order (at least 2)")
    ap.add_argument("--out", help="output mp4 (default WORKSPACE/story/joined_...)")
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
