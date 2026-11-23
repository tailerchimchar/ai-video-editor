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
    monkeypatch.setattr(
        understand_cli,
        "pool_game",
        lambda asset, plays, src, *, month: (
            seen.setdefault("pooled", []).append((asset, src, month)) or []
        ),
    )
    monkeypatch.setattr(understand_cli, "recording_month", lambda a, v: "2026-10")
    return seen


def test_cli_prints_budget_exhausted(monkeypatch, capsys):
    _wire(monkeypatch, GameModel(budget_spent_usd=1.5, budget_exhausted=True))
    assert understand_cli.main(["--asset", "a1", "--source", "v.mp4", "--no-pool"]) == 0
    out = capsys.readouterr().out
    assert "budget_spent_usd 1.5000" in out and "budget_exhausted" in out


def test_cli_quiet_when_budget_left_and_sweep_off_by_default(monkeypatch, capsys):
    seen = _wire(monkeypatch, GameModel(budget_spent_usd=0.2))
    understand_cli.main(["--asset", "a1", "--source", "v.mp4", "--no-pool"])
    assert "budget_exhausted" not in capsys.readouterr().out
    assert "pooled" not in seen
    assert seen["sweep"] is False and seen["fps"] == 0.5


def test_cli_pools_by_default_with_recording_month(monkeypatch, capsys):
    seen = _wire(monkeypatch, GameModel())
    understand_cli.main(["--asset", "a1", "--source", "v.mp4"])
    assert seen["pooled"] == [("a1", "v.mp4", "2026-10")]
    assert "month_pool 2026-10" in capsys.readouterr().out


def test_recording_month_from_created_at_then_mtime_then_today(tmp_path, monkeypatch):
    import os
    import sqlite3
    from datetime import date, datetime

    from ai_video_editor.config import settings

    db = tmp_path / "db.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE assets (id TEXT, created_at TEXT)")
    conn.execute("INSERT INTO assets VALUES ('a1', '2026-08-03T21:10:00+00:00')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(settings, "database_path", db)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    stamp = datetime(2025, 12, 24, 12).timestamp()
    os.utime(video, (stamp, stamp))
    real = understand_cli.recording_month
    assert real("a1", str(video)) == "2026-08"
    assert real("other", str(video)) == "2025-12"
    assert real("other", str(tmp_path / "gone.mp4")) == date.today().strftime("%Y-%m")
