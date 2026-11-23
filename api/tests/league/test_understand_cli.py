"""league-understand CLI: prints the plays, the spend and budget exhaustion."""

from ai_video_editor.league import understand_cli
from ai_video_editor.league.play_models import GameModel


def _wire(monkeypatch, model):
    seen = {}

    def fake_understand(asset, video, timeline, **kw):
        seen.update(kw)
        return model

    monkeypatch.setattr(understand_cli, "load_timeline", lambda a: object())
    monkeypatch.setattr(understand_cli, "understand_game", fake_understand)
    return seen


def test_cli_prints_budget_exhausted(monkeypatch, capsys):
    _wire(monkeypatch, GameModel(budget_spent_usd=1.5, budget_exhausted=True))
    assert understand_cli.main(["--asset", "a1", "--source", "v.mp4"]) == 0
    out = capsys.readouterr().out
    assert "budget_spent_usd 1.5000" in out and "budget_exhausted" in out


def test_cli_quiet_when_budget_left_and_sweep_off_by_default(monkeypatch, capsys):
    seen = _wire(monkeypatch, GameModel(budget_spent_usd=0.2))
    understand_cli.main(["--asset", "a1", "--source", "v.mp4"])
    assert "budget_exhausted" not in capsys.readouterr().out
    assert seen["sweep"] is False and seen["fps"] == 0.5
