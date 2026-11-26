"""Export orchestration with a fake target, store, runner and probe."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest

from ai_video_editor.config import settings
from ai_video_editor.narration import export as export_mod
from ai_video_editor.narration.export import (
    NarrationExportError,
    StaleNarrationError,
    export_narration,
)
from ai_video_editor.narration.models import (
    CameraBox,
    NarrationSession,
    PixelBox,
    Take,
    TargetRef,
)

REF = TargetRef(kind="fake", key="round1")
BOX = PixelBox(x=100, y=60, w=320, h=180)


class FakeTarget:
    kind: ClassVar[str] = "fake"

    def __init__(self, root: Path) -> None:
        self.root = root

    def video_path(self, key: str) -> Path:
        return self.root / key / "video.mp4"

    def work_dir(self, key: str) -> Path:
        return self.root / key

    def game(self, key: str) -> str:
        return "league"

    def video_url(self, key: str) -> str:
        return f"/workspace/{key}/video.mp4"


class FakeStore:
    def __init__(self, root: Path, session: NarrationSession | None) -> None:
        self.root = root
        self.session = session

    def load(self, ref: TargetRef) -> NarrationSession | None:
        return self.session

    def save(self, session: NarrationSession) -> None:
        self.session = session

    def write_take(self, ref: TargetRef, take_id: str, data: bytes, suffix: str) -> str:
        raise NotImplementedError

    def take_path(self, ref: TargetRef, media: str) -> Path:
        return self.root / ref.key / "narration" / "takes" / media

    def delete_take(self, ref: TargetRef, media: str) -> None:
        raise NotImplementedError

    def export_path(self, ref: TargetRef) -> Path:
        return self.root / ref.key / "narration" / "narrated.mp4"


class FakeRun:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.cmds: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> tuple[bool, str | None]:
        self.cmds.append(cmd)
        if self.ok:
            Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(cmd[-1]).write_bytes(b"mp4")
            return True, None
        return False, "boom"


def _take(i: int, start: float, cam: bool = False) -> Take:
    return Take(
        id=f"t{i}",
        start_s=start,
        duration_s=1.0,
        media=f"t{i}.webm",
        has_camera=cam,
        created_at="2026-11-25T00:00:00Z",
    )


def _session(*takes: Take, fp: str = "fp-1", camera: bool = False) -> NarrationSession:
    return NarrationSession(
        target=REF,
        game="league",
        video_fingerprint=fp,
        takes=list(takes),
        camera=CameraBox(enabled=camera),
    )


def _export(tmp_path, session, *, fp="fp-1", force=False, run=None, box_calls=None, probes=None):
    run = run if run is not None else FakeRun()

    def box_for(camera, game, w, h):
        if box_calls is not None:
            box_calls.append((camera, game, w, h))
        return BOX

    def probe(path):
        if probes is not None:
            probes.append(path)
        return (1920, 1080)

    out = export_narration(
        REF,
        target=FakeTarget(tmp_path),
        store=FakeStore(tmp_path, session),
        force=force,
        run=run,
        fingerprint=lambda p: fp,
        probe=probe,
        box_for=box_for,
    )
    return out, run


def test_export_runs_ffmpeg_and_returns_the_store_export_path(tmp_path):
    out, run = _export(tmp_path, _session(_take(1, 1.0), _take(2, 4.0)))
    assert out == tmp_path / "round1" / "narration" / "narrated.mp4"
    assert out.read_bytes() == b"mp4"
    (cmd,) = run.cmds
    inputs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]
    assert inputs == [
        (tmp_path / "round1" / "video.mp4").as_posix(),
        (tmp_path / "round1" / "narration" / "takes" / "t1.webm").as_posix(),
        (tmp_path / "round1" / "narration" / "takes" / "t2.webm").as_posix(),
    ]
    assert "adelay=4000|4000" in cmd[cmd.index("-filter_complex") + 1]


def test_takes_are_mixed_in_time_order(tmp_path):
    _, run = _export(tmp_path, _session(_take(2, 4.0), _take(1, 1.0)))
    inputs = [a for i, a in enumerate(run.cmds[0]) if i and run.cmds[0][i - 1] == "-i"]
    assert inputs[1].endswith("t1.webm") and inputs[2].endswith("t2.webm")


def test_no_session_or_no_takes_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="no takes"):
        _export(tmp_path, _session())
    with pytest.raises(ValueError, match="no takes"):
        _export(tmp_path, None)


def test_stale_video_is_refused(tmp_path):
    run = FakeRun()
    with pytest.raises(StaleNarrationError):
        _export(tmp_path, _session(_take(1, 1.0), fp="fp-old"), fp="fp-new", run=run)
    assert run.cmds == []


def test_force_exports_a_stale_video(tmp_path):
    out, run = _export(tmp_path, _session(_take(1, 1.0), fp="fp-old"), fp="fp-new", force=True)
    assert len(run.cmds) == 1 and out.exists()


def test_camera_disabled_uses_no_box_and_never_probes(tmp_path):
    calls, probes = [], []
    _, run = _export(tmp_path, _session(_take(1, 1.0, cam=True)), box_calls=calls, probes=probes)
    assert calls == [] and probes == []
    cmd = run.cmds[0]
    assert cmd[cmd.index("-c:v") + 1] == "copy"


def test_camera_enabled_places_the_box_from_the_probed_frame(tmp_path):
    calls, probes = [], []
    session = _session(_take(1, 1.0, cam=True), camera=True)
    _, run = _export(tmp_path, session, box_calls=calls, probes=probes)
    assert probes == [tmp_path / "round1" / "video.mp4"]
    assert calls == [(session.camera, "league", 1920, 1080)]
    fc = run.cmds[0][run.cmds[0].index("-filter_complex") + 1]
    assert "overlay=100:60:" in fc


def test_camera_enabled_without_camera_takes_skips_the_probe(tmp_path):
    probes = []
    _export(tmp_path, _session(_take(1, 1.0), camera=True), probes=probes)
    assert probes == []


def test_ffmpeg_failure_raises_and_leaves_no_output(tmp_path):
    with pytest.raises(NarrationExportError, match="boom"):
        _export(tmp_path, _session(_take(1, 1.0)), run=FakeRun(ok=False))
    assert not (tmp_path / "round1" / "narration" / "narrated.mp4").exists()


def test_default_box_for_delegates_to_layout_lazily(monkeypatch):
    import sys
    import types

    seen = []
    fake_layout = types.ModuleType("ai_video_editor.narration.layout")

    def camera_box(camera, game, frame_w, frame_h):
        seen.append((game, frame_w, frame_h))
        return BOX

    fake_layout.camera_box = camera_box
    monkeypatch.setitem(sys.modules, "ai_video_editor.narration.layout", fake_layout)
    assert export_mod.layout_box(CameraBox(enabled=True), "league", 1280, 720) == BOX
    assert seen == [("league", 1280, 720)]


# --- real ffmpeg: the whole export on generated media ---

REAL_BOX = PixelBox(x=400, y=200, w=160, h=90)


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def _frame(path: Path, t: float, w: int = 640, h: int = 360) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(t), "-i", path.as_posix(), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    return np.frombuffer(raw, np.uint8)[: w * h * 3].reshape(h, w, 3).astype(np.int16)


def _tone_energy(path: Path, hz: float, win_s: float = 0.02, sr: int = 48000) -> np.ndarray:
    """Per-window energy of one frequency (single-bin DFT) in the mono mix."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path.as_posix(), "-vn", "-ac", "1", "-ar", str(sr),
         "-f", "f32le", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    pcm = np.frombuffer(raw, np.float32)
    n = int(sr * win_s)
    frames = pcm[: len(pcm) // n * n].reshape(-1, n)
    basis = np.exp(-2j * np.pi * hz * np.arange(n) / sr)
    return np.abs(frames @ basis) / n


@pytest.mark.real_ffmpeg
@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg"
)
def test_real_ffmpeg_export_mixes_voice_and_camera(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ffmpeg_path", "ffmpeg")
    root = tmp_path / "round1"
    takes_dir = root / "narration" / "takes"
    takes_dir.mkdir(parents=True)
    video = root / "video.mp4"
    _ff("-f", "lavfi", "-i", "testsrc=size=640x360:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "6", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", video.as_posix())  # fmt: skip
    _ff("-f", "lavfi", "-i", "color=c=red:size=320x240:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
        "-t", "1.5", "-c:v", "libvpx", "-c:a", "libopus",
        (takes_dir / "t1.webm").as_posix())  # fmt: skip
    _ff("-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000",
        "-t", "1.0", "-c:a", "libopus", (takes_dir / "t2.webm").as_posix())  # fmt: skip
    session = NarrationSession(
        target=REF,
        video_fingerprint="fp",
        takes=[
            Take(id="t1", start_s=1.0, duration_s=1.5, media="t1.webm", has_camera=True,
                 created_at="2026-11-25T00:00:00Z"),
            Take(id="t2", start_s=4.0, duration_s=1.0, media="t2.webm",
                 created_at="2026-11-25T00:00:00Z"),
        ],
        camera=CameraBox(enabled=True),
    )  # fmt: skip

    out = export_narration(
        REF,
        target=FakeTarget(tmp_path),
        store=FakeStore(tmp_path, session),
        fingerprint=lambda p: "fp",
        box_for=lambda camera, game, w, h: REAL_BOX,
    )

    duration = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
         out.as_posix()],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert float(duration) == pytest.approx(6.0, abs=0.15)

    b = REAL_BOX
    inner = (slice(b.y + 15, b.y + b.h - 15), slice(b.x + 15, b.x + b.w - 15))
    during = np.abs(_frame(out, 1.5)[inner] - _frame(video, 1.5)[inner]).mean()
    after = np.abs(_frame(out, 3.0)[inner] - _frame(video, 3.0)[inner]).mean()
    assert during > 40, during
    assert after < 8, after

    energy = _tone_energy(out, 1000.0)
    onset = np.argmax(energy > energy.max() / 2) * 0.02
    assert onset == pytest.approx(1.0, abs=0.1)
    # The original keeps playing (un-ducked) after the last take ends.
    game = _tone_energy(out, 440.0)
    tail, quiet = game[275:295].mean(), game[30:45].mean()
    assert tail > 0.5 * game.max(), (tail, game.max())
    # ...and is ducked while the owner talks (1.6-1.9 s, after the attack).
    assert game[80:95].mean() < 0.5 * tail, (game[80:95].mean(), tail)
    assert quiet > 0.5 * tail
    print(f"duration={float(duration):.3f}s cam_diff@1.5={during:.1f} "
          f"cam_diff@3.0={after:.1f} 1kHz_onset={onset:.2f}s "
          f"440Hz_ducked/tail={game[80:95].mean() / tail:.2f}")  # fmt: skip
