"""Hook picks + storyboard routes (real `joined` target, sandbox workspace)."""

from __future__ import annotations

import json

import pytest
from tests.test_story_hook_edits import _jm

from ai_video_editor.config import settings
from ai_video_editor.story.hook_edits import base_digest

BASE = "/api/v1/narration/joined/joined_t"


def _story() -> object:
    from pathlib import Path

    d = Path(settings.workspace_dir) / "story"
    d.mkdir(parents=True, exist_ok=True)
    (d / "joined_t.mp4").write_bytes(b"x")
    return d


@pytest.fixture
def joined_map(client):
    d = _story()
    jm = _jm()
    jm.video = str(d / "joined_t.mp4")
    jm.games[0].chapters[0].play_id = "p00"
    jm.games[0].chapters[2].play_id = "p02"
    (d / "joined_t.join.json").write_text(jm.model_dump_json(), encoding="utf-8")
    return d


@pytest.fixture
def joined_map_v1(client):
    d = _story()
    jm = _jm()
    jm.schema_version = 1
    jm.hook = None
    (d / "joined_t.join.json").write_text(jm.model_dump_json(), encoding="utf-8")
    return d


def test_get_hook_returns_rendered_picks(client, joined_map):
    r = client.get(BASE + "/hook")
    assert r.status_code == 200 and r.json()["dirty"] is False and len(r.json()["picks"]) == 2


def test_get_hook_404_for_v1_map(client, joined_map_v1):
    r = client.get(BASE + "/hook")
    assert r.status_code == 404 and r.json()["detail"] == "no hook data for this video"


def test_put_overlapping_picks_is_settled(client, joined_map):
    r = client.put(
        BASE + "/hook",
        json={
            "picks": [
                {"chapter_id": "r1:p00", "at_s": 0},
                {"chapter_id": "r1:p02", "at_s": 3},
            ]
        },
    )
    assert r.status_code == 200
    assert [p["at_s"] for p in r.json()["picks"]] == [0.0, 14.0]
    assert (joined_map / "joined_t.edits.json").is_file()


def test_put_unchanged_picks_writes_no_file(client, joined_map):
    picks = client.get(BASE + "/hook").json()["picks"]
    body = {
        "picks": [
            {k: p[k] for k in ("id", "chapter_id", "at_s", "start_s", "end_s")} for p in picks
        ]
    }
    r = client.put(BASE + "/hook", json=body)
    assert r.status_code == 200 and r.json()["dirty"] is False
    assert not (joined_map / "joined_t.edits.json").exists()


def test_put_unknown_chapter_422(client, joined_map):
    r = client.put(BASE + "/hook", json={"picks": [{"chapter_id": "r1:nope"}]})
    assert r.status_code == 422 and "r1:nope" in r.json()["detail"]


def test_put_stale_edits_409_and_delete_clears(client, joined_map):
    stale = {
        "schema_version": 1,
        "video": "joined_t",
        "base_digest": "sha256:000000000000",
        "hook": {"mode": "play", "title": None, "picks": []},
    }
    (joined_map / "joined_t.edits.json").write_text(json.dumps(stale), encoding="utf-8")
    body = {"picks": [{"chapter_id": "r1:p00"}]}
    r = client.put(BASE + "/hook", json=body)
    assert r.status_code == 409 and "re-joined" in r.json()["detail"]
    r = client.delete(BASE + "/hook")
    assert r.status_code == 200 and r.json()["dirty"] is False
    assert not (joined_map / "joined_t.edits.json").exists()
    assert client.put(BASE + "/hook", json=body).status_code == 200


def test_put_accepts_the_same_chapter_twice_after_a_split(client, joined_map):
    r = client.put(
        BASE + "/hook",
        json={
            "picks": [
                {"id": "a1", "chapter_id": "r1:p00", "at_s": 0, "end_s": -6},
                {"id": "a2", "chapter_id": "r1:p00", "at_s": 20, "start_s": -6},
            ]
        },
    )
    assert r.status_code == 200 and len(r.json()["picks"]) == 2


def test_storyboard_lists_games_and_in_hook(client, joined_map):
    sb = client.get(BASE + "/storyboard").json()
    chs = sb["games"][0]["chapters"]
    assert chs[0]["in_hook"] is True and chs[1]["in_hook"] is False
    assert chs[0]["id"] == "r1:p00" and sb["hook"]["picks"]


def test_storyboard_404_for_v1_map(client, joined_map_v1):
    assert client.get(BASE + "/storyboard").status_code == 404


def test_digest_helper_matches():
    assert base_digest(_jm().hook).startswith("sha256:")


def test_corrupt_edits_file_is_422_on_get_hook_and_storyboard(client, joined_map):
    (joined_map / "joined_t.edits.json").write_text("{not json", encoding="utf-8")
    for path in ("/hook", "/storyboard"):
        r = client.get(BASE + path)
        assert r.status_code == 422, path
        assert "cannot read" in r.json()["detail"]
