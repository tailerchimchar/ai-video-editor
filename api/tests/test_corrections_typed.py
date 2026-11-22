"""AVE-7: typed correction rows, top-K + RUBRIC_VERSION cache key, and
league_examples.jsonl created on first use. Cost guard: the VLM sidecar
cache key must not move on appends outside the rendered window or on the
typed-row migration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_video_editor.main import app
from ai_video_editor.vlm import corrections, hint_files, prompts


@pytest.fixture
def hints_dir(tmp_path, monkeypatch) -> Path:
    scratch = tmp_path / "game_hints"
    scratch.mkdir()
    (scratch / "valorant.md").write_text("<!-- PROMPT_VERSION: 7 -->\n# hints\n", encoding="utf-8")
    (scratch / "league.md").write_text("<!-- PROMPT_VERSION: 1 -->\n# lol\n", encoding="utf-8")
    (scratch / "_default.md").write_text("default hints", encoding="utf-8")
    monkeypatch.setattr(hint_files, "HINTS_DIR", scratch)
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "game_hints_dir", scratch)
    return scratch


@pytest.fixture
def top_k_3(monkeypatch) -> int:
    monkeypatch.setattr(corrections.settings, "corrections_top_k", 3)
    return 3


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _rewrite_rows(path: Path, transform) -> None:
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
    path.write_text("\n".join(json.dumps(transform(r)) for r in rows) + "\n", encoding="utf-8")


def test_row_outside_top_k_keeps_effective_version(hints_dir, top_k_3) -> None:
    for i in range(top_k_3):
        corrections.append_correction("valorant", feedback=f"f{i}", rule=f"r{i}")
    path = corrections._examples_path("valorant")
    before = corrections.effective_prompt_version("valorant")
    # An older row placed before the window is never rendered.
    old = '{"added_at": "2025-01-01T00:00:00+00:00", "feedback": "old", "rule": "old"}\n'
    path.write_text(old + path.read_text(encoding="utf-8"), encoding="utf-8")
    assert corrections.effective_prompt_version("valorant") == before


def test_row_entering_top_k_changes_effective_version(hints_dir, top_k_3) -> None:
    corrections.append_correction("valorant", feedback="a", rule="r1")
    before = corrections.effective_prompt_version("valorant")
    corrections.append_correction("valorant", feedback="b", rule="r2")
    assert corrections.effective_prompt_version("valorant") != before


def test_rubric_version_bump_changes_effective_version(hints_dir, monkeypatch) -> None:
    before = corrections.effective_prompt_version("valorant")
    monkeypatch.setattr(corrections, "RUBRIC_VERSION", corrections.RUBRIC_VERSION + "-next")
    assert corrections.effective_prompt_version("valorant") != before


def test_hint_file_edit_changes_effective_version(hints_dir) -> None:
    before = corrections.effective_prompt_version("valorant")
    (hints_dir / "valorant.md").write_text("<!-- PROMPT_VERSION: 8 -->\n", encoding="utf-8")
    assert corrections.effective_prompt_version("valorant") != before


def test_typed_migration_keeps_effective_version(hints_dir) -> None:
    corrections.append_correction("valorant", feedback="a", rule="r1", tags=["t"])
    path = corrections._examples_path("valorant")
    before = corrections.effective_prompt_version("valorant")
    _rewrite_rows(path, lambda r: {**r, "concept": None, "verdict": None, "example": None})
    assert corrections.effective_prompt_version("valorant") == before


def test_shipped_valorant_examples_survive_typed_migration(hints_dir) -> None:
    real = Path(corrections.__file__).parent / "game_hints"
    path = hints_dir / "valorant_examples.jsonl"
    path.write_bytes((real / "valorant_examples.jsonl").read_bytes())
    before = corrections.effective_prompt_version("valorant")
    _rewrite_rows(path, lambda r: {**r, "concept": None, "verdict": None, "example": None})
    assert corrections.effective_prompt_version("valorant") == before


def test_typed_fields_roundtrip_and_render(hints_dir) -> None:
    corrections.append_correction(
        "valorant",
        feedback="fb",
        rule="rr",
        concept="wave management",
        verdict="false_positive",
        example="freeze under tower",
    )
    got = corrections.load_recent_corrections("valorant")[0]
    assert (got.concept, got.verdict, got.example) == (
        "wave management",
        "false_positive",
        "freeze under tower",
    )
    rendered = corrections.render_corrections_section([got])
    assert "wave management" in rendered
    assert "false_positive" in rendered
    assert "freeze under tower" in rendered


def test_league_examples_created_on_first_use_and_rendered(hints_dir) -> None:
    assert not (hints_dir / "league_examples.jsonl").exists()
    corrections.append_correction("league", feedback="fb", rule="LOL_RULE", concept="kiting")
    assert (hints_dir / "league_examples.jsonl").is_file()
    text, resolved = prompts.load_game_hints("League of Legends")
    assert resolved == "league"
    assert "LOL_RULE" in text


def test_http_recent_corrections_returns_typed_fields(hints_dir, client) -> None:
    posted = client.post(
        "/api/v1/mentor/record_correction",
        json={
            "game": "valorant",
            "feedback": "fb1",
            "rule": "rule1",
            "concept": "trading",
            "verdict": "style",
            "example": "ex",
        },
    )
    assert posted.status_code == 200
    body = client.get("/api/v1/mentor/recent_corrections", params={"game": "valorant"}).json()
    row = body["corrections"][0]
    assert (row["concept"], row["verdict"], row["example"]) == ("trading", "style", "ex")


def test_http_record_rejects_unknown_verdict(hints_dir, client) -> None:
    resp = client.post(
        "/api/v1/mentor/record_correction",
        json={"game": "valorant", "feedback": "fb1", "rule": "rule1", "verdict": "nope"},
    )
    assert resp.status_code == 422
