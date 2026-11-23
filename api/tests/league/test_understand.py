"""understand_game: frozen file, budget, prose sidecar (Task 3). No real VLM or ffmpeg."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from ai_video_editor.anthropic_surface import _LEDGER_DDL, spent_usd
from ai_video_editor.league import understand as understand_mod
from ai_video_editor.league.phases import phases_for  # noqa: F401
from ai_video_editor.league.play_models import GameModel
from ai_video_editor.league.play_vocab import load_vocab
from ai_video_editor.league.plays import objectives_from
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


def _label(second: int) -> dict:
    state = "none" if second < 20 else "approach" if second < 26 else (
        "fighting" if second < 40 else "aftermath")  # fmt: skip
    return {"second": second, "state": state, "kill_banner": second == 30,
            "allies_visible": 1, "enemies_visible": 1}  # fmt: skip


# Window starts 30 s before the first kill: approach 20, engage 26, banner 30, outcome 40.
LABELS = [_label(s) for s in range(0, 52, 2)]


class Backend:
    name = "fake"
    feature = "vlm"

    def __init__(self):
        self.n = 0

    def call(self, *, frames, system_prompt, user_prompt, max_tokens=1024):
        self.n += 1
        return json.dumps({"frames": LABELS, "description": "prose only"})


@pytest.fixture
def env(tmp_path, monkeypatch):
    a = tmp_path / "m.jpg"
    a.write_bytes(b"x")
    monkeypatch.setattr(pc, "sample_window", lambda *a_, **k: [a])
    monkeypatch.setattr(pc, "sample_panels", lambda *a_, **k: [a])
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


def test_vocab_hash_mismatch_returns_frozen_with_warning(env, caplog):
    first, _ = _run(env)
    other = VOCAB.model_copy(update={"hash": "changed"})
    with caplog.at_level("WARNING"):
        again, args = _run(env, vocab=other)
    assert args["backend"].n == 0
    assert again.vocab_hash == first.vocab_hash
    assert "force" in caplog.text
    _, forced = _run(env, vocab=other, force=True)
    assert forced["backend"].n == 2


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
                "INSERT INTO spend_ledger (asset_id, feature, cost_usd) VALUES ('plays:a1', ?, ?)",
                (f, c),
            )
    conn.close()
    assert spent_usd("plays:a1", feature_prefix="play_", db_path=db) == pytest.approx(0.3)


def test_sweep_adds_trade_play_when_budget_remains(env, monkeypatch):
    monkeypatch.setattr("ai_video_editor.league.understand.get_duration_seconds", lambda _p: 400.0)

    class B(Backend):
        def call(self, *, frames, system_prompt, user_prompt, max_tokens=1024):
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
            "INSERT INTO spend_ledger (asset_id, feature, cost_usd) "
            "VALUES ('plays:a1', 'play_confirm', 1.4)"
        )
    conn.close()
    model, args = _run(env, budget_usd=1.5)
    assert args["backend"].n == len(model.plays) > 0
    assert model.budget_spent_usd == 0.0


class LedgerBackend(Backend):
    """Each call books $1 on the understanding key, like the real backend."""

    def __init__(self, db):
        super().__init__()
        self.db = db

    def call(self, **kw):
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute(
                "INSERT INTO spend_ledger (asset_id, feature, cost_usd) "
                "VALUES ('plays:a1', 'play_confirm', 1.0)"
            )
        conn.close()
        return super().call(**kw)


def test_owner_plays_confirmed_first_absent_only_while_budget_remains(env, monkeypatch):
    seen = []
    real = understand_mod.confirm_play

    def spy(video, play, **kw):
        seen.append(play.id)
        return real(video, play, **kw)

    monkeypatch.setattr(understand_mod, "confirm_play", spy)
    model, _ = _run(env, budget_usd=1.0, backend=LedgerBackend(env[1]))
    assert seen == ["p01"]  # p01 is the owner's kill; p02 (absent) found no budget left
    by_id = {p.id: p for p in model.plays}
    assert by_id["p01"].window.source == "vlm"
    assert by_id["p02"].window.source == "fallback"
    assert model.budget_spent_usd == pytest.approx(1.0)
    assert model.budget_exhausted is True  # p02 was left unconfirmed for lack of budget


def test_spend_cap_stops_pass_and_marks_budget_exhausted(env):
    from ai_video_editor.vlm.backends.anthropic_backend import VLMBudgetError

    class Capped(Backend):
        def call(self, **kw):
            self.n += 1
            raise VLMBudgetError("plays:a1 has spent $1.50")

    model, args = _run(env, backend=Capped(), sweep=True)
    assert args["backend"].n == 1
    assert model.budget_exhausted is True
    assert all(p.window.source == "fallback" for p in model.plays)
    raw = json.loads((env[0] / "plays" / "a1.json").read_text(encoding="utf-8"))
    assert raw["budget_exhausted"] is True


def test_default_backend_is_sonnet_on_its_own_key_and_cap(monkeypatch):
    from ai_video_editor.config import settings
    from ai_video_editor.vlm.backends.anthropic_backend import AnthropicBackend

    monkeypatch.setattr(settings, "vlm_backend", "anthropic")
    be = understand_mod.default_backend("a1", cap_usd=1.5)
    assert isinstance(be, AnthropicBackend)
    assert be.model == "claude-sonnet-4-6"
    assert be.asset_id == "plays:a1"
    assert be.feature == "play_confirm"
    assert be._cap_kwargs == {"cap_usd": 1.5, "cap_name": "GAME_UNDERSTANDING_BUDGET_USD"}


def test_default_backend_falls_back_to_select_backend(monkeypatch):
    from ai_video_editor.config import settings
    from ai_video_editor.vlm.backends.ollama_backend import OllamaBackend

    monkeypatch.setattr(settings, "vlm_backend", "ollama")
    assert isinstance(understand_mod.default_backend("a1", cap_usd=1.5), OllamaBackend)


def test_objectives_filled_from_elites_and_buildings_skipping_souls():
    parts = [
        {"puuid": f"P{i + 1}", "participantId": i + 1, "championName": c,
         "teamId": 100 if i < 5 else 200}
        for i, c in enumerate(CHAMPS)
    ]  # fmt: skip
    match = {
        "metadata": {"matchId": "NA1_O"},
        "info": {"gameStartTimestamp": 1000, "gameEndTimestamp": 2_001_000, "participants": parts},
    }
    events = [
        {"type": "ELITE_MONSTER_KILL", "timestamp": 700_000, "killerId": 2, "killerTeamId": 100,
         "monsterType": "DRAGON", "monsterSubType": "FIRE_DRAGON"},
        {"type": "DRAGON_SOUL_GIVEN", "timestamp": 701_000, "teamId": 100, "name": "Fire"},
        {"type": "BUILDING_KILL", "timestamp": 800_000, "killerId": 7, "teamId": 100,
         "buildingType": "TOWER_BUILDING", "laneType": "MID_LANE"},
    ]  # fmt: skip
    tl = parse_timeline(
        match,
        {"info": {"frames": [{"timestamp": 0, "events": events}]}},
        owner_puuid="P1",
        offset_s=20.0,
        confidence="high",
    )
    objs = objectives_from(tl)
    assert [(o.type, o.sub, o.team, o.t_s) for o in objs] == [
        ("DRAGON", "FIRE_DRAGON", "ally", 700.0),
        ("TOWER_BUILDING", "MID_LANE", "enemy", 800.0),
    ]


def test_understand_game_writes_objectives(env):
    model, _ = _run(env)
    assert model.objectives == objectives_from(_timeline())


def _three_kill_timeline():
    parts = [
        {"puuid": f"P{i + 1}", "participantId": i + 1, "championName": c,
         "teamId": 100 if i < 5 else 200}
        for i, c in enumerate(CHAMPS)
    ]  # fmt: skip
    match = {
        "metadata": {"matchId": "NA1_B"},
        "info": {"gameStartTimestamp": 1000, "gameEndTimestamp": 2_001_000, "participants": parts},
    }

    def ck(t, victim):
        return {"type": "CHAMPION_KILL", "timestamp": t * 1000, "killerId": 1,
                "victimId": victim, "assistingParticipantIds": [],
                "position": {"x": 8000, "y": 7800}}  # fmt: skip

    events = [ck(600, 6), ck(1000, 7), ck(1400, 8)]
    tl = {"info": {"frames": [{"timestamp": 0, "events": events}]}}
    return parse_timeline(match, tl, owner_puuid="P1", offset_s=20.0, confidence="high")


def test_cap_is_per_run_baseline_plus_budget(env, monkeypatch):
    """$1.40 already on the key, $1.50 budget: the backend cap is $2.90, so a
    rerun gets its own full budget. At $0.75 a call two confirmations land and
    the third play finds the run budget spent."""
    from ai_video_editor.anthropic_surface import SpendCapError, check_budget, record_spend
    from ai_video_editor.config import settings
    from ai_video_editor.vlm.backends.anthropic_backend import VLMBudgetError

    tmp, db = env
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO spend_ledger (asset_id, feature, cost_usd) "
            "VALUES ('plays:a1', 'play_confirm', 1.4)"
        )
    conn.close()
    monkeypatch.setattr(settings, "vlm_backend", "anthropic")
    monkeypatch.setattr(settings, "model_prices_usd_per_mtok", {"claude-sonnet-4-6": (0.75, 0.0)})
    built = []

    class FakeAnthropic(Backend):
        """Mirrors AnthropicBackend's ledger handling against the test db."""

        def __init__(self, *, asset_id, model, feature, cap_usd, cap_name):
            super().__init__()
            self.asset_id, self.cap_usd, self.cap_name = asset_id, cap_usd, cap_name
            built.append(self)

        def call(self, **kw):
            try:
                check_budget(self.asset_id, db_path=db, cap_usd=self.cap_usd,
                             cap_name=self.cap_name)  # fmt: skip
            except SpendCapError as exc:
                raise VLMBudgetError(str(exc)) from exc
            record_spend(self.asset_id, "play_confirm", "claude-sonnet-4-6",
                         SimpleNamespace(input_tokens=1_000_000, output_tokens=0),
                         db_path=db)  # fmt: skip
            return super().call(**kw)

    monkeypatch.setattr(understand_mod, "AnthropicBackend", FakeAnthropic)
    model = understand_game(
        "a1", "v.mp4", _three_kill_timeline(), vocab=VOCAB, budget_usd=1.5, fps=0.5,
        sweep_fps=0.25, sweep=False, out_path=tmp / "plays" / "a1.json", ledger_db=db,
    )  # fmt: skip
    (be,) = built
    assert be.asset_id == "plays:a1" and be.cap_usd == pytest.approx(2.9)
    assert be.n == 2
    assert [p.window.source for p in model.plays] == ["vlm", "vlm", "fallback"]
    assert model.budget_spent_usd == pytest.approx(1.5)
    assert model.budget_exhausted is True
    # the backend's own cap refuses the next call outright
    with pytest.raises(SpendCapError):
        be.call(frames=[], system_prompt="s", user_prompt="u")
