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

from dataclasses import dataclass
from pathlib import Path

from ..config import settings
from .models import AudioMix, PixelBox

# Rounded-corner radius as a fraction of the camera box height.
CORNER_RADIUS_FRAC = 0.08
AUDIO_CODEC = ["-c:a", "aac", "-b:a", "192k"]


@dataclass(frozen=True)
class TakeInput:
    path: Path
    start_s: float
    duration_s: float
    has_camera: bool


def export_cmd(
    video: Path,
    takes: list[TakeInput],
    out: Path,
    *,
    mix: AudioMix,
    camera_box: PixelBox | None,
    rounded: bool = True,
) -> list[str]:
    """argv mixing `takes` over `video` (ducked original audio; camera
    overlay at `camera_box` for takes with camera when the box is given;
    video stream copied when nothing is overlaid). `rounded` masks the
    camera's corners."""
    if not takes:
        raise ValueError("no takes to mix")
    inputs = [arg for p in (video, *(t.path for t in takes)) for arg in ("-i", p.as_posix())]
    graph = [*_audio_graph(takes, mix)]
    camera = [(k + 1, t) for k, t in enumerate(takes) if t.has_camera]
    if camera_box is not None and camera:
        graph += _camera_graph(camera, camera_box, rounded)
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


def _audio_graph(takes: list[TakeInput], mix: AudioMix) -> list[str]:
    """Takes → [narr] (the voice) and [key] (the ducking sidechain);
    original [0:a] ducked by [key] and summed with [narr] → [aout]."""
    chains = []
    for k, take in enumerate(takes, start=1):
        ms = round(take.start_s * 1000)
        chains.append(f"[{k}:a]aformat=channel_layouts=stereo,adelay={ms}|{ms}[t{k}]")
    labels = "".join(f"[t{k}]" for k in range(1, len(takes) + 1))
    chains.append(
        f"{labels}amix=inputs={len(takes)}:normalize=0:duration=longest,"
        f"volume={mix.narration_gain},asplit=2[narr][key]"
    )
    # apad keeps the key alive after the last take so the ducked original
    # is not cut short when the narration ends before the video does.
    chains.append(
        "[key]apad[keyp];"
        "[0:a][keyp]sidechaincompress="
        f"threshold={mix.duck_threshold}:ratio={mix.duck_ratio}:"
        f"attack={mix.duck_attack_ms}:release={mix.duck_release_ms}[ducked]"
    )
    chains.append("[ducked][narr]amix=inputs=2:normalize=0:duration=first[aout]")
    return chains


def _camera_graph(camera: list[tuple[int, TakeInput]], box: PixelBox, rounded: bool) -> list[str]:
    """Each camera take shifted to its second, fitted to the box, optionally
    rounded, then overlaid on the running video only during its window."""
    chains = []
    prev = "[0:v]"
    for n, (k, take) in enumerate(camera, start=1):
        fit = (
            f"[{k}:v]setpts=PTS-STARTPTS+{take.start_s}/TB,"
            f"scale={box.w}:{box.h}:force_original_aspect_ratio=increase,crop={box.w}:{box.h}"
        )
        if rounded:
            fit += f",{_rounded_mask(box.h)}"
        chains.append(f"{fit}[cam{n}]")
        out = "[vout]" if n == len(camera) else f"[v{n}]"
        end = take.start_s + take.duration_s
        chains.append(
            f"{prev}[cam{n}]overlay={box.x}:{box.y}:"
            f"enable='between(t,{take.start_s},{end})':eof_action=pass{out}"
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
