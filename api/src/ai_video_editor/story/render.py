"""Story renderer on the compilation path: one ffmpeg encode per chapter,
an optional title card before each chapter, then a stream-copy concat.

Every part is encoded to the same shape (1920x1080, 30 fps, yuv420p, AAC
48 kHz stereo) so `compile._concat` can join them without re-encoding.
Command builders are pure and return argv; `_run_ffmpeg` is the only
subprocess seam.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path

from ..anthropic_surface import SpendCapError, check_budget
from ..compile import _concat
from ..config import settings
from ..edits import apply_sfx, focus_filter, resolve_roi
from ..intros.config import IntroConfig, _TextLogoConfig
from ..intros.render import render_intro
from ..league.items import ItemInfo
from ..sfx_library import death_path, kaching_path, sfx_hit_s
from ..vlm.watch_montage import WatchResult, save_critique, watch_montage
from .broll import BRollInsert
from .planner import Chapter, ChapterEffect, StoryPlan

_log = logging.getLogger(__name__)

__all__ = [
    "StoryRenderError",
    "WatchResult",
    "broll_chapter_cmd",
    "chapter_cmd",
    "effect_segment_cmd",
    "effect_segments",
    "effect_time_map",
    "render_story",
]

WIDTH, HEIGHT, FPS = 1920, 1080, 30
REVIEW_FPS = 0.25
CARD_INTRO_NAME = "_story_card"
CARD_TIMEOUT_S = 60.0
# Single-pass EBU R128 normalisation of the (very quiet) owner mic.
MIC_LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
_VIDEO_CHAIN = f"[0:v]scale={WIDTH}:{HEIGHT},setsar=1,fps={FPS}[v]"
# A source without audio still gets a (silent) track so every part keeps the
# same shape for the stream-copy concat (title cards carry audio).
SILENT_AUDIO = "anullsrc=r=48000:cl=stereo"


class StoryRenderError(RuntimeError):
    """A chapter, card or concat step failed; the message carries ffmpeg's tail."""


def _encode_opts() -> list[str]:
    return [
        "-c:v",
        settings.ffmpeg_video_codec,
        *settings.ffmpeg_video_codec_opts.split(),
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(FPS),
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        "192k",
    ]


def _fade_pair(dur: float, fade_s: float) -> str:
    out_at = round(dur - fade_s, 3)
    return f"afade=t=in:st=0:d={fade_s},afade=t=out:st={out_at}:d={fade_s}"


def audio_filter(mic_stream: int | None, *, dur: float = 0.0, fade_s: float = 0.0) -> str:
    """Mic (loudness-normalised when `story_mic_loudnorm`) on top of ducked game
    audio; game audio alone when no mic is known."""
    fade = f",{_fade_pair(dur, fade_s)}" if fade_s > 0 else ""
    if mic_stream is None:
        return f"[0:a:0]{_fade_pair(dur, fade_s) if fade_s > 0 else 'anull'}[a]"
    game = settings.story_game_audio_volume
    mic = MIC_LOUDNORM if settings.story_mic_loudnorm else "volume=1.0"
    return (
        f"[0:a:{mic_stream}]{mic}[m];[0:a:0]volume={game}[g];"
        f"[m][g]amix=inputs=2:duration=first:normalize=0{fade}[a]"
    )


def chapter_cmd(
    asset_path: str,
    start: float,
    end: float,
    out_path: str,
    *,
    mic_stream: int | None,
    has_audio: bool = True,
    fade_s: float = 0.0,
) -> list[str]:
    """One chapter's argv. `has_audio=False` (the source has no audio stream)
    renders the video with a silent track instead of failing on `0:a:0`."""
    dur = end - start
    video = _VIDEO_CHAIN
    if fade_s > 0:
        video = video.replace(
            "[v]",
            f",fade=t=in:st=0:d={fade_s},fade=t=out:st={round(dur - fade_s, 3)}:d={fade_s}[v]",
        )
    head = [settings.ffmpeg_path, "-y", "-ss", str(start), "-to", str(end), "-i", asset_path]
    if not has_audio:
        silent = ["-f", "lavfi", "-t", str(round(end - start, 3)), "-i", SILENT_AUDIO]
        maps = ["-filter_complex", video, "-map", "[v]", "-map", "1:a"]
        return [*head, *silent, *maps, *_encode_opts(), out_path]
    return [
        *head,
        "-filter_complex",
        f"{video};{audio_filter(mic_stream, dur=dur, fade_s=fade_s)}",
        "-map",
        "[v]",
        "-map",
        "[a]",
        *_encode_opts(),
        out_path,
    ]


