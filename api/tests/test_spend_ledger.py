"""Dollar ledger: cost math, unknown models, cap boundary."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from ai_video_editor.anthropic_surface import SpendCapError, check_budget, record_spend
from ai_video_editor.config import settings


@pytest.fixture
def db(tmp_path):
    return tmp_path / "ledger.db"


def _rows(db):
    with sqlite3.connect(db) as c:
        return c.execute(
            "SELECT asset_id, feature, model, input_tokens, output_tokens, cost_usd "
            "FROM spend_ledger"
        ).fetchall()


def test_cost_math_sonnet(db):
    cost = record_spend(
        "a1",
        "rank",
        "claude-sonnet-4-6",
        SimpleNamespace(input_tokens=1_000_000, output_tokens=100_000),
        db_path=db,
    )
    assert cost == pytest.approx(3.0 + 1.5)
    assert _rows(db) == [
        ("a1", "rank", "claude-sonnet-4-6", 1_000_000, 100_000, pytest.approx(4.5))
    ]


def test_unknown_model_costed_at_max_price_warns_and_trips_cap(db, monkeypatch, caplog):
    monkeypatch.setattr(
        settings,
        "model_prices_usd_per_mtok",
        {"cheap": (1.0, 15.0), "pricey": (3.0, 5.0)},
    )
    monkeypatch.setattr(settings, "cost_cap_per_asset_usd", 2.0)
    with caplog.at_level("WARNING", logger="ai_video_editor.anthropic_surface"):
        cost = record_spend(
            "a1",
            "rank",
            "mystery",
            SimpleNamespace(input_tokens=500_000, output_tokens=100_000),
            db_path=db,
        )
    assert cost == pytest.approx((500_000 * 3.0 + 100_000 * 15.0) / 1_000_000)  # $3.00
    assert "mystery" in caplog.text
    assert _rows(db) == [("a1", "rank", "mystery", 500_000, 100_000, pytest.approx(3.0))]
    with pytest.raises(SpendCapError):
        check_budget("a1", db_path=db)


def test_unknown_model_with_empty_price_table_is_null(db, monkeypatch):
    monkeypatch.setattr(settings, "model_prices_usd_per_mtok", {})
    usage = SimpleNamespace(input_tokens=10, output_tokens=5)
    assert record_spend("a1", "rank", "mystery", usage, db_path=db) is None


def test_no_asset_id_writes_nothing(db):
    record_spend(
        None,
        "rank",
        "claude-haiku-4-5",
        SimpleNamespace(input_tokens=10, output_tokens=5),
        db_path=db,
    )
    check_budget(None, db_path=db)
    assert not db.exists() or _rows(db) == []


def test_check_budget_on_empty_db_passes(db):
    check_budget("a1", db_path=db)


def test_cap_refused_at_boundary_not_below(db, monkeypatch):
    monkeypatch.setattr(settings, "cost_cap_per_asset_usd", 2.0)
    # haiku: $1/Mtok in -> 1.5M tokens = $1.50
    record_spend(
        "a1",
        "rank",
        "claude-haiku-4-5",
        SimpleNamespace(input_tokens=1_500_000, output_tokens=0),
        db_path=db,
    )
    check_budget("a1", db_path=db)  # 1.50 < 2.0
    record_spend(
        "a1",
        "rank",
        "claude-haiku-4-5",
        SimpleNamespace(input_tokens=500_000, output_tokens=0),
        db_path=db,
    )
    with pytest.raises(SpendCapError):
        check_budget("a1", db_path=db)  # exactly 2.0
    check_budget("other", db_path=db)


# ---------------------------------------------------------------------
# Call sites: check before, record after, graceful degradation
# ---------------------------------------------------------------------


class _Usage(SimpleNamespace):
    pass


def _spy(monkeypatch, module, log):
    monkeypatch.setattr(module, "check_budget", lambda a: log.append(("check", a)))
    monkeypatch.setattr(module, "record_spend", lambda a, f, m, u: log.append(("record", a, f)))


def test_ranker_checks_before_and_records_after(monkeypatch):
    from ai_video_editor import ranker

    log: list = []
    _spy(monkeypatch, ranker, log)

    class FakeMessages:
        def parse(self, **_):
            log.append(("api",))
            return SimpleNamespace(
                parsed_output=SimpleNamespace(results=[]),
                usage=_Usage(input_tokens=1, output_tokens=1),
                model="m",
            )

    monkeypatch.setattr(ranker, "make_client", lambda: SimpleNamespace(messages=FakeMessages()))
    monkeypatch.setattr(ranker, "reserve_call", lambda *a: None)
    ranker.rank_candidates("league", [{"id": "c1"}], asset_id="a9")
    assert log == [("check", "a9"), ("api",), ("record", "a9", "rank")]


def test_ranker_survives_ledger_write_failure(monkeypatch):
    from ai_video_editor import ranker

    monkeypatch.setattr(ranker, "check_budget", lambda a: None)

    def boom(*a):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(ranker, "record_spend", boom)
    monkeypatch.setattr(ranker, "reserve_call", lambda *a: None)

    class FakeMessages:
        def parse(self, **_):
            return SimpleNamespace(
                parsed_output=SimpleNamespace(results=[SimpleNamespace(keep=True)]),
                usage=_Usage(input_tokens=1, output_tokens=1),
                model="m",
            )

    monkeypatch.setattr(ranker, "make_client", lambda: SimpleNamespace(messages=FakeMessages()))
    assert len(ranker.rank_candidates("league", [{"id": "c1"}], asset_id="a9")) == 1


def _vlm_backend(monkeypatch, log, asset_id="a9"):
    from ai_video_editor.vlm.backends import anthropic_backend as ab

    _spy(monkeypatch, ab, log)

    class FakeMessages:
        def create(self, **_):
            log.append(("api",))
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="{}")],
                usage=_Usage(input_tokens=1, output_tokens=1),
                model="claude-haiku-4-5",
            )

    monkeypatch.setattr(
        ab.anthropic, "Anthropic", lambda **_: SimpleNamespace(messages=FakeMessages())
    )
    return ab.AnthropicBackend(api_key="k", asset_id=asset_id)


def test_vlm_backend_checks_before_and_records_after(monkeypatch):
    log: list = []
    backend = _vlm_backend(monkeypatch, log)
    assert backend.call(frames=[], system_prompt="s", user_prompt="u") == "{}"
    assert log == [("check", "a9"), ("api",), ("record", "a9", "vlm")]


def test_vlm_backend_capped_asset_is_unavailable_not_a_crash(monkeypatch):
    from ai_video_editor.vlm.backends import anthropic_backend as ab
    from ai_video_editor.vlm.backends.base import VLMUnavailableError

    log: list = []
    backend = _vlm_backend(monkeypatch, log)

    def capped(a):
        raise SpendCapError("over cap")

    monkeypatch.setattr(ab, "check_budget", capped)
    with pytest.raises(VLMUnavailableError):
        backend.call(frames=[], system_prompt="s", user_prompt="u")
    assert ("api",) not in log


def test_vlm_backend_ledger_error_is_unavailable_not_a_crash(monkeypatch):
    from ai_video_editor.vlm.backends import anthropic_backend as ab
    from ai_video_editor.vlm.backends.base import VLMUnavailableError

    log: list = []
    backend = _vlm_backend(monkeypatch, log)

    def locked(a):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ab, "check_budget", locked)
    with pytest.raises(VLMUnavailableError):
        backend.call(frames=[], system_prompt="s", user_prompt="u")
    assert ("api",) not in log


def test_select_backend_threads_asset_id():
    from ai_video_editor.vlm.client import select_backend

    assert select_backend("anthropic", asset_id="a9").asset_id == "a9"


# ---------------------------------------------------------------------
# Fix wave: max_tokens, budget errors, own cap keys, LIKE escaping
# ---------------------------------------------------------------------


def _create_spy(monkeypatch, seen: list):
    from ai_video_editor.vlm.backends import anthropic_backend as ab

    class FakeMessages:
        def create(self, **kw):
            seen.append(kw)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="{}")],
                usage=_Usage(input_tokens=1, output_tokens=1),
                model="claude-sonnet-4-6",
            )

    monkeypatch.setattr(
        ab.anthropic, "Anthropic", lambda **_: SimpleNamespace(messages=FakeMessages())
    )
    monkeypatch.setattr(ab, "record_spend", lambda *a, **k: None)
    return ab


def test_vlm_backend_max_tokens_defaults_to_1024_and_is_overridable(monkeypatch):
    seen: list = []
    ab = _create_spy(monkeypatch, seen)
    monkeypatch.setattr(ab, "check_budget", lambda *a, **k: None)
    backend = ab.AnthropicBackend(api_key="k", asset_id="a9")
    backend.call(frames=[], system_prompt="s", user_prompt="u")
    backend.call(frames=[], system_prompt="s", user_prompt="u", max_tokens=4096)
    assert [kw["max_tokens"] for kw in seen] == [1024, 4096]


def test_vlm_backend_own_cap_is_passed_to_check_budget(monkeypatch):
    seen: list = []
    ab = _create_spy(monkeypatch, seen)
    checks: list = []
    monkeypatch.setattr(ab, "check_budget", lambda a, **k: checks.append((a, k)))
    backend = ab.AnthropicBackend(
        api_key="k", asset_id="plays:a9", cap_usd=1.5, cap_name="GAME_UNDERSTANDING_BUDGET_USD"
    )
    backend.call(frames=[], system_prompt="s", user_prompt="u")
    assert checks == [
        ("plays:a9", {"cap_usd": 1.5, "cap_name": "GAME_UNDERSTANDING_BUDGET_USD"})
    ]


def test_vlm_backend_cap_error_is_both_spend_cap_and_unavailable(monkeypatch):
    from ai_video_editor.vlm.backends.base import VLMUnavailableError

    seen: list = []
    ab = _create_spy(monkeypatch, seen)

    def capped(a, **_):
        raise SpendCapError("over cap")

    monkeypatch.setattr(ab, "check_budget", capped)
    backend = ab.AnthropicBackend(api_key="k", asset_id="a9")
    with pytest.raises(SpendCapError) as info:
        backend.call(frames=[], system_prompt="s", user_prompt="u")
    assert isinstance(info.value, VLMUnavailableError)
    assert seen == []


def test_spent_usd_prefix_underscore_is_literal(db):
    from ai_video_editor.anthropic_surface import _LEDGER_DDL, spent_usd

    conn = sqlite3.connect(db)
    conn.execute(_LEDGER_DDL)
    with conn:
        for f, c in (("play_confirm", 0.1), ("playXconfirm", 5.0), ("play%", 7.0)):
            conn.execute(
                "INSERT INTO spend_ledger (asset_id, feature, cost_usd) VALUES ('a1', ?, ?)",
                (f, c),
            )
    conn.close()
    assert spent_usd("a1", feature_prefix="play_", db_path=db) == pytest.approx(0.1)
