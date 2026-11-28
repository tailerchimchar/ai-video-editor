"""Hook (cold open) and outro for a story-join. Owner 2026-11-27.

Hook: one short clip per pick (a game's chapter), cut just before the play's
outcome lands (the owner's first kill or death in it). The last frame
freezes, blurs and darkens (optionally with a teaser text), so the payoff is
hidden until the viewer reaches the full chapter later. The first clip also
carries the opening line ("Want to know the trick to laning?"). Each clip is
its own mp4, so story-join swipes (with the whoosh) between hook clips and
into game 1 like between games.

Outro: the last game's final frame, blurred and darkened, with "thanks for
watching" and the like & subscribe animation large in the centre, over the
owner's outro music when WORKSPACE/music/outro.* exists (the owner supplies
it; it never goes to Drive or Supabase). Silent otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import settings
from ..edits import _escape_drawtext, _escape_fontfile_path
from .planner import StoryPlan
from .render import (
    FPS,
    HEIGHT,
    LOUDNORM_FILTER,
    WIDTH,
    StoryRenderError,
    _encode_opts,
    _run_ffmpeg,
)
from .suggest.placement import video_second

HOOK_LEAD_S = 6.0  # footage before the outcome
HOOK_CUT_BEFORE_S = 0.25  # stop this long before the kill / death lands
HOOK_MIN_S = 2.0
FREEZE_S = 1.4  # frozen, blurred tail with the teaser text
BLUR_RAMP_S = 0.25
TITLE_S = 2.8  # the opening line stays this long
DEFAULT_FREEZE_TEXT = ""  # owner 2026-11-27: no teaser text by default
OUTRO_S = 8.0
OUTRO_SUB_AT_S = 0.6
OUTRO_SUB_WIDTH = 960  # px, the subscribe animation in the outro
# The animation fades out from 4.65 s (make_subscribe_anim.py); the outro
# holds its "subscribed" frame from here instead.
OUTRO_SUB_HOLD_S = 4.6
OUTRO_FRAME_BEFORE_END_S = 3.0  # stories fade to black at the very end
OUTRO_TEXT = "THANKS FOR WATCHING"
MUSIC_EXTS = (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".opus")
_FONTS = ("C:/Windows/Fonts/impact.ttf", "C:/Windows/Fonts/arialbd.ttf")


@dataclass(frozen=True)
class HookPick:
    """Game `game` (1-based, join order), chapter `chapter` (1-based, as the
    story lists them); `text` pops on the frozen frame."""

    game: int
    chapter: int
    text: str = DEFAULT_FREEZE_TEXT


@dataclass(frozen=True)
class HookClip:
    story: Path
    start_s: float  # in story.mp4
    end_s: float
    text: str


def parse_pick(spec: str) -> HookPick:
    """`3:2` or `3:2:2v3?` -> HookPick(3, 2, text)."""
    parts = spec.split(":", 2)
    try:
        game, chapter = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        raise ValueError(f"hook pick must be GAME:CHAPTER[:TEXT], got {spec!r}") from None
    text = parts[2] if len(parts) > 2 and parts[2] else DEFAULT_FREEZE_TEXT
    return HookPick(game, chapter, text)


def hook_window(plan: StoryPlan, chapter_index: int, card_s: float) -> tuple[float, float]:
    """(start, end) in story.mp4 seconds: up to HOOK_LEAD_S of the chapter,
    ending HOOK_CUT_BEFORE_S before its first owner kill or death. A chapter
    with neither keeps its first HOOK_LEAD_S."""
    ch = plan.chapters[chapter_index]
    start = video_second(plan, chapter_index, ch.start_s, card_s)
    stop = video_second(plan, chapter_index, ch.end_s, card_s)
    assert start is not None and stop is not None
    events = sorted(t for t in ch.kill_times_s + ch.death_times_s if ch.start_s <= t <= ch.end_s)
    if events:
        hit = video_second(plan, chapter_index, events[0], card_s)
        assert hit is not None
        end = hit - HOOK_CUT_BEFORE_S
    else:
        end = min(stop, start + HOOK_LEAD_S)
    begin = max(start, end - HOOK_LEAD_S)
    if end - begin < HOOK_MIN_S:  # outcome right at the chapter start: borrow lead-in
        begin = max(0.0, end - HOOK_LEAD_S)
    if end - begin < HOOK_MIN_S:
        raise StoryRenderError(f"chapter {chapter_index + 1} is too short for a hook")
    return round(begin, 2), round(end, 2)


def font_path() -> str:
    for f in _FONTS:
        if Path(f).is_file():
            return f
    raise StoryRenderError("no hook font found (impact.ttf / arialbd.ttf)")


def _text(text: str, *, size: int, y: str, at: float, until: float | None, font: str) -> str:
    """drawtext that pops in at `at` (1.5x -> 1x over 0.15 s)."""
    pop = f"{size}*(1+0.5*max(0\\,1-(t-{at})/0.15))"
    enable = f"gte(t\\,{at})" if until is None else f"between(t\\,{at}\\,{until})"
    return (
        f"drawtext=fontfile='{_escape_fontfile_path(font)}':text='{_escape_drawtext(text)}'"
        f":fontsize='{pop}':fontcolor=white:borderw={max(4, size // 14)}:bordercolor=black"
        f":shadowx=4:shadowy=4:shadowcolor=black@0.6:x=(w-tw)/2:y={y}:enable='{enable}'"
    )


def hook_cmd(clip: HookClip, out: str, *, title: str | None, font: str) -> list[str]:
    """One hook clip: play, then freeze + blur ramp + teaser text."""
    d = round(clip.end_s - clip.start_s, 3)
    total = round(d + FREEZE_S, 3)
    v = [
        f"[0:v]fps={FPS},scale={WIDTH}:{HEIGHT},setsar=1,format=yuv420p,"
        f"tpad=stop_mode=clone:stop_duration={FREEZE_S},split[p][q]",
        "[q]gblur=sigma=28,eq=brightness=-0.12:saturation=0.6[qb]",
        f"[p][qb]blend=all_expr='A+(B-A)*clip((T-{d})/{BLUR_RAMP_S}\\,0\\,1)'[f]",
    ]
    texts = []
    if clip.text:
        texts.append(_text(clip.text, size=200, y="(h-th)/2", at=d + 0.05, until=None, font=font))
    if title:
        texts.append(_text(title, size=104, y="h*0.14", at=0.15, until=TITLE_S, font=font))
    v.append(f"[f]{','.join(texts)}[v]" if texts else "[f]null[v]")
    a = (
        "[0:a]aresample=48000,aformat=channel_layouts=stereo,"
        f"afade=t=out:st={max(0.0, d - 0.25)}:d=0.25,apad=whole_dur={total}[a]"
    )
    return [
        settings.ffmpeg_path,
        "-y",
        "-ss",
        f"{clip.start_s}",
        "-t",
        f"{d}",
        "-i",
        clip.story.as_posix(),
        "-filter_complex",
        ";".join([*v, a]),
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-t",
        f"{total}",
        *_encode_opts(),
        out,
    ]


def music_asset() -> Path | None:
    """WORKSPACE/music/outro.<ext>, supplied by the owner (local only)."""
    root = Path(settings.workspace_dir) / "music"
    for ext in MUSIC_EXTS:
        p = root / f"outro{ext}"
        if p.is_file():
            return p
    return None


def outro_cmd(
    last_story: Path,
    out: str,
    *,
    subscribe: Path | None,
    music: Path | None,
    music_start_s: float = 0.0,
    font: str,
) -> list[str]:
    """End card from the last game's final frame (see the module doc)."""
    frames = round(OUTRO_S * FPS)
    inputs = ["-sseof", f"-{OUTRO_FRAME_BEFORE_END_S}", "-i", last_story.as_posix()]
    g = [
        f"[0:v]trim=end_frame=1,setpts=PTS-STARTPTS,scale={WIDTH}:{HEIGHT},setsar=1,"
        f"loop=loop={frames}:size=1:start=0,setpts=N/{FPS}/TB,fps={FPS},"
        "gblur=sigma=20,eq=brightness=-0.22:saturation=0.7,format=yuv420p[bg]"
    ]
    v = "[bg]"
    n = 1
    if subscribe is not None:
        inputs += ["-i", subscribe.as_posix()]
        g.append(
            f"[{n}:v]trim=end={OUTRO_SUB_HOLD_S},setpts=PTS-STARTPTS,"
            f"tpad=stop_mode=clone:stop_duration={OUTRO_S},"
            f"setpts=PTS+{OUTRO_SUB_AT_S}/TB,scale={OUTRO_SUB_WIDTH}:-2[s]"
        )
        g.append(f"{v}[s]overlay=x=(W-w)/2:y=(H-h)/2+90:eof_action=repeat[vs]")
        v, n = "[vs]", n + 1
    g.append(f"{v}{_text(OUTRO_TEXT, size=120, y='h*0.16', at=0.2, until=None, font=font)}[v]")
    if music is not None:
        inputs += ["-ss", f"{music_start_s}", "-t", f"{OUTRO_S}", "-i", music.as_posix()]
        g.append(
            # Same loudness as the stories (render.LOUDNORM_FILTER): released
            # tracks are mastered far louder than a levelled game mix.
            f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo,{LOUDNORM_FILTER},"
            "aresample=48000,afade=t=in:d=0.4,"
            f"afade=t=out:st={OUTRO_S - 1.5}:d=1.5,apad=whole_dur={OUTRO_S}[a]"
        )
    else:
        g.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={OUTRO_S}[a]")
    return [
        settings.ffmpeg_path,
        "-y",
        *inputs,
        "-filter_complex",
        ";".join(g),
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-t",
        f"{OUTRO_S}",
        *_encode_opts(),
        out,
    ]


def render_hook(clips: list[HookClip], work: Path, *, title: str | None) -> list[Path]:
    """Render each hook clip to `work/hook_<n>.mp4`; the opening line rides
    on the first."""
    work.mkdir(parents=True, exist_ok=True)
    font = font_path()
    outs: list[Path] = []
    for i, clip in enumerate(clips, 1):
        out = work / f"hook_{i}.mp4"
        cmd = hook_cmd(clip, out.as_posix(), title=title if i == 1 else None, font=font)
        ok, err = _run_ffmpeg(cmd, 300)
        if not ok:
            raise StoryRenderError(f"hook clip {i} failed: {err}")
        outs.append(out)
    return outs


def render_outro(
    last_story: Path,
    work: Path,
    *,
    subscribe: Path | None,
    music: Path | None,
    music_start_s: float = 0.0,
) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    out = work / "outro.mp4"
    cmd = outro_cmd(
        last_story,
        out.as_posix(),
        subscribe=subscribe,
        music=music,
        music_start_s=music_start_s,
        font=font_path(),
    )
    ok, err = _run_ffmpeg(cmd, 300)
    if not ok:
        raise StoryRenderError(f"outro failed: {err}")
    return out
