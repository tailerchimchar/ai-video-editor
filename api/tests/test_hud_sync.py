from types import SimpleNamespace

import numpy as np
import pytest

from ai_video_editor.league.hud_sync import HudSync, calibrate_from_hud, change_time, kda_frames
from ai_video_editor.league.riot_timeline import RiotTimeline, TimelineEvent

FPS = 10.0


def _frames(n, flip_at=None, h=20, w=60, noise=False, seed=0):
    rng = np.random.default_rng(seed)
    base = np.zeros((n, h, w), np.uint8)
    if noise:
        base = rng.integers(0, 150, size=(n, h, w), dtype=np.uint8)
    if flip_at is not None:
        base[flip_at:, 5:15, 10:30] = 250
    return base


def test_change_time_clean_flip():
    f = _frames(160, flip_at=80)
    assert change_time(f, 100.0, FPS) == pytest.approx(100.0 + 80 / FPS)


def test_change_time_noise_without_flip_is_none():
    assert change_time(_frames(160, noise=True), 100.0, FPS) is None


def test_change_time_flip_at_window_edge():
    f = _frames(160, flip_at=1)
    assert change_time(f, 0.0, FPS) == pytest.approx(0.1)
    f = _frames(160, flip_at=159)
    assert change_time(f, 0.0, FPS) == pytest.approx(15.9)


def test_change_time_too_few_pixels_or_frames():
    f = np.zeros((50, 20, 60), np.uint8)
    f[25:, 0:2, 0:3] = 255  # 6 px < 8
    assert change_time(f, 0.0, FPS) is None
    assert change_time(f[:1], 0.0, FPS) is None


def test_kda_frames_argv_and_shape():
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[0] == "ffprobe":
            return SimpleNamespace(stdout="1280x720\n", returncode=0)
        return SimpleNamespace(stdout=bytes(3 * 76 * 21), returncode=0)

    out = kda_frames("v.mp4", 12.5, 4.0, fps=10.0, run=fake_run)
    assert out.shape == (3, 21, 76) and out.dtype == np.uint8
    ff = calls[1]
    assert ff[ff.index("-ss") + 1] == "12.5" and ff[ff.index("-t") + 1] == "4.0"
    assert ff[ff.index("-i") + 1] == "v.mp4"
    vf = ff[ff.index("-vf") + 1]
    assert vf == "fps=10.0,crop=76:21:1081:0,format=gray"
    assert "rawvideo" in ff and ff[-1] == "pipe:"


def test_kda_frames_ffmpeg_failure_returns_empty():
    def fake_run(argv, **kw):
        if argv[0] == "ffprobe":
            return SimpleNamespace(stdout="1280x720\n", returncode=0)
        return SimpleNamespace(stdout=b"", returncode=1)

    assert kda_frames("v.mp4", 0, 1, run=fake_run).shape[0] == 0


def _ev(kind, t):
    return TimelineEvent(kind=kind, t_game_s=t, t_rec_s=None, actors=[], detail={})


def _tl(events, offset=-20.0):
    return RiotTimeline(
        match_id="m",
        confidence="high",
        offset_s=offset,
        game_end_s=None,
        events=events,
        owner_frames=[],
    )


def _reader_for(true_offsets):
    """Fake reader: returns frames flipping at the event's recording time. The
    event is identified from the requested window start."""

    def reader(video, start_s, dur_s, *, fps=10.0, run=None):
        n = int(dur_s * fps)
        for t_game, off in true_offsets.items():
            guess_start = t_game - 20.0 - 8.0
            if abs(start_s - guess_start) < 1e-6:
                flip = round((t_game + off - start_s) * fps)
                return _frames(n, flip_at=flip)
        return _frames(n)

    return reader


def test_calibrate_median_and_outlier():
    ts = [100.0, 200.0, 300.0, 400.0, 500.0]
    offs = dict(zip(ts, [-21.6, -21.6, -21.5, -21.6, -15.0], strict=True))
    tl = _tl([_ev("kill", t) for t in ts])
    r = calibrate_from_hud("v.mp4", tl, reader=_reader_for(offs))
    assert r is not None and r.n == 5
    assert r.offset_s == pytest.approx(-21.6, abs=0.11)
    assert r.spread_s <= 0.5
    assert len(r.samples) == 5


