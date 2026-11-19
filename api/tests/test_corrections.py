"""Tests for the corrections memory + mentor router. Isolates the
game-hints dir so the real production examples file is untouched."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_video_editor.main import app
from ai_video_editor.vlm import corrections, hint_files, prompts


@pytest.fixture
def hints_dir(tmp_path, monkeypatch) -> Path:
    """Point both corrections.py and prompts.py at a scratch dir with
    a synthetic `valorant.md` so tests can freely append/read without
    touching real prompts."""
    scratch = tmp_path / "game_hints"
    scratch.mkdir()
    (scratch / "valorant.md").write_text(
        "<!-- PROMPT_VERSION: 7 -->\n# test hints\n",
        encoding="utf-8",
    )
    (scratch / "_default.md").write_text("default hints", encoding="utf-8")
    monkeypatch.setattr(hint_files, "HINTS_DIR", scratch)
    return scratch


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_append_and_load_roundtrip(hints_dir) -> None:
    c = corrections.append_correction(
        "valorant",
        feedback="kills off by 1s",
        rule="include killfeed pop in output window",
        clip_ref="may30_v3.mp4",
        tags=["anchor"],
    )
    assert c.rule == "include killfeed pop in output window"
    got = corrections.load_recent_corrections("valorant", limit=5)
    assert len(got) == 1
    assert got[0].feedback == "kills off by 1s"
    assert got[0].clip_ref == "may30_v3.mp4"


def test_load_returns_newest_first(hints_dir) -> None:
    for i in range(3):
        corrections.append_correction(
            "valorant", feedback=f"fb{i}", rule=f"rule{i}"
        )
    got = corrections.load_recent_corrections("valorant", limit=5)
    assert [c.rule for c in got] == ["rule2", "rule1", "rule0"]


def test_load_respects_limit(hints_dir) -> None:
    for i in range(15):
        corrections.append_correction("valorant", feedback=f"f{i}", rule=f"r{i}")
    got = corrections.load_recent_corrections("valorant", limit=5)
    assert len(got) == 5


def test_load_empty_when_no_file(hints_dir) -> None:
    assert corrections.load_recent_corrections("valorant") == []


def test_load_skips_malformed_lines(hints_dir) -> None:
    path = corrections._examples_path("valorant")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Interleave good + garbage lines
    path.write_text(
        '{"added_at": "2026-01-01T00:00:00+00:00", "feedback": "ok", "rule": "r1"}\n'
        "not json at all\n"
        "{missing quotes}\n"
        '{"added_at": "2026-02-01T00:00:00+00:00", "feedback": "ok2", "rule": "r2"}\n',
        encoding="utf-8",
    )
    got = corrections.load_recent_corrections("valorant")
    assert len(got) == 2
    assert {c.rule for c in got} == {"r1", "r2"}


def test_render_corrections_section_empty_string_when_none() -> None:
    assert corrections.render_corrections_section([]) == ""


def test_render_corrections_section_formats_markdown() -> None:
    cs = [
        corrections.Correction(
            added_at="2026-07-14T10:00:00+00:00",
            feedback="fb",
            rule="my rule",
            clip_ref="clip1",
            tags=["a", "b"],
        )
    ]
    rendered = corrections.render_corrections_section(cs)
    assert "## Recent corrections" in rendered
    assert "my rule" in rendered
    assert "`clip1`" in rendered
    assert "a, b" in rendered


def test_load_game_hints_injects_corrections(hints_dir) -> None:
    corrections.append_correction(
        "valorant", feedback="fb", rule="THE_INJECTED_RULE"
    )
    text, resolved = prompts.load_game_hints("valorant")
    assert resolved == "valorant"
    assert "THE_INJECTED_RULE" in text
    assert "Recent corrections" in text


def test_effective_version_changes_after_correction(hints_dir) -> None:
    v0 = corrections.effective_prompt_version("valorant")
    corrections.append_correction("valorant", feedback="fb", rule="r")
    v1 = corrections.effective_prompt_version("valorant")
    assert v0 != v1


def test_effective_version_default_hints_ignores_examples(hints_dir) -> None:
    v0 = corrections.effective_prompt_version(None)
    corrections.append_correction("valorant", feedback="fb", rule="r")
    assert corrections.effective_prompt_version(None) == v0


def test_http_record_correction(hints_dir, client) -> None:
    resp = client.post(
        "/api/v1/mentor/record_correction",
        json={
            "game": "Valorant",
            "feedback": "kills off by 1s",
            "rule": "require killfeed pop within 1s of frag",
            "clip_ref": "may30_v3.mp4",
            "tags": ["anchor"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["game"] == "valorant"
    assert body["correction"]["rule"] == "require killfeed pop within 1s of frag"
    got = corrections.load_recent_corrections("valorant")
    assert len(got) == 1


def test_http_recent_corrections(hints_dir, client) -> None:
    corrections.append_correction("valorant", feedback="a", rule="r1")
    corrections.append_correction("valorant", feedback="b", rule="r2")
    resp = client.get(
        "/api/v1/mentor/recent_corrections", params={"game": "valorant"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert body["corrections"][0]["rule"] == "r2"


def test_http_record_correction_rejects_short_rule(hints_dir, client) -> None:
    resp = client.post(
        "/api/v1/mentor/record_correction",
        json={"game": "valorant", "feedback": "x", "rule": ""},
    )
    assert resp.status_code == 422


def test_http_recent_corrections_rejects_bad_limit(hints_dir, client) -> None:
    resp = client.get(
        "/api/v1/mentor/recent_corrections",
        params={"game": "valorant", "limit": 999},
    )
    assert resp.status_code == 400