# --- owner-requested effects over a time range inside a chapter (AVE-66) ---

LETTERBOX_BAR = 0.12  # fraction of the height per bar
SLOWMO_FACTOR, SLOWMO_MIN = 0.5, 0.25
ZOOM_FACTOR, ZOOM_ROI = 1.3, "center"
# Same defaults as the /edit/focus endpoint (routers/edits.FocusRequest).
FOCUS_DEFAULTS = {"x": 0.5, "y": 0.5, "radius": 0.2, "dim": 0.3}
# Spatial effects stack in this order: zoom, then the spotlight, bars on top.
_SPATIAL_ORDER = ("zoom", "focus", "letterbox")


def _num(fx: ChapterEffect, key: str, default: float) -> float:
    return float(fx.params.get(key, default))


def _spatial_filter(fx: ChapterEffect) -> str:
    if fx.kind == "letterbox":
        px = round(HEIGHT * _num(fx, "bar", LETTERBOX_BAR))
        bar = "drawbox=x=0:y={y}:w=" + f"{WIDTH}:h={px}:color=black:t=fill"
        return f"{bar.format(y=0)},{bar.format(y=HEIGHT - px)}"
    if fx.kind == "zoom":
        factor = _num(fx, "factor", ZOOM_FACTOR)
        w, h, x, y = resolve_roi(str(fx.params.get("roi", ZOOM_ROI)), factor)
        return f"crop={w}:{h}:{x}:{y},scale={WIDTH}:{HEIGHT},setsar=1"
    if fx.kind == "focus":
        p = {k: _num(fx, k, v) for k, v in FOCUS_DEFAULTS.items()}
        return focus_filter(p["x"], p["y"], p["radius"], p["dim"])
    return ""


def effect_video_filter(effects: list[ChapterEffect]) -> str:
    """Spatial filters (after the scale/fps chain) for the effects active on
    one segment, comma-joined; '' when none (slow-mo is temporal)."""
    by_kind = {fx.kind: fx for fx in effects}
    return ",".join(
        f for k in _SPATIAL_ORDER if k in by_kind and (f := _spatial_filter(by_kind[k]))
    )


def slowmo_factor(effects: list[ChapterEffect]) -> float | None:
    """Playback speed of the (last) slow-mo among `effects`, clamped to
    [0.25, 1]; None when there is no slow-mo."""
    slow = [fx for fx in effects if fx.kind == "slowmo"]
    if not slow:
        return None
    f = _num(slow[-1], "factor", SLOWMO_FACTOR)
    clamped = min(1.0, max(SLOWMO_MIN, f))
    if clamped != f:
        _log.warning("story: slowmo factor %s clamped to %s", f, clamped)
    return clamped


def atempo_chain(factor: float) -> str:
    """atempo filters for `factor` (atempo takes >= 0.5 per stage)."""
    stages: list[float] = []
    f = factor
    while f < 0.5 - 1e-9:
        stages.append(0.5)
        f /= 0.5
    stages.append(round(f, 4))
    return ",".join(f"atempo={s:g}" for s in stages)


Segment = tuple[float, float, list[ChapterEffect]]


