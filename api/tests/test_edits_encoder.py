import pytest

from ai_video_editor import edits
from ai_video_editor.config import settings


@pytest.fixture
def argv(monkeypatch):
    monkeypatch.setattr(settings, "ffmpeg_video_codec_opts", "-preset slow -crf 18")
    seen: list[list[str]] = []
    monkeypatch.setattr(edits, "_run_ffmpeg", lambda cmd: seen.append(cmd) or (True, None))
    return seen


SEGS = [{"start_seconds": 0.0, "end_seconds": 1.0, "text": "hi"}]


@pytest.mark.parametrize("aspect", ["16:9", "9:16", edits.HUD_ASPECT])
def test_edits_use_configured_codec_opts(argv, aspect):
    edits.apply_caption("in.mp4", "out.mp4", start=0.0, end=2.0, segments=SEGS, aspect=aspect)
    cmd = argv[0]
    assert "slow" in cmd and "18" in cmd
    assert "veryfast" not in cmd and "20" not in cmd


def test_zoom_and_focus_use_configured_codec_opts(argv):
    edits.apply_zoom(
        "in.mp4", "out.mp4", start=0.0, end=2.0, factor=1.5, roi="center", aspect="16:9"
    )
    edits.apply_focus(
        "in.mp4",
        "out.mp4",
        start=0.0,
        end=2.0,
        x_frac=0.5,
        y_frac=0.5,
        r_frac=0.2,
        dim=0.5,
        aspect="9:16",
    )
    assert len(argv) == 2
    for cmd in argv:
        assert "slow" in cmd and "18" in cmd
