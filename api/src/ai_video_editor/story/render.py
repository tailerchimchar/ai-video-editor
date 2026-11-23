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
from pathlib import Path

from ..anthropic_surface import SpendCapError, check_budget
from ..compile import _concat
from ..config import settings
from ..edits import apply_sfx
from ..intros.config import IntroConfig, _TextLogoConfig
from ..intros.render import render_intro
from ..sfx_library import kaching_path
from ..vlm.watch_montage import WatchResult, save_critique, watch_montage
from .planner import Chapter, StoryPlan

_log = logging.getLogger(__name__)

__all__ = ["StoryRenderError", "WatchResult", "chapter_cmd", "render_story"]

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


def _with_kill_sfx(part: Path, ch: Chapter, i: int) -> Path:
    """Kaching on each owner kill; the sfx file replaces the part when it works."""
    if not (settings.story_kill_sfx and ch.kill_times_s):
        return part
    out = part.with_name(f"chapter_{i:02d}_sfx.mp4")
    sfx = [
        {"sfx_path": str(kaching_path()), "at_seconds": round(t - ch.start_s, 3), "volume": 0.8}
        for t in ch.kill_times_s
    ]
    ok, err = apply_sfx(part.as_posix(), out.as_posix(), sfx=sfx)
    if not ok:
        _log.warning("story: kill sfx failed for chapter %d: %s", i, err)
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


def render_story(
    asset_path: str,
    plan: StoryPlan,
    out_dir: Path,
    *,
    cards: bool = True,
    review: bool = False,
) -> Path:
    """Write `out_dir/story.json`, `out_dir/parts/` and `out_dir/story.mp4`.
    With `review`, also `out_dir/critique.md`. Raises StoryRenderError."""
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
        cmd = chapter_cmd(
            asset_path,
            ch.start_s,
            ch.end_s,
            part.as_posix(),
            mic_stream=mic,
            has_audio=has_audio,
            fade_s=fade_s,
        )
        ok, err = _run_ffmpeg(cmd, chapter_timeout(ch.start_s, ch.end_s))
        if not ok:
            raise StoryRenderError(f"chapter {i} ({ch.title!r}) failed: {err}")
        parts.append(_with_kill_sfx(part, ch, i).as_posix())

    story = out_dir / "story.mp4"
    ok, err = _concat(parts, story.as_posix())
    if not ok:
        raise StoryRenderError(f"concat failed: {err}")
    if review:
        _review(story, plan, out_dir)
    return story
