"""Pure argv tests for the narration export mix (no ffmpeg needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_video_editor.config import settings
from ai_video_editor.narration.camera_windows import CameraWindow
from ai_video_editor.narration.mix import TakeInput, export_cmd
from ai_video_editor.narration.models import AudioMix, GameAudioRegion, PixelBox

VIDEO = Path("story.mp4")
OUT = Path("narrated.mp4")
BOX = PixelBox(x=1500, y=560, w=320, h=180)


def _take(name: str, start: float, dur: float, cam: bool = False) -> TakeInput:
    return TakeInput(path=Path(name), start_s=start, duration_s=dur, has_camera=cam)


def _win(take: TakeInput, k: int, a: float, b: float, box: PixelBox = BOX) -> CameraWindow:
    return CameraWindow(input_index=k, take=take, start_s=a, end_s=b, box=box)


def _graph(cmd: list[str]) -> str:
    return cmd[cmd.index("-filter_complex") + 1]


def _inputs(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]


def _maps(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]


# --- audio graph ---


def test_zero_takes_is_an_error():
    with pytest.raises(ValueError):
        export_cmd(VIDEO, [], OUT, mix=AudioMix())


def test_inputs_are_the_video_then_each_take_and_out_is_last():
    takes = [_take("a.webm", 1.0, 2.0), _take("b.webm", 4.0, 1.0)]
    cmd = export_cmd(VIDEO, takes, OUT, mix=AudioMix())
    assert cmd[0] == settings.ffmpeg_path
    assert _inputs(cmd) == ["story.mp4", "a.webm", "b.webm"]
    assert cmd[-1] == "narrated.mp4"


def test_each_take_is_delayed_to_its_second_in_ms():
    takes = [_take("a.webm", 1.0, 2.0), _take("b.webm", 4.25, 1.0)]
    fc = _graph(export_cmd(VIDEO, takes, OUT, mix=AudioMix()))
    assert "[1:a]" in fc and "adelay=1000|1000" in fc
    assert "[2:a]" in fc and "adelay=4250|4250" in fc
    assert "amix=inputs=2:normalize=0" in fc


def test_narration_gain_then_split_into_voice_and_key():
    mix = AudioMix(narration_gain=1.5)
    fc = _graph(export_cmd(VIDEO, [_take("a.webm", 0.0, 1.0)], OUT, mix=mix))
    assert "volume=1.5" in fc
    assert "asplit=2[narr][key]" in fc


def test_original_audio_is_ducked_by_the_voice_with_audiomix_params():
    mix = AudioMix(duck_threshold=0.05, duck_ratio=6.0, duck_attack_ms=30.0, duck_release_ms=500.0)
    fc = _graph(export_cmd(VIDEO, [_take("a.webm", 0.0, 1.0)], OUT, mix=mix))
    assert "[0:a]" in fc and "sidechaincompress=" in fc
    assert "threshold=0.05" in fc and "ratio=6.0" in fc
    assert "attack=30.0" in fc and "release=500.0" in fc
    assert "amix=inputs=2:normalize=0:duration=first[aout]" in fc


def test_the_key_is_padded_so_the_original_outlives_the_last_take():
    fc = _graph(export_cmd(VIDEO, [_take("a.webm", 0.0, 1.0)], OUT, mix=AudioMix()))
    assert "[key]apad[keyp]" in fc and "[game][keyp]sidechaincompress" in fc


def test_audio_only_path_copies_the_video_stream():
    cmd = export_cmd(VIDEO, [_take("a.webm", 1.0, 2.0)], OUT, mix=AudioMix())
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert _maps(cmd) == ["0:v:0", "[aout]"]
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    assert cmd[cmd.index("-b:a") + 1] == "192k"
    assert cmd[cmd.index("-movflags") + 1] == "+faststart"


def test_camera_takes_without_windows_copy_the_video():
    takes = [_take("a.webm", 1.0, 2.0, cam=True)]
    cmd = export_cmd(VIDEO, takes, OUT, mix=AudioMix())
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert "overlay" not in _graph(cmd)


# --- camera graph ---


def test_camera_path_reencodes_with_the_settings_encoder(monkeypatch):
    monkeypatch.setattr(settings, "ffmpeg_video_codec", "libx264")
    monkeypatch.setattr(settings, "ffmpeg_video_codec_opts", "-preset veryfast -crf 20")
    takes = [_take("a.webm", 1.0, 1.5, cam=True)]
    cmd = export_cmd(
        VIDEO, takes, OUT, mix=AudioMix(), camera_windows=[_win(takes[0], 1, 1.0, 2.5)]
    )
    i = cmd.index("-c:v")
    assert cmd[i : i + 6] == ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
    assert _maps(cmd) == ["[vout]", "[aout]"]


def test_camera_take_is_shifted_scaled_and_overlaid_in_its_window():
    takes = [_take("a.webm", 1.0, 1.5, cam=True)]
    wins = [_win(takes[0], 1, 1.0, 2.5)]
    fc = _graph(export_cmd(VIDEO, takes, OUT, mix=AudioMix(), camera_windows=wins))
    assert "[1:v]trim=start=0.0:end=1.5,setpts=PTS-STARTPTS+1.0/TB" in fc
    assert "scale=320:180:force_original_aspect_ratio=increase,crop=320:180" in fc
    assert "overlay=1500:560:enable='between(t,1.0,2.5)':eof_action=pass" in fc


def test_only_camera_takes_are_overlaid_and_overlays_chain():
    takes = [
        _take("a.webm", 1.0, 1.0, cam=True),
        _take("b.webm", 3.0, 1.0),
        _take("c.webm", 5.0, 2.0, cam=True),
    ]
    wins = [_win(takes[0], 1, 1.0, 2.0), _win(takes[2], 3, 5.0, 7.0)]
    fc = _graph(export_cmd(VIDEO, takes, OUT, mix=AudioMix(), camera_windows=wins))
    assert "[1:v]" in fc and "[3:v]" in fc and "[2:v]" not in fc
    assert fc.count("overlay=") == 2
    assert "enable='between(t,1.0,2.0)'" in fc
    assert "enable='between(t,5.0,7.0)'" in fc
    assert fc.index("[0:v]") < fc.index("enable='between(t,1.0")
    assert "[vout]" in fc


def test_rounded_corners_add_an_alpha_mask_by_default():
    takes = [_take("a.webm", 1.0, 1.0, cam=True)]
    wins = [_win(takes[0], 1, 1.0, 2.0)]
    fc = _graph(export_cmd(VIDEO, takes, OUT, mix=AudioMix(), camera_windows=wins))
    assert "format=yuva420p,geq=" in fc
    # radius ~ 8% of the box height (180 px -> 14 px)
    assert "14" in fc[fc.index("geq=") :]


def test_square_corners_skip_the_mask():
    takes = [_take("a.webm", 1.0, 1.0, cam=True)]
    wins = [_win(takes[0], 1, 1.0, 2.0)]
    cmd = export_cmd(VIDEO, takes, OUT, mix=AudioMix(), camera_windows=wins, rounded=False)
    assert "geq=" not in _graph(cmd)


# --- v2: take chains, game regions, windows ---


def test_take_chain_trims_gains_and_delays():
    take = TakeInput(Path("a.webm"), 12.0, 5.0, False, media_in_s=1.5, gain=0.8)
    cmd = export_cmd(VIDEO, [take], OUT, mix=AudioMix())
    assert (
        "[1:a]atrim=start=1.5:duration=5.0,asetpts=PTS-STARTPTS,volume=0.8,"
        "aformat=channel_layouts=stereo,adelay=12000|12000[t1]" in _graph(cmd)
    )


def test_game_gain_and_regions_precede_the_sidechain():
    cmd = export_cmd(
        VIDEO,
        [_take("a.webm", 0, 1)],
        OUT,
        mix=AudioMix(game_gain=0.5),
        game_regions=[GameAudioRegion(id="r1", start_s=10, end_s=20, level=0.3)],
    )
    g = _graph(cmd)
    assert "[0:a]volume=0.5,volume=0.3:enable='between(t,10.0,20.0)'[game]" in g
    assert "[game][keyp]sidechaincompress" in g


def test_game_chain_is_always_emitted_and_regions_sorted():
    g = _graph(export_cmd(VIDEO, [_take("a.webm", 0, 1)], OUT, mix=AudioMix()))
    assert "[0:a]volume=1.0[game]" in g
    regions = [
        GameAudioRegion(id="b", start_s=30, end_s=40, level=0.1),
        GameAudioRegion(id="a", start_s=10, end_s=20, level=0.3),
    ]
    cmd = export_cmd(VIDEO, [_take("a.webm", 0, 1)], OUT, mix=AudioMix(), game_regions=regions)
    g = _graph(cmd)
    assert g.index("between(t,10.0,20.0)") < g.index("between(t,30.0,40.0)")


def test_camera_windows_one_overlay_each():
    take = _take("a.webm", 10.0, 30.0, cam=True)
    small = PixelBox(x=10, y=20, w=100, h=50)
    wins = [_win(take, 1, 10.0, 15.0), _win(take, 1, 20.0, 25.0, small)]
    fc = _graph(export_cmd(VIDEO, [take], OUT, mix=AudioMix(), camera_windows=wins))
    assert "[1:v]trim=start=0.0:end=5.0,setpts=PTS-STARTPTS+10.0/TB,scale=320:180" in fc
    assert "[1:v]trim=start=10.0:end=15.0,setpts=PTS-STARTPTS+20.0/TB,scale=100:50" in fc
    assert "[cam1]" in fc and "[cam2]" in fc
    assert "overlay=1500:560:enable='between(t,10.0,15.0)':eof_action=pass" in fc
    assert "overlay=10:20:enable='between(t,20.0,25.0)':eof_action=pass[vout]" in fc


def test_window_trim_uses_media_in():
    take = TakeInput(Path("a.webm"), 10.0, 30.0, True, media_in_s=2.0)
    wins = [_win(take, 1, 15.0, 20.0)]
    fc = _graph(export_cmd(VIDEO, [take], OUT, mix=AudioMix(), camera_windows=wins))
    assert "trim=start=7.0:end=12.0" in fc