def test_calibrate_too_few_samples():
    ts = [100.0, 200.0]
    tl = _tl([_ev("kill", t) for t in ts])
    assert calibrate_from_hud("v", tl, reader=_reader_for({100.0: -21.6, 200.0: -21.6})) is None


def test_calibrate_spread_too_big():
    ts = [100.0, 200.0, 300.0, 400.0, 500.0]
    offs = dict(zip(ts, [-21.6, -19.0, -23.5, -17.0, -25.0], strict=True))
    tl = _tl([_ev("kill", t) for t in ts])
    assert calibrate_from_hud("v", tl, reader=_reader_for(offs)) is None


def test_calibrate_skips_close_events_and_other_kinds():
    seen = []
    base = _reader_for({100.0: -21.6, 300.0: -21.6, 400.0: -21.6, 500.0: -21.6})

    def reader(video, start_s, dur_s, **kw):
        seen.append(start_s)
        return base(video, start_s, dur_s)

    tl = _tl(
        [
            _ev("kill", 200.0),
            _ev("assist", 201.5),  # close together: both skipped
            _ev("kill", 100.0),
            _ev("death", 300.0),
            _ev("kill", 400.0),
            _ev("assist", 500.0),
            _ev("ally_death", 600.0),
            _ev("building", 700.0),
        ]
    )
    r = calibrate_from_hud("v", tl, reader=reader)
    assert r is not None and r.n == 4
    assert sorted(t for t, _ in r.samples) == [100.0, 300.0, 400.0, 500.0]


def test_calibrate_caps_events_spread_across_game():
    ts = [float(100 + 50 * i) for i in range(30)]
    seen = []

    def reader(video, start_s, dur_s, **kw):
        seen.append(start_s)
        return _frames(int(dur_s * 10))

    calibrate_from_hud("v", _tl([_ev("kill", t) for t in ts]), max_events=12, reader=reader)
    assert len(seen) == 12
    assert min(seen) < 100 and max(seen) > 1400  # first and last region both used


# --- CLI ---


def _cli_fakes(result):
    from ai_video_editor.league import hud_sync_cli

    written = []
    deps = dict(
        load=lambda aid: _tl([], offset=-20.0),
        path=lambda aid: "asset.mp4",
        calibrate=lambda src, tl: result,
        record=lambda aid, off, source=None: written.append((aid, off, source)),
    )
    return hud_sync_cli, deps, written


def test_cli_dry_run_writes_nothing(capsys):
    cli, deps, written = _cli_fakes(HudSync(-21.6, 3, 0.1, [(100.0, -21.6)]))
    assert cli.main(["--asset", "a1"], **deps) == 0
    out = capsys.readouterr().out
    assert "-20.00" in out and "-21.60" in out and "n=3" in out and "100.0" in out
    assert written == []


def test_cli_write_records_hud_source():
    cli, deps, written = _cli_fakes(HudSync(-21.6, 3, 0.1, [(100.0, -21.6)]))
    assert cli.main(["--asset", "a1", "--write"], **deps) == 0
    assert written == [("a1", -21.6, "hud")]


def test_cli_no_result_returns_1_and_never_writes():
    cli, deps, written = _cli_fakes(None)
    assert cli.main(["--asset", "a1", "--write"], **deps) == 1
    assert written == []


def test_cli_missing_timeline_or_video():
    cli, deps, _written = _cli_fakes(None)
    assert cli.main(["--asset", "a1"], **{**deps, "load": lambda a: None}) == 2
    assert cli.main(["--asset", "a1"], **{**deps, "path": lambda a: None}) == 2


def test_cli_guess_searches_around_the_given_offset(capsys):
    # Yunara 2026-11-26: stored -17 was ~26 s off, outside the +-8 s window.
    cli, deps, written = _cli_fakes(HudSync(9.1, 5, 0.1, [(100.0, 9.1)]))
    seen = []
    deps["calibrate"] = lambda src, tl: (
        seen.append(tl.offset_s) or HudSync(9.1, 5, 0.1, [(100.0, 9.1)])
    )
    assert cli.main(["--asset", "a1", "--guess", "9", "--write"], **deps) == 0
    assert seen == [9.0]
    assert "searching around 9.00" in capsys.readouterr().out
    assert written == [("a1", 9.1, "hud")]
