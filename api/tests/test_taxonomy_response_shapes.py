"""Pins the JSON the two classifiers feed to the MCP tools
(`list_session_buckets`, `list_shorts_topics`) against fixtures recorded
before the taxonomy migration. Any change here is a breaking MCP change.

`session_index.json` is a session that exercises every bucket, phase and
topic; `buckets.json` / `topics.json` are the recorded outputs.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from pathlib import Path

import pytest

from ai_video_editor import shorts
from ai_video_editor.bucketing import route_session_folder

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "taxonomy"


def _expected(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def session_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "session"
    folder.mkdir()
    shutil.copy(FIXTURE_DIR / "session_index.json", folder / "index.json")
    return folder


def test_session_buckets_response_is_unchanged(session_folder: Path) -> None:
    manifest = route_session_folder(session_folder)
    assert json.loads(json.dumps(asdict(manifest))) == _expected("buckets.json")


def test_shorts_topics_response_is_unchanged(
    session_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shorts, "_highlights_folder_for_asset", lambda a, c: session_folder)
    out = shorts.preview_buckets({}, [])
    out["highlights_folder"] = "<folder>"
    assert json.loads(json.dumps(out)) == _expected("topics.json")
