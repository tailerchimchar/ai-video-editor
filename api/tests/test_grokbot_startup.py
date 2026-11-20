"""Runner start-up from the wrong directory: one readable line, exit 1."""

import pytest
from pydantic import ValidationError

from ai_video_editor.config import Settings
from ai_video_editor.grokbot import startup


def _missing_settings_error(tmp_path, monkeypatch) -> ValidationError:
    monkeypatch.delenv("OUTPLAYED_MEDIA_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here, like C:\Windows\System32
    with pytest.raises(ValidationError) as e:
        Settings(_env_file=None)  # type: ignore[call-arg]
    return e.value


def test_settings_error_message_names_missing_vars(tmp_path, monkeypatch):
    msg = startup.settings_error_message(_missing_settings_error(tmp_path, monkeypatch))
    assert msg.startswith("run from the api directory so api/.env loads (missing: ")
    assert "OUTPLAYED_MEDIA_DIR" in msg and "WORKSPACE_DIR" in msg


def test_run_turns_import_time_settings_error_into_exit_1(tmp_path, monkeypatch, capsys):
    err = _missing_settings_error(tmp_path, monkeypatch)

    def boom(name):
        raise err

    monkeypatch.setattr(startup.importlib, "import_module", boom)
    with pytest.raises(SystemExit) as e:
        startup.runner()
    assert e.value.code == 1
    assert "run from the api directory so api/.env loads" in capsys.readouterr().err


def test_run_calls_the_entry_point(monkeypatch):
    called = []
    fake = type("M", (), {"main": staticmethod(lambda: called.append(1))})
    monkeypatch.setattr(startup.importlib, "import_module", lambda name: fake)
    startup.run("x:main")
    assert called == [1]


def test_runner_script_goes_through_the_startup_shim():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'grokbot-runner = "ai_video_editor.grokbot.startup:runner"' in text
