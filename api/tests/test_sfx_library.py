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
