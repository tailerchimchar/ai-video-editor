"""Pure ffmpeg argv for a narration export (the mix recipe in
docs/specs/spec-narration.md). No I/O: reads only `settings` for the binary
and encoder, so every branch is unit-testable without ffmpeg.

Input order: `-i video`, then one `-i` per take (take k is input k + 1).
The graph has two independent halves:

- audio: every take delayed to its second, mixed into the narration; the
  original audio ducks under it (sidechain) and the two are summed.
- video (optional): camera takes overlaid in the box during their window.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import settings
from .models import AudioMix, GameAudioRegion

if TYPE_CHECKING:
    from .camera_windows import CameraWindow

# Rounded-corner radius as a fraction of the camera box height.
CORNER_RADIUS_FRAC = 0.08
AUDIO_CODEC = ["-c:a", "aac", "-b:a", "192k"]


@dataclass(frozen=True)
class TakeInput:
    path: Path
    start_s: float
    duration_s: float
    has_camera: bool
    media_in_s: float = 0.0
    gain: float = 1.0


def export_cmd(
    video: Path,
    takes: list[TakeInput],
    out: Path,
    *,
    mix: AudioMix,
    game_regions: Sequence[GameAudioRegion] = (),
    camera_windows: Sequence[CameraWindow] = (),
    rounded: bool = True,
) -> list[str]:
    """argv mixing `takes` over `video` (game audio scaled by `mix.game_gain`
    and per-region levels, then ducked under the voice; one camera overlay per
    window; video stream copied when there are no windows). `rounded` masks
    the camera's corners."""
    if not takes:
        raise ValueError("no takes to mix")
    inputs = [arg for p in (video, *(t.path for t in takes)) for arg in ("-i", p.as_posix())]
    graph = [*_audio_graph(takes, mix, game_regions)]
    if camera_windows:
        graph += _camera_graph(camera_windows, rounded)
        video_out = ["-map", "[vout]", "-map", "[aout]", *_video_encode_opts()]
    else:
        video_out = ["-map", "0:v:0", "-map", "[aout]", "-c:v", "copy"]
    return [
        settings.ffmpeg_path,
        "-y",
        "-v",
        "error",
        *inputs,
        "-filter_complex",
        ";".join(graph),
        *video_out,
        *AUDIO_CODEC,
        "-movflags",
        "+faststart",
        out.as_posix(),
    ]


def _audio_graph(
    takes: list[TakeInput], mix: AudioMix, game_regions: Sequence[GameAudioRegion]
) -> list[str]:
    """Takes → [narr] (the voice) and [key] (the ducking sidechain);
    game audio [0:a] → [game] (gain, region levels), ducked by [key]
    and summed with [narr] → [aout]."""
    chains = []
    for k, take in enumerate(takes, start=1):
        ms = round(take.start_s * 1000)
        chains.append(
            f"[{k}:a]atrim=start={take.media_in_s}:duration={take.duration_s},"
            f"asetpts=PTS-STARTPTS,volume={take.gain},"
            f"aformat=channel_layouts=stereo,adelay={ms}|{ms}[t{k}]"
        )
    labels = "".join(f"[t{k}]" for k in range(1, len(takes) + 1))
    chains.append(
        f"{labels}amix=inputs={len(takes)}:normalize=0:duration=longest,"
        f"volume={mix.narration_gain},asplit=2[narr][key]"
    )
    levels = "".join(
        f",volume={r.level}:enable='between(t,{r.start_s},{r.end_s})'"
        for r in sorted(game_regions, key=lambda r: r.start_s)
    )
    chains.append(f"[0:a]volume={mix.game_gain}{levels}[game]")
    # apad keeps the key alive after the last take so the ducked original
    # is not cut short when the narration ends before the video does.
    chains.append(
        "[key]apad[keyp];"
        "[game][keyp]sidechaincompress="
        f"threshold={mix.duck_threshold}:ratio={mix.duck_ratio}:"
        f"attack={mix.duck_attack_ms}:release={mix.duck_release_ms}[ducked]"
    )
    chains.append("[ducked][narr]amix=inputs=2:normalize=0:duration=first[aout]")
    return chains


def _camera_graph(windows: Sequence[CameraWindow], rounded: bool) -> list[str]:
    """Each window: the take's slice, shifted to its timeline second, fitted
    to the window's box, optionally rounded, overlaid only while it lasts."""
    chains = []
    prev = "[0:v]"
    for n, win in enumerate(windows, start=1):
        a, b, box, take = win.start_s, win.end_s, win.box, win.take
        t0 = a - take.start_s + take.media_in_s
        t1 = b - take.start_s + take.media_in_s
        fit = (
            f"[{win.input_index}:v]trim=start={t0}:end={t1},setpts=PTS-STARTPTS+{a}/TB,"
            f"scale={box.w}:{box.h}:force_original_aspect_ratio=increase,crop={box.w}:{box.h}"
        )
        if rounded:
            fit += f",{_rounded_mask(box.h)}"
        chains.append(f"{fit}[cam{n}]")
        out = "[vout]" if n == len(windows) else f"[v{n}]"
        chains.append(
            f"{prev}[cam{n}]overlay={box.x}:{box.y}:"
            f"enable='between(t,{a},{b})':eof_action=pass{out}"
        )
        prev = out
    return chains


def _rounded_mask(box_h: int) -> str:
    """Alpha mask: pixels outside a quarter circle in each corner become
    transparent. dx/dy = distance past the straight edge of the inner
    rectangle; a pixel is kept when dx² + dy² <= r²."""
    r = max(1, round(box_h * CORNER_RADIUS_FRAC))
    dx = f"max(max({r}-X,X-(W-1-{r})),0)"
    dy = f"max(max({r}-Y,Y-(H-1-{r})),0)"
    alpha = f"if(lte({dx}*{dx}+{dy}*{dy},{r * r}),255,0)"
    return f"format=yuva420p,geq=lum='lum(X,Y)':cb='cb(X,Y)':cr='cr(X,Y)':a='{alpha}'"


def _video_encode_opts() -> list[str]:
    """The configured encoder, without the story renderer's fixed frame rate
    (a narrated video keeps its source rate)."""
    return [
        "-c:v",
        settings.ffmpeg_video_codec,
        *settings.ffmpeg_video_codec_opts.split(),
        "-pix_fmt",
        "yuv420p",
    ]