def effect_segments(effects: list[ChapterEffect], start_s: float, end_s: float) -> list[Segment]:
    """Split [start_s, end_s] at every effect boundary (clamped to the
    chapter); each segment carries the effects active over it. Different
    kinds stack; two of one kind overlapping: the later in the list wins,
    with a warning. Boundaries closer than a frame are merged."""
    clamped = [
        (max(start_s, fx.start_s), min(end_s, fx.end_s), fx)
        for fx in effects
        if min(end_s, fx.end_s) > max(start_s, fx.start_s)
    ]
    frame = 1.0 / FPS
    cuts = [start_s]
    for t in sorted({t for a, b, _ in clamped for t in (a, b)}):
        if t - cuts[-1] >= frame and end_s - t >= frame:
            cuts.append(t)
    cuts.append(end_s)
    segs: list[Segment] = []
    warned: set[str] = set()
    for a, b in pairwise(cuts):
        mid = (a + b) / 2
        by_kind: dict[str, ChapterEffect] = {}
        for lo, hi, fx in clamped:
            if lo <= mid < hi:
                if fx.kind in by_kind and fx.kind not in warned:
                    _log.warning("story: %s effects overlap at %.2fs; the later wins", fx.kind, a)
                    warned.add(fx.kind)
                by_kind[fx.kind] = fx
        winners = {id(fx) for fx in by_kind.values()}
        active = [fx for _, _, fx in clamped if id(fx) in winners]
        segs.append((a, b, [fx for i, fx in enumerate(active) if fx not in active[:i]]))
    return segs


def effect_time_map(
    effects: list[ChapterEffect], start_s: float, end_s: float | None = None
) -> Callable[[float], float]:
    """Map a recording second inside the chapter to its second in the rendered
    part: slow-mo ranges stretch by 1/factor, everything else is 1:1. Pass
    the chapter's `end_s` so the segments match the render exactly."""
    last = max([start_s, *(fx.end_s for fx in effects)]) if end_s is None else end_s
    segs = [
        (a, b, slowmo_factor(act) or 1.0) for a, b, act in effect_segments(effects, start_s, last)
    ]

    def tmap(t: float) -> float:
        out = t - start_s
        for a, b, k in segs:
            if t > a:
                out += (min(t, b) - a) * (1.0 / k - 1.0)
        return out

    return tmap


def _relabel(chain: str, label: str, extra: str) -> str:
    """Append `extra` filters before the chain's final `[label]` output."""
    tail = f"[{label}]"
    if not extra:
        return chain
    assert chain.endswith(tail), chain
    return f"{chain[: -len(tail)]},{extra}{tail}"


def effect_segment_cmd(
    asset_path: str,
    start: float,
    end: float,
    out_path: str,
    effects: list[ChapterEffect],
    *,
    mic_stream: int | None,
    has_audio: bool = True,
    fade_in_s: float = 0.0,
    fade_out_s: float = 0.0,
) -> list[str]:
    """One segment of a chapter with `effects` active over all of it: the
    chapter's normal chain plus the effect filters. Slow-mo stretches video
    (setpts) and the mixed audio (atempo) together. Fades are on the rendered
    (stretched) duration and only on the ends asked for."""
    slow = slowmo_factor(effects)
    out_dur = round((end - start) / (slow or 1.0), 3)
    setpts = f"setpts=PTS/{slow:g}," if slow else ""
    vparts = [f"[0:v]{setpts}scale={WIDTH}:{HEIGHT},setsar=1,fps={FPS}"]
    if spatial := effect_video_filter(effects):
        vparts.append(spatial)
    vfades, afades = [], []
    if fade_in_s > 0:
        vfades.append(f"fade=t=in:st=0:d={fade_in_s}")
        afades.append(f"afade=t=in:st=0:d={fade_in_s}")
    if fade_out_s > 0:
        at = round(out_dur - fade_out_s, 3)
        vfades.append(f"fade=t=out:st={at}:d={fade_out_s}")
        afades.append(f"afade=t=out:st={at}:d={fade_out_s}")
    video = ",".join(vparts + vfades) + "[v]"
    head = [settings.ffmpeg_path, "-y", "-ss", str(start), "-to", str(end), "-i", asset_path]
    if not has_audio:
        silent = ["-f", "lavfi", "-t", str(out_dur), "-i", SILENT_AUDIO]
        maps = ["-filter_complex", video, "-map", "[v]", "-map", "1:a"]
        return [*head, *silent, *maps, *_encode_opts(), out_path]
    post = ",".join(([atempo_chain(slow)] if slow else []) + afades)
    if mic_stream is None:
        audio = f"[0:a:0]{post or 'anull'}[a]"
    else:
        audio = _relabel(audio_filter(mic_stream), "a", post)
    return [
        *head,
        "-filter_complex",
        f"{video};{audio}",
        "-map",
        "[v]",
        "-map",
        "[a]",
        *_encode_opts(),
        out_path,
    ]


