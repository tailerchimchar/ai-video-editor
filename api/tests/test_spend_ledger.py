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


def test_unknown_model_writes_row_with_null_cost(db):
    assert (
        record_spend(
            "a1", "rank", "mystery", SimpleNamespace(input_tokens=10, output_tokens=5), db_path=db
        )
        is None
    )
    assert _rows(db) == [("a1", "rank", "mystery", 10, 5, None)]


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
