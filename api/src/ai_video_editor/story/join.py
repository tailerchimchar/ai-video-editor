"""story-join: one video from several games' story.mp4, a swipe between games.

Each game after the first slides in from the right over the previous one
(SWIPE_S, xfade slideleft) with a whoosh (WORKSPACE/sfx/whoosh.wav, made by
tools/brand/make_whoosh.py). No title cards (owner 2026-11-26). The like &
subscribe overlay (WORKSPACE/assets/subscribe_animation/subscribe_alpha.mov)
plays once, bottom centre just above the ability bar, SUBSCRIBE_LEAD_S into
the game after game `subscribe_after`. The default is 1: the midpoint of a
2-game video, the 1/3 point of a 3-game one.

Rendering is segmented so no ffmpeg pass holds more than two videos (one pass
with xfade chained over 6 inputs peaked at 3.1 GB on the 16 GB laptop,
2026-11-27): each piece's body and each swipe (with SEG_PAD_S either side,
so the whoosh fits) is encoded on its own, video only; one audio-only pass
builds the whole soundtrack (crossfades + whooshes); the video segments are
then concatenated without re-encoding and muxed with that soundtrack. Piece
lengths are snapped to whole frames so video and audio stay in sync.

Optional bookends (story/bookends.py, owner 2026-11-27): a hook (cold open
of picked chapters, cut before their outcome, opening line on top) swiped
into game 1, and an outro end card swiped in after the last game. Their
parts render to WORKSPACE/story/_joined/<out stem>/ first.

Also writes `<out>.join.json`, the map of the joined video (see join_map.py),
and `<out>.chapters.txt`, the YouTube description chapters.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..config import settings
from . import bookends
from .bookends import HookClip, HookPick
from .join_map import (
    SUBSCRIBE_LEAD_S,
    JoinHook,
    JoinOptions,
    build_join_map,
    hook_block,
    youtube_chapters,
)
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
SEG_PAD_S = 0.5  # plain footage either side of a swipe inside its segment


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


def parts_dir(out: Path) -> Path:
    """Where a join's hook / outro parts render (also the joined video's
    narration work dir: the `_` keeps story listings from showing it)."""
    return Path(settings.workspace_dir) / "story" / "_joined" / Path(out).stem


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


def frames_s(d: float) -> float:
    """`d` snapped down to whole frames (pieces are cut on the frame grid)."""
    return math.floor(round(d * FPS, 6)) / FPS


@dataclass(frozen=True)
class Segment:
    """One separately encoded stretch of the joined video. A body plays
    piece `piece` from `start_s`; a swipe plays the tail of piece `piece`
    from `start_s` and slides piece `piece + 1` in after SEG_PAD_S."""

    kind: Literal["body", "swipe"]
    piece: int
    start_s: float  # in the piece
    dur_s: float
    at_s: float  # in the joined video

    @property
    def frames(self) -> int:
        return round(self.dur_s * FPS)


def plan_segments(
    durations: list[float], swipe_s: float = SWIPE_S, pad_s: float = SEG_PAD_S
) -> list[Segment]:
    """Bodies and swipes in order. `durations` are frame-snapped; the joined
    length is sum(durations) - swipes * swipe_s, as in swipe_offsets."""
    segs: list[Segment] = []
    t = 0.0
    n = len(durations)
    edge = swipe_s + pad_s
    for k, d in enumerate(durations):
        b0 = 0.0 if k == 0 else edge
        b1 = d if k == n - 1 else d - edge
        if b1 - b0 < 1 / FPS:
            raise StoryRenderError(f"piece {k + 1} is too short to join ({d:.2f} s)")
        segs.append(Segment("body", k, round(b0, 4), round(b1 - b0, 4), round(t, 4)))
        t += b1 - b0
        if k < n - 1:
            length = swipe_s + 2 * pad_s
            segs.append(Segment("swipe", k, round(d - edge, 4), length, round(t, 4)))
            t += length
    return segs


def _video_opts() -> list[str]:
    opts = _encode_opts()
    return opts[: opts.index("-c:a")]


def _audio_opts() -> list[str]:
    opts = _encode_opts()
    return opts[opts.index("-c:a") :]


def _norm(i: int, label: str, *, dur: float) -> str:
    """Input `i` on the story settings, padded and cut to exactly `dur`."""
    return (
        f"[{i}:v]fps={FPS},settb=AVTB,format=yuv420p,setsar=1,"
        f"tpad=stop_mode=clone:stop_duration=1,trim=duration={dur},setpts=PTS-STARTPTS[{label}]"
    )


def segment_cmd(
    seg: Segment,
    pieces: list[str],
    out: str,
    *,
    swipe_s: float = SWIPE_S,
    pad_s: float = SEG_PAD_S,
    subscribe: tuple[str, float] | None = None,
) -> list[str]:
    """Video-only encode of one segment. `subscribe` (path, at_s in the
    segment) overlays the like & subscribe animation bottom centre."""
    g: list[str]
    if seg.kind == "body":
        inputs = ["-ss", f"{seg.start_s}", "-i", pieces[seg.piece]]
        g = [_norm(0, "v", dur=seg.dur_s)]
    else:
        edge = swipe_s + pad_s
        inputs = ["-ss", f"{seg.start_s}", "-i", pieces[seg.piece], "-i", pieces[seg.piece + 1]]
        g = [
            _norm(0, "a0", dur=edge),
            _norm(1, "b0", dur=edge),
            f"[a0][b0]xfade=transition=slideleft:duration={swipe_s}:offset={pad_s}[v]",
        ]
    if subscribe is not None:
        path, at = subscribe
        sidx = inputs.count("-i")
        inputs += ["-i", path]
        wf, bf = SUBSCRIBE_WIDTH_FRAC, SUBSCRIBE_BOTTOM_FRAC
        g[-1] = g[-1].removesuffix("[v]") + "[pre]"
        g.append(f"[{sidx}:v]setpts=PTS-STARTPTS+{at}/TB[s0]")
        g.append(f"[s0][pre]scale2ref=w=main_w*{wf}:h=ow/mdar[s][base]")
        g.append(
            f"[base][s]overlay=x=(main_w-overlay_w)/2:y=main_h*{bf}-overlay_h"
            f":eof_action=pass,format=yuv420p[v]"
        )
    return [
        settings.ffmpeg_path,
        "-y",
        *inputs,
        "-filter_complex",
        ";".join(g),
        "-map",
        "[v]",
        "-an",
        "-frames:v",
        str(seg.frames),
        *_video_opts(),
        out,
    ]


def audio_cmd(
    pieces: list[str],
    durations: list[float],
    out: str,
    *,
    swipe_s: float = SWIPE_S,
    whoosh: tuple[str, float] | None = None,
) -> list[str]:
    """The whole soundtrack in one audio-only pass: each piece cut to its
    frame-snapped length, faded over its swipes and delayed to where its
    video starts (sample-exact; acrossfade lost 0.06-0.28 s per long pair,
    2026-11-27), `whoosh` (path, peak_s) delayed so its peak lands
    mid-swipe, all summed."""
    n = len(pieces)
    starts = [0.0, *swipe_offsets(durations, swipe_s)]
    total = round(starts[-1] + durations[-1], 4)
    inputs: list[str] = []
    g: list[str] = []
    mix: list[str] = []
    for i, (p, d) in enumerate(zip(pieces, durations, strict=True)):
        inputs += ["-i", p]
        fades = ""
        if i:
            fades += f",afade=t=in:d={swipe_s}"
        if i < n - 1:
            fades += f",afade=t=out:st={round(d - swipe_s, 4)}:d={swipe_s}"
        ns = round(starts[i] * 48000)  # samples: exact on the frame grid
        g.append(
            f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,"
            f"apad=whole_dur={d},atrim=duration={d},asetpts=N/SR/TB{fades},"
            f"adelay={ns}S|{ns}S[a{i}]"
        )
        mix.append(f"[a{i}]")
    if whoosh is not None and n > 1:
        path, peak = whoosh
        inputs += ["-i", path]
        labels = "".join(f"[w{i}]" for i in range(n - 1))
        g.append(f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo,asplit={n - 1}{labels}")
        for i, o in enumerate(starts[1:]):
            ms = max(0, round((o + swipe_s / 2 - peak) * 1000))
            g.append(f"[w{i}]adelay={ms}|{ms}[wd{i}]")
            mix.append(f"[wd{i}]")
    g.append(
        f"{''.join(mix)}amix=inputs={len(mix)}:duration=longest:normalize=0,"
        f"apad=whole_dur={total},atrim=duration={total}[am]"
    )
    return [
        settings.ffmpeg_path,
        "-y",
        *inputs,
        "-filter_complex",
        ";".join(g),
        "-map",
        "[am]",
        "-vn",
        *_audio_opts(),
        out,
    ]


def concat_cmd(list_file: str, audio: str, out: str) -> list[str]:
    """Stitch the video segments (same encoder settings, so no re-encode)
    and mux the soundtrack."""
    return [
        settings.ffmpeg_path,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        list_file,
        "-i",
        audio,
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        out,
    ]


def _subscribe_in(segs: list[Segment], at: float) -> tuple[int, float] | None:
    """(segment index, at in that segment) of the body holding `at`."""
    for i, s in enumerate(segs):
        if s.kind == "body" and s.at_s <= at < s.at_s + s.dur_s:
            return i, round(at - s.at_s, 3)
    return None


def render_joined(
    pieces: list[str],
    durations: list[float],
    out: Path,
    work: Path,
    *,
    swipe_s: float = SWIPE_S,
    whoosh: tuple[str, float] | None = None,
    subscribe: tuple[str, float] | None = None,
) -> None:
    """Segments -> soundtrack -> concat (see the module doc). `durations`
    must be frame-snapped. Segment files are deleted afterwards."""
    segs = plan_segments(durations, swipe_s)
    seg_dir = work / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    sub = _subscribe_in(segs, subscribe[1]) if subscribe is not None else None
    files: list[Path] = []
    for i, seg in enumerate(segs):
        f = seg_dir / f"seg_{i:03d}.mp4"
        overlay = (subscribe[0], sub[1]) if subscribe is not None and sub and sub[0] == i else None
        cmd = segment_cmd(seg, pieces, f.as_posix(), swipe_s=swipe_s, subscribe=overlay)
        ok, err = _run_ffmpeg(cmd, 120 + 3 * seg.dur_s)
        if not ok:
            raise StoryRenderError(f"join segment {i + 1}/{len(segs)} ({seg.kind}) failed: {err}")
        files.append(f)
    audio = seg_dir / "audio.m4a"
    total = sum(durations)
    ok, err = _run_ffmpeg(
        audio_cmd(pieces, durations, audio.as_posix(), swipe_s=swipe_s, whoosh=whoosh),
        120 + total,
    )
    if not ok:
        raise StoryRenderError(f"join soundtrack failed: {err}")
    listing = seg_dir / "segments.txt"
    listing.write_text("".join(f"file '{f.as_posix()}'\n" for f in files), encoding="utf-8")
    ok, err = _run_ffmpeg(concat_cmd(listing.as_posix(), audio.as_posix(), out.as_posix()), 600)
    if not ok:
        raise StoryRenderError(f"join concat failed: {err}")
    shutil.rmtree(seg_dir, ignore_errors=True)


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


def _load_plan(round_name: str) -> StoryPlan:
    return StoryPlan.model_validate_json(
        (story_path(round_name).parent / "story.json").read_text(encoding="utf-8")
    )


def hook_clips(rounds: list[str], picks: list[HookPick]) -> list[HookClip]:
    """Resolve each pick (1-based game and chapter) to a window of that
    game's story.mp4, ending before the play's outcome."""
    clips: list[HookClip] = []
    for p in picks:
        if not 1 <= p.game <= len(rounds):
            raise StoryRenderError(f"hook pick game {p.game} is not 1..{len(rounds)}")
        plan = _load_plan(rounds[p.game - 1])
        if not 1 <= p.chapter <= len(plan.chapters):
            raise StoryRenderError(
                f"hook pick game {p.game} has chapters 1..{len(plan.chapters)}, not {p.chapter}"
            )
        ch = plan.chapters[p.chapter - 1]
        base_start, base_end = bookends.hook_window_rec(plan, p.chapter - 1, p.mode)
        rec_start = max(base_start - p.start_s, min(ch.start_s, base_start))
        rec_end = min(base_end + p.end_s, ch.end_s) if p.end_s else base_end
        if rec_end - rec_start < bookends.HOOK_MIN_S:
            raise StoryRenderError(f"hook pick {p.game}:{p.chapter} is too short after its edits")
        start, end = bookends.hook_video_window(
            plan, p.chapter - 1, rec_start, rec_end, settings.story_card_seconds
        )
        clips.append(
            HookClip(
                story_path(rounds[p.game - 1]),
                start,
                end,
                p.text,
                freeze=p.mode == "tease",
                rec_start_s=rec_start,
                rec_end_s=rec_end,
            )
        )
    return clips


def join_stories(
    rounds: list[str],
    out: Path,
    *,
    subscribe_after: int | None = 1,
    title: str | None = None,
    swipe_s: float = SWIPE_S,
    hook: list[HookPick] | None = None,
    hook_title: str | None = None,
    outro: bool = False,
    music: Path | None = None,
    music_start_s: float = 0.0,
) -> Path:
    """Join each round's story.mp4 with a right-to-left swipe and a whoosh
    (when WORKSPACE/sfx/whoosh.wav exists) between games. Overlay the
    subscribe animation in the game after `subscribe_after` (None, an
    out-of-range value or a missing asset: no overlay). `hook`: chapters
    for the cold open (`hook_title` pops over the first); `outro`: add the
    end card, over `music` (default WORKSPACE/music/outro.*, if any). Write
    the join map and chapter list next to the mp4."""
    out = Path(out)
    stories = [story_path(r) for r in rounds]
    for r, s in zip(rounds, stories, strict=True):
        if not s.is_file():
            raise StoryRenderError(f"story video missing for round {r}: {s}")
    out.parent.mkdir(parents=True, exist_ok=True)
    work = parts_dir(out)
    asset = subscribe_asset()
    clips = hook_clips(rounds, hook) if hook else []
    hooks = bookends.render_hook(clips, work, title=hook_title) if hook else []
    outro_file: Path | None = None
    if outro:
        outro_file = bookends.render_outro(
            stories[-1],
            work,
            subscribe=asset if asset.is_file() else None,
            music=music if music is not None else bookends.music_asset(),
            music_start_s=music_start_s,
        )
    pieces = [*hooks, *stories, *([outro_file] if outro_file else [])]
    durations = [frames_s(_duration(p)) for p in pieces]
    offs = swipe_offsets(durations, swipe_s)
    starts = [0.0, *offs]  # where each piece starts in the joined video
    first = len(hooks)  # piece index of game 1
    sub_at: float | None = None
    if subscribe_after is not None and 1 <= subscribe_after < len(rounds) and asset.is_file():
        sub_at = round(starts[first + subscribe_after] + SUBSCRIBE_LEAD_S, 3)
    wh = whoosh_asset()
    render_joined(
        [p.as_posix() for p in pieces],
        durations,
        out,
        work,
        swipe_s=swipe_s,
        whoosh=(wh.as_posix(), _whoosh_peak(wh)) if wh.is_file() else None,
        subscribe=(asset.as_posix(), sub_at) if sub_at is not None else None,
    )
    sub = (subscribe_after, _asset_duration(asset), asset) if sub_at is not None else None
    hook_map: JoinHook | None = None
    if hook:
        plans = [_load_plan(r) for r in rounds]
        hook_map = hook_block(
            rounds,
            plans,
            hook,
            clips,
            starts[:first],
            durations[:first],
            [_offset(p) for p in plans],
            mode=hook[0].mode,
            title=hook_title,
        )
    write_join_files(
        rounds,
        out,
        transition_s=swipe_s,
        title=title,
        subscribe=sub,
        lead_s=starts[first],
        outro=(durations[-1], outro_file) if outro_file else None,
        hook=hook_map,
        options=JoinOptions(
            subscribe_after=subscribe_after, outro=outro, music_start_s=music_start_s
        ),
    )
    return out


def write_join_files(
    rounds: list[str],
    out: Path,
    *,
    transition_s: float = SWIPE_S,
    title: str | None = None,
    subscribe: tuple[int, float, Path] | None = None,
    lead_s: float = 0.0,
    outro: tuple[float, Path] | None = None,
    hook: JoinHook | None = None,
    options: JoinOptions | None = None,
) -> Path:
    """`<out>.join.json` and `<out>.chapters.txt` next to the joined mp4."""
    plans = [_load_plan(r) for r in rounds]
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
        lead_s=lead_s,
        outro=outro,
        hook=hook,
        options=options,
        hook_mode=hook.mode if hook else "tease",
    )
    map_path = out.with_suffix(".join.json")
    map_path.write_text(jm.model_dump_json(indent=2), encoding="utf-8")
    out.with_suffix(".chapters.txt").write_text(youtube_chapters(jm), encoding="utf-8")
    return map_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="story-join", description=__doc__.splitlines()[0])
    ap.add_argument("rounds", nargs="*", help="round names, in game order (at least 2)")
    ap.add_argument(
        "--apply-edits",
        metavar="STEM",
        help="re-join a joined video from the owner's hook edits (STEM.edits.json); no rounds",
    )
    ap.add_argument(
        "--force-edits", action="store_true", help="apply edits made on an older join map"
    )
    ap.add_argument("--out", help="output mp4 (default WORKSPACE/story/joined_...)")
    ap.add_argument("--title", help="video title for the join map")
    ap.add_argument(
        "--subscribe-after",
        type=int,
        default=1,
        help="play the subscribe overlay in the game after this one (default 1; 0 = none)",
    )
    ap.add_argument(
        "--hook",
        action="append",
        default=[],
        metavar="GAME:CHAPTER[:TEXT]",
        help="a cold-open clip: game n's chapter m (1-based). Tease mode (default) cuts "
        "before the outcome and freezes (optional TEXT pops on the frozen frame); "
        "--hook-mode play plays through it. Repeat for more clips.",
    )
    ap.add_argument(
        "--hook-mode",
        choices=["tease", "play"],
        default="tease",
        help="tease: cut before the outcome, freeze + blur (default); "
        "play: run through the outcome, no freeze or teaser text",
    )
    ap.add_argument("--hook-title", help="opening line over the first hook clip")
    ap.add_argument("--outro", action="store_true", help="add the end card after the last game")
    ap.add_argument("--music", help="outro music file (default WORKSPACE/music/outro.*)")
    ap.add_argument("--music-start", type=float, default=0.0, help="seconds into the music")
    args = ap.parse_args(argv)
    if args.apply_edits:
        if args.rounds:
            ap.error("--apply-edits takes no rounds")
        from .apply_edits import apply_edits

        try:
            print(apply_edits(args.apply_edits, force=args.force_edits))
        except (StoryRenderError, ValueError) as e:
            print(f"story-join: {e}", file=sys.stderr)
            return 1
        return 0
    if len(args.rounds) < 2:
        ap.error("need at least 2 rounds")
    out = Path(args.out) if args.out else default_out(args.rounds)
    try:
        res = join_stories(
            args.rounds,
            out,
            subscribe_after=args.subscribe_after or None,
            title=args.title,
            hook=[
                dataclasses.replace(bookends.parse_pick(h), mode=args.hook_mode) for h in args.hook
            ]
            or None,
            hook_title=args.hook_title,
            outro=args.outro,
            music=Path(args.music) if args.music else None,
            music_start_s=args.music_start,
        )
    except (StoryRenderError, ValueError) as e:
        print(f"story-join: {e}", file=sys.stderr)
        return 1
    print(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
