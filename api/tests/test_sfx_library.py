import subprocess
from types import SimpleNamespace

from ai_video_editor import sfx_library
from ai_video_editor.config import settings


def test_kaching_cmd_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ffmpeg_path", "ff")
    out = tmp_path / "k.wav"
    cmd = sfx_library.kaching_cmd(out)
    assert cmd[0] == "ff"
    assert cmd[cmd.index("-f") + 1] == "lavfi"
    assert "1760" in cmd[cmd.index("-i") + 1] and "d=0.35" in cmd[cmd.index("-i") + 1]
    assert cmd[cmd.index("-ac") + 1] == "2"
    assert cmd[-1] == str(out)


def test_kaching_path_synthesised_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        from pathlib import Path

        Path(cmd[-1]).write_bytes(b"RIFF")
        return SimpleNamespace(returncode=0, stderr="")

    p = sfx_library.kaching_path(run=fake_run)
    assert p == tmp_path / "sfx" / "kaching.wav"
    assert p.is_file() and len(calls) == 1


def test_kaching_path_reuses_existing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    (tmp_path / "sfx").mkdir()
    (tmp_path / "sfx" / "kaching.wav").write_bytes(b"mine")

    def boom(*a, **k):
        raise AssertionError("must not synthesise")

    assert sfx_library.kaching_path(run=boom).read_bytes() == b"mine"


def _write_wav(path, silence_s, tone_s=0.2, sr=48000):
    import wave

    import numpy as np

    t = np.arange(int(tone_s * sr)) / sr
    tone = (0.5 * np.sin(2 * np.pi * 1000 * t) * 32767).astype("<i2")
    pcm = np.concatenate([np.zeros(int(silence_s * sr), dtype="<i2"), tone])
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def test_sfx_onset_measures_leading_silence(tmp_path):
    p = tmp_path / "a.wav"
    _write_wav(p, 0.25)
    assert abs(sfx_library.sfx_onset_s(p) - 0.25) < 0.02


def test_sfx_onset_zero_without_silence(tmp_path):
    p = tmp_path / "b.wav"
    _write_wav(p, 0.0)
    assert sfx_library.sfx_onset_s(p) < 0.005


def test_sfx_onset_zero_when_unreadable(tmp_path):
    p = tmp_path / "c.wav"
    p.write_bytes(b"not audio")
    assert sfx_library.sfx_onset_s(p) == 0.0
    assert sfx_library.sfx_onset_s(tmp_path / "missing.wav") == 0.0


def test_sfx_onset_cached_per_mtime(tmp_path):
    p = tmp_path / "d.wav"
    _write_wav(p, 0.1)
    calls = []
    real = subprocess.run

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    sfx_library.sfx_onset_s(p, run=counting)
    sfx_library.sfx_onset_s(p, run=counting)
    assert len(calls) == 1


def test_death_cmd_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ffmpeg_path", "ff")
    out = tmp_path / "d.wav"
    cmd = sfx_library.death_cmd(out)
    assert cmd[0] == "ff"
    assert cmd[cmd.index("-f") + 1] == "lavfi"
    graph = cmd[cmd.index("-i") + 1]
    assert "highpass" in graph and "2800" in graph and "5300" in graph
    assert cmd[-1] == str(out)


def test_death_path_synthesised_once_and_keeps_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        from pathlib import Path

        Path(cmd[-1]).write_bytes(b"RIFF")
        return SimpleNamespace(returncode=0, stderr="")

    p = sfx_library.death_path(run=fake_run)
    assert p == tmp_path / "sfx" / "death.wav" and len(calls) == 1
    sfx_library.death_path(run=fake_run)
    assert len(calls) == 1
    p.write_bytes(b"mine")
    assert sfx_library.death_path(run=fake_run).read_bytes() == b"mine"


def test_sfx_hit_uses_sidecar_hit_point(tmp_path):
    p = tmp_path / "kaching.wav"
    _write_wav(p, 0.05)
    (tmp_path / "kaching.json").write_text('{"hit_s": 0.43}', encoding="utf-8")
    assert sfx_library.sfx_hit_s(p) == 0.43


def test_sfx_hit_falls_back_to_onset(tmp_path):
    p = tmp_path / "death.wav"
    _write_wav(p, 0.25)
    assert abs(sfx_library.sfx_hit_s(p) - 0.25) < 0.02


def test_sfx_hit_ignores_bad_sidecar(tmp_path):
    p = tmp_path / "k.wav"
    _write_wav(p, 0.25)
    (tmp_path / "k.json").write_text('{"hit_s": -1}', encoding="utf-8")
    assert abs(sfx_library.sfx_hit_s(p) - 0.25) < 0.02
    (tmp_path / "k.json").write_text("not json", encoding="utf-8")
    assert abs(sfx_library.sfx_hit_s(p) - 0.25) < 0.02