# Item B-roll overlay: main icon top-left, below the HUD-safe margin; the
# build-path components in a row under it, an up arrow between (components
# build into the item).
ICON_X, ICON_Y, ICON_PX = 48, 140, 96
COMP_PX, COMP_GAP = 48, 8
ARROW_H = 28
MAX_COMPONENTS = 4
_SCALE = f"scale={WIDTH}:{HEIGHT},setsar=1,fps={FPS}"

# (main icon, component icons) per item id; None = no main icon on disk.
IconPaths = dict[int, tuple[str | None, list[str]]]


def _arrow(x_center: int, top: int) -> str:
    """Up arrow (shaft + stepped head) drawn with drawbox; no font needed."""
    box = "drawbox=x={x}:y={y}:w={w}:h={h}:color=white@0.9:t=fill"
    head = [(16, 4), (10, 4), (4, 4)]  # widths from the base up
    parts = [box.format(x=x_center - 2, y=top + 10, w=4, h=ARROW_H - 14)]
    y = top + 10
    for w, h in head:
        y -= h
        parts.append(box.format(x=x_center - w // 2, y=y, w=w, h=h))
    return ",".join(parts)


def broll_chapter_cmd(
    asset_path: str,
    start: float,
    end: float,
    out_path: str,
    inserts: list[BRollInsert],
    icons: IconPaths,
    *,
    mic_stream: int | None,
    has_audio: bool = True,
    fade_s: float = 0.0,
) -> list[str]:
    """One chapter with item cutaways. The video is the chapter footage cut
    around each insert's window, the purchase footage (with the item icon and
    build path overlaid) filling the window; the audio is the whole chapter's
    original audio, so the narration plays over the cutaway."""
    dur = round(end - start, 3)
    ins_sorted = sorted(inserts, key=lambda i: i.mic_start_s)
    head = [settings.ffmpeg_path, "-y", "-ss", str(start), "-to", str(end), "-i", asset_path]
    extra: list[str] = []
    n_in = 1
    graph: list[str] = []
    pieces: list[str] = []
    gaps: list[tuple[float, float]] = []
    cursor = 0.0
    timeline: list[tuple[str, object]] = []
    for ins in ins_sorted:
        lo = round(max(0.0, ins.mic_start_s - start), 3)
        hi = round(min(dur, ins.window_end_s - start), 3)
        if lo > cursor:
            timeline.append(("gap", (cursor, lo)))
            gaps.append((cursor, lo))
        timeline.append(("ins", ins))
        cursor = hi
    if cursor < dur:
        timeline.append(("gap", (cursor, dur)))
        gaps.append((cursor, dur))
    if gaps:
        outs = "".join(f"[s{j}]" for j in range(len(gaps)))
        graph.append(f"[0:v]split={len(gaps)}{outs}")
    g = 0
    for k, (kind, item) in enumerate(timeline):
        label = f"[p{k}]"
        if kind == "gap":
            a, b = item  # type: ignore[misc]
            graph.append(f"[s{g}]trim=start={a}:end={b},setpts=PTS-STARTPTS,{_SCALE}{label}")
            g += 1
            pieces.append(label)
            continue
        ins: BRollInsert = item  # type: ignore[assignment]
        idx = n_in
        extra += ["-ss", str(ins.src_start_s), "-to", str(ins.src_end_s), "-i", asset_path]
        n_in += 1
        cur = f"[b{k}_0]"
        graph.append(f"[{idx}:v]trim=duration={ins.dur_s},setpts=PTS-STARTPTS,{_SCALE}{cur}")
        main, comps = icons.get(ins.item_id, (None, []))
        layers: list[tuple[str, int, int, int]] = []
        if main:
            layers.append((main, ICON_PX, ICON_X, ICON_Y))
            if ins.build_path and comps:
                row_y = ICON_Y + ICON_PX + ARROW_H
                for j, c in enumerate(comps[:MAX_COMPONENTS]):
                    layers.append((c, COMP_PX, ICON_X + j * (COMP_PX + COMP_GAP), row_y))
        for n, (path, px, x, y) in enumerate(layers):
            extra += ["-loop", "1", "-t", str(ins.dur_s), "-i", path]
            ic, nxt = f"[i{k}_{n}]", f"[b{k}_{n + 1}]"
            graph.append(f"[{n_in}:v]scale={px}:{px},setsar=1,format=rgba{ic}")
            graph.append(f"{cur}{ic}overlay={x}:{y}:eof_action=pass{nxt}")
            n_in += 1
            cur = nxt
        if len(layers) > 1:
            nxt = f"[b{k}_a]"
            graph.append(f"{cur}{_arrow(ICON_X + ICON_PX // 2, ICON_Y + ICON_PX)}{nxt}")
            cur = nxt
        pieces.append(cur)
    fades = (
        f"fade=t=in:st=0:d={fade_s},fade=t=out:st={round(dur - fade_s, 3)}:d={fade_s}"
        if fade_s > 0
        else "null"
    )
    graph.append(f"{''.join(pieces)}concat=n={len(pieces)}:v=1:a=0,{fades}[v]")
    video = ";".join(graph)
    if not has_audio:
        silent = ["-f", "lavfi", "-t", str(dur), "-i", SILENT_AUDIO]
        maps = ["-filter_complex", video, "-map", "[v]", "-map", f"{n_in}:a"]
        return [*head, *extra, *silent, *maps, *_encode_opts(), out_path]
    return [
        *head,
        *extra,
        "-filter_complex",
        f"{video};{audio_filter(mic_stream, dur=dur, fade_s=fade_s)}",
        "-map",
        "[v]",
        "-map",
        "[a]",
        *_encode_opts(),
        out_path,
    ]


def icon_paths(inserts: list[BRollInsert], items: dict[int, ItemInfo] | None) -> IconPaths:
    """Icons on disk for each insert's item (missing files are skipped)."""
    out: IconPaths = {}
    for ins in inserts:
        it = (items or {}).get(ins.item_id)
        if it is None:
            continue
        main = it.icon_path.as_posix() if it.icon_path.is_file() else None
        comps = [
            items[c].icon_path.as_posix()
            for c in it.from_ids
            if c in items and items[c].icon_path.is_file()
        ]
        out[ins.item_id] = (main, comps)
    return out


def _probe_audio_count(path: str) -> int | None:
    """Number of audio streams in `path`; None when ffprobe cannot tell (then
    the render assumes audio, as before)."""
    from ..candidates.mic_track import _ffprobe

    cmd = [_ffprobe(), "-v", "error", "-select_streams", "a", "-show_entries",
           "stream=index", "-of", "json", path]  # fmt: skip
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return None
        return len(json.loads(r.stdout or "{}").get("streams") or [])
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


# Recordings differ by 15 dB or more (Zeri 2026-11-23 came in at about
# -49 dB where Jhin was -34 dB), so every story is levelled to one
# loudness: EBU R128 at -16 LUFS, the usual target for online video.
LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"


def loudnorm_cmd(src: str, out_path: str) -> list[str]:
    """Level the audio of a finished story; the video stream is copied."""
    return [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        src,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c:v",
        "copy",
        "-af",
        LOUDNORM_FILTER,
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        out_path,
    ]


def level_audio(story: Path) -> None:
    """Replace `story` with its loudness-levelled copy. Raises StoryRenderError."""
    tmp = story.with_name(story.stem + ".level.mp4")
    ok, err = _run_ffmpeg(loudnorm_cmd(story.as_posix(), tmp.as_posix()), chapter_timeout(0, 3600))
    if not ok:
        tmp.unlink(missing_ok=True)
        raise StoryRenderError(f"audio levelling failed: {err}")
    tmp.replace(story)


def chapter_timeout(start: float, end: float) -> float:
    return 60 + 3 * (end - start)


def card_config(title: str) -> IntroConfig:
    """Text card sized so a 40-char title fits the 1920 px frame."""
    size = max(40, min(110, int(1700 / (0.6 * max(1, len(title))))))
    return IntroConfig(
        name=CARD_INTRO_NAME,
        duration=settings.story_card_seconds,
        logo=_TextLogoConfig(text=title, font_size=size, bounce_pixels=0),
    )


def card_normalize_cmd(src: str, out_path: str) -> list[str]:
    """Re-encode a rendered intro to the chapter part shape (48 kHz audio)."""
    return [
        settings.ffmpeg_path,
        "-y",
        "-i",
        src,
        "-vf",
        f"scale={WIDTH}:{HEIGHT},setsar=1,fps={FPS}",
        *_encode_opts(),
        out_path,
    ]


def _run_ffmpeg(cmd: list[str], timeout: float) -> tuple[bool, str | None]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, "ffmpeg not found"
    except subprocess.TimeoutExpired:
        return False, f"ffmpeg timed out after {timeout:.0f}s"
    if r.returncode != 0:
        return False, r.stderr[-1500:]
    return True, None


def _render_card(title: str, out_path: Path) -> None:
    res = render_intro(card_config(title))
    if not res.get("ok") or not res.get("output"):
        raise StoryRenderError(f"title card {title!r} failed: {res.get('error')}")
    ok, err = _run_ffmpeg(card_normalize_cmd(res["output"], out_path.as_posix()), CARD_TIMEOUT_S)
    if not ok:
        raise StoryRenderError(f"title card {title!r} normalise failed: {err}")


def _sfx_entries(ch: Chapter) -> list[tuple[Path, float, float]]:
    """(file, recording-second, volume) for each owner kill and death, per config."""
    entries: list[tuple[Path, float, float]] = []
    if settings.story_kill_sfx and ch.kill_times_s:
        entries += [(kaching_path(), t, 0.8) for t in ch.kill_times_s]
    if settings.story_death_sfx and ch.death_times_s:
        entries += [(death_path(), t, settings.story_death_sfx_volume) for t in ch.death_times_s]
    return entries


def _with_sfx(part: Path, ch: Chapter, i: int) -> Path:
    """Kaching on owner kills, a glass-break on owner deaths. Each lands on its
    moment (the file's leading silence is subtracted; slow-mo is mapped
    through `effect_time_map`). The mixed file replaces
    the part when it works."""
    entries = _sfx_entries(ch)
    if not entries:
        return part
    out = part.with_name(f"chapter_{i:02d}_sfx.mp4")
    # Effects stretch the part (slow-mo); a broll chapter renders without them.
    fx = [] if ch.broll else ch.effects
    tmap = effect_time_map(fx, ch.start_s, ch.end_s)
    sfx = [
        {
            "sfx_path": str(path),
            "at_seconds": max(0.0, round(tmap(t) - sfx_hit_s(path), 3)),
            "volume": vol,
        }
        for path, t, vol in entries
    ]
    ok, err = apply_sfx(part.as_posix(), out.as_posix(), sfx=sfx)
    if not ok:
        _log.warning("story: sfx failed for chapter %d: %s", i, err)
        return part
    return out


def _review(video: Path, plan: StoryPlan, out_dir: Path) -> None:
    """Vision critique at 0.25 fps under the story budget (outside the asset cap)."""
    key = f"story:{plan.asset_id}"
    try:
        check_budget(key, cap_usd=settings.story_budget_usd, cap_name="STORY_BUDGET_USD")
    except SpendCapError as e:
        _log.warning("story review skipped: %s", e)
        return
    result = watch_montage(video, fps=REVIEW_FPS, spend_key=key)
    save_critique(result, out_dir / "critique.md")


def _render_effect_chapter(
    asset_path: str,
    ch: Chapter,
    i: int,
    part: Path,
    *,
    mic_stream: int | None,
    has_audio: bool,
    fade_s: float,
) -> None:
    """Render a chapter with `effects`: one encode per effect segment (the
    chapter fade only on its own ends), stream-copy joined into `part`."""
    segs = effect_segments(ch.effects, ch.start_s, ch.end_s)
    single = len(segs) == 1
    seg_paths: list[str] = []
    for n, (a, b, active) in enumerate(segs, start=1):
        out = part if single else part.with_name(f"chapter_{i:02d}_seg{n:02d}.mp4")
        cmd = effect_segment_cmd(
            asset_path,
            a,
            b,
            out.as_posix(),
            active,
            mic_stream=mic_stream,
            has_audio=has_audio,
            fade_in_s=fade_s if n == 1 else 0.0,
            fade_out_s=fade_s if n == len(segs) else 0.0,
        )
        out_dur = (b - a) / (slowmo_factor(active) or 1.0)
        # geq (focus) is a per-pixel expression: much slower than the rest.
        slow_filter = 4 if any(fx.kind == "focus" for fx in active) else 1
        ok, err = _run_ffmpeg(cmd, chapter_timeout(0.0, out_dur) * slow_filter)
        if not ok:
            raise StoryRenderError(f"chapter {i} ({ch.title!r}) effect segment {n} failed: {err}")
        seg_paths.append(out.as_posix())
    if single:
        return
    ok, err = _concat(seg_paths, part.as_posix())
    if not ok:
        raise StoryRenderError(f"chapter {i} ({ch.title!r}) segment concat failed: {err}")


def render_story(
    asset_path: str,
    plan: StoryPlan,
    out_dir: Path,
    *,
    cards: bool = True,
    review: bool = False,
    items: dict[int, ItemInfo] | None = None,
) -> Path:
    """Write `out_dir/story.json`, `out_dir/parts/` and `out_dir/story.mp4`.
    With `review`, also `out_dir/critique.md`. Chapters carrying `broll`
    inserts render with item cutaways (icons from `items`); chapters
    carrying `effects` render them per time range (not both: broll wins).
    Raises StoryRenderError."""
    out_dir = Path(out_dir)
    parts_dir = out_dir / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "story.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")

    audio_count = _probe_audio_count(asset_path)  # probed once for every chapter
    has_audio = audio_count != 0
    mic = plan.mic_stream
    if mic is not None and audio_count is not None and mic >= audio_count:
        _log.warning(
            "story: mic stream %d not in source (%d audio); game audio only", mic, audio_count
        )
        mic = None
    use_cards = cards and plan.cards
    fade_s = 0.0 if plan.cards else settings.story_transition_s
    parts: list[str] = []
    for i, ch in enumerate(plan.chapters, start=1):
        if use_cards:
            card = parts_dir / f"card_{i:02d}.mp4"
            _render_card(ch.title, card)
            parts.append(card.as_posix())
        part = parts_dir / f"chapter_{i:02d}.mp4"
        common = {"mic_stream": mic, "has_audio": has_audio, "fade_s": fade_s}
        if ch.broll and ch.effects:
            _log.warning(
                "story: chapter %d has broll and effects; rendering broll, skipping effects", i
            )
        if ch.effects and not ch.broll:
            _render_effect_chapter(asset_path, ch, i, part, **common)
            parts.append(_with_sfx(part, ch, i).as_posix())
            continue
        if ch.broll:
            cmd = broll_chapter_cmd(
                asset_path,
                ch.start_s,
                ch.end_s,
                part.as_posix(),
                ch.broll,
                icon_paths(ch.broll, items),
                **common,
            )
        else:
            cmd = chapter_cmd(asset_path, ch.start_s, ch.end_s, part.as_posix(), **common)
        ok, err = _run_ffmpeg(cmd, chapter_timeout(ch.start_s, ch.end_s))
        if not ok:
            raise StoryRenderError(f"chapter {i} ({ch.title!r}) failed: {err}")
        parts.append(_with_sfx(part, ch, i).as_posix())

    story = out_dir / "story.mp4"
    ok, err = _concat(parts, story.as_posix())
    if not ok:
        raise StoryRenderError(f"concat failed: {err}")
    level_audio(story)
    if review:
        _review(story, plan, out_dir)
    return story
