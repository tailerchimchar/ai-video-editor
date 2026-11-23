"""understand_game: frozen file, budget, prose sidecar (Task 3). No real VLM or ffmpeg."""

import json
import sqlite3

import pytest

from ai_video_editor.anthropic_surface import _LEDGER_DDL, spent_usd
from ai_video_editor.league.phases import phases_for  # noqa: F401
from ai_video_editor.league.play_models import GameModel
from ai_video_editor.league.play_vocab import load_vocab
from ai_video_editor.league.riot_timeline import parse_timeline
from ai_video_editor.league.understand import understand_game
from ai_video_editor.vlm import play_confirm as pc

VOCAB = load_vocab("league")
CHAMPS = ["Jhin", "Thresh", "Lux", "Garen", "Ahri", "Diana", "Ezreal", "Zed", "Vi", "Karma"]


def _timeline():
    parts = [
        {"puuid": f"P{i + 1}", "participantId": i + 1, "championName": c,
         "teamId": 100 if i < 5 else 200}
        for i, c in enumerate(CHAMPS)
    ]  # fmt: skip
    match = {
        "metadata": {"matchId": "NA1_U"},
        "info": {"gameStartTimestamp": 1000, "gameEndTimestamp": 2_001_000, "participants": parts},
    }

    def ck(t, killer, victim):
        return {"type": "CHAMPION_KILL", "timestamp": t * 1000, "killerId": killer,
                "victimId": victim, "assistingParticipantIds": [],
                "position": {"x": 8000, "y": 7800}}  # fmt: skip

    events = [ck(600, 1, 6), ck(1200, 2, 7)]
    tl = {"info": {"frames": [{"timestamp": 0, "events": events}]}}
    return parse_timeline(match, tl, owner_puuid="P1", offset_s=20.0, confidence="high")


class Backend:
    name = "fake"
    feature = "vlm"

    def __init__(self):
        self.n = 0

    def call(self, *, frames, system_prompt, user_prompt):
        self.n += 1
        return json.dumps({"approach_s": 20, "engage_s": 25, "outcome_s": 40,
                           "first_banner_s": 30, "description": "prose only"})  # fmt: skip


@pytest.fixture
def env(tmp_path, monkeypatch):
    a = tmp_path / "m.jpg"
    a.write_bytes(b"x")
    monkeypatch.setattr(pc, "sample_window", lambda *a_, **k: [a])
    monkeypatch.setattr(pc, "get_duration_seconds", lambda _p: 2100.0)
    monkeypatch.setattr("ai_video_editor.league.understand.get_duration_seconds", lambda _p: 2100.0)
    db = tmp_path / "ledger.db"
    sqlite3.connect(db).execute(_LEDGER_DDL).connection.close()
    return tmp_path, db


def _run(env, **kw):
    tmp, db = env
    args = dict(vocab=VOCAB, budget_usd=1.0, fps=1.0, sweep_fps=0.25, sweep=False,
                out_path=tmp / "plays" / "a1.json", backend=Backend(), ledger_db=db)  # fmt: skip
    args.update(kw)
    return understand_game("a1", "v.mp4", _timeline(), **args), args


def test_frozen_file_returned_when_hash_matches(env):
    first, _ = _run(env)
    assert all(p.window.source == "vlm" for p in first.plays)
    again, args2 = _run(env)
    assert args2["backend"].n == 0
    assert [p.id for p in again.plays] == [p.id for p in first.plays]
    assert all(p.text for p in again.plays)


def test_vocab_hash_mismatch_reruns(env):
    _run(env)
    other = VOCAB.model_copy(update={"hash": "changed"})
    _, args = _run(env, vocab=other)
    assert args["backend"].n == 2


def test_force_reruns(env):
    _run(env)
    _, args = _run(env, force=True)
    assert args["backend"].n == 2


def test_budget_stops_confirmations_and_leaves_pending(env):
    model, args = _run(env, budget_usd=0.0)
    assert args["backend"].n == 0
    assert model.plays
    for p in model.plays:
        assert p.window.source == "fallback"
        assert "approach_unconfirmed" in p.window.flags
    assert (env[0] / "plays" / "a1.json").is_file()


def test_plays_file_is_pretty_and_prose_is_separate(env):
    tmp, _ = env
    _run(env)
    raw = (tmp / "plays" / "a1.json").read_text(encoding="utf-8")
    assert raw.startswith("{\n  ")
    assert "prose only" not in raw
    GameModel.model_validate_json(raw)
    prose = json.loads((tmp / "plays" / "a1.prose.json").read_text(encoding="utf-8"))
    assert set(prose.values()) == {"prose only"}


def test_spent_usd_sums_feature_prefix(env):
    _, db = env
    conn = sqlite3.connect(db)
    with conn:
        for f, c in (("play_confirm", 0.1), ("play_sweep", 0.2), ("vlm", 5.0)):
            conn.execute(
                "INSERT INTO spend_ledger (asset_id, feature, cost_usd) VALUES ('a1', ?, ?)", (f, c)
            )
    conn.close()
    assert spent_usd("a1", feature_prefix="play_", db_path=db) == pytest.approx(0.3)


def test_sweep_adds_trade_play_when_budget_remains(env, monkeypatch):
    monkeypatch.setattr("ai_video_editor.league.understand.get_duration_seconds", lambda _p: 400.0)

    class B(Backend):
        def call(self, *, frames, system_prompt, user_prompt):
            if "NO kill banner" in system_prompt:
                return json.dumps({"findings": [
                    {"t_s": 300.0, "type": "trade", "description": "poke", "confidence": 0.9}
                ]})  # fmt: skip
            return super().call(frames=frames, system_prompt=system_prompt, user_prompt=user_prompt)

    model, _ = _run(env, sweep=True, backend=B())
    assert [p.type for p in model.plays].count("trade") == 1


def test_budget_is_per_run_and_reports_delta(env):
    _, db = env
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO spend_ledger (asset_id, feature, cost_usd) VALUES ('a1', 'play_confirm', 1.4)"
        )
    conn.close()
    model, args = _run(env, budget_usd=1.5)
    assert args["backend"].n == len(model.plays) > 0
    assert model.budget_spent_usd == 0.0
