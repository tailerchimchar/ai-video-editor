"""Edit plan: schema round-trip, validation against the registry, resumable executor."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_video_editor.compile_journal import append_journal, read_journal, revert_to_version
from ai_video_editor.edit_plan import (
    STEPS,
    EditPlan,
    PlanNotExecutableError,
    PlanProgress,
    execute_plan,
    load_progress,
    validate_plan,
)

SOUNDS = {"hit_marker", "whoosh"}


def _plan(**overrides) -> dict:
    base = {
        "format": "story_short",
        "source_clips": [
            {"clip_ref": "c1", "start": 0.0, "end": 12.4, "intensity": 0.7},
            {"clip_ref": "c2", "start": 3.0, "end": 9.0, "intensity": 0.4},
        ],
        "order": ["c2", "c1"],
        "music": {"song_id": "s1", "trim_to_peak": True},
        "effects": [{"clip_ref": "c1", "kind": "zoom", "at": 8.1, "params": {"roi": "center"}}],
        "cards": [{"after": None, "text": "first time in voice comms", "seconds": 2.5}],
        "sfx": [{"clip_ref": "c1", "at": 8.3, "sound": "hit_marker"}],
        "rejected": [{"wanted": "slow_mo", "why": "not in arsenal yet"}],
    }
    return EditPlan.model_validate({**base, **overrides})


# ----- schema -----


def test_plan_round_trips():
    plan = _plan()
    assert EditPlan.model_validate_json(plan.model_dump_json()) == plan


def test_music_trim_fields_are_optional_and_additive():
    plan = _plan(music={"song_id": "s1", "trim_to_peak": False, "trim_start_s": 4.0})
    assert plan.music.trim_start_s == 4.0
    assert plan.music.trim_end_s is None
    assert _plan().music.trim_start_s is None


# ----- validate_plan -----


def test_valid_plan_has_no_problems():
    assert validate_plan(_plan(), known_sounds=SOUNDS) == []


def test_unknown_kind_is_rejected():
    plan = _plan(effects=[{"clip_ref": "c1", "kind": "sparkle", "at": 1.0}])
    (msg,) = validate_plan(plan, known_sounds=SOUNDS)
    assert "unknown kind 'sparkle'" in msg


def test_not_renderable_kind_is_rejected():
    plan = _plan(effects=[{"clip_ref": "c1", "kind": "slow_mo", "at": 1.0}])
    (msg,) = validate_plan(plan, known_sounds=SOUNDS)
    assert "'slow_mo' is not renderable" in msg


def test_unknown_sound_is_rejected():
    plan = _plan(sfx=[{"clip_ref": "c1", "at": 1.0, "sound": "airhorn"}])
    (msg,) = validate_plan(plan, known_sounds=SOUNDS)
    assert "unknown sound 'airhorn'" in msg


def test_unknown_format_is_rejected():
    (msg,) = validate_plan(_plan(format="meme_reel"), known_sounds=SOUNDS)
    assert "unknown format 'meme_reel'" in msg


def test_bad_params_are_rejected():
    plan = _plan(effects=[{"clip_ref": "c1", "kind": "focus", "at": 1.0, "params": {"x": 0.5}}])
    (msg,) = validate_plan(plan, known_sounds=SOUNDS)
    assert "bad params for 'focus'" in msg


def test_order_must_reference_source_clips():
    (msg,) = validate_plan(_plan(order=["c1", "ghost"]), known_sounds=SOUNDS)
    assert "'ghost' is not in source_clips" in msg


def test_sounds_default_to_the_workspace_sfx_library(sandbox):
    sfx_dir = sandbox / "ws" / "media_library" / "lol" / "sfx"
    sfx_dir.mkdir(parents=True)
    (sfx_dir / "hit_marker.wav").write_bytes(b"")
    assert validate_plan(_plan()) == []
    assert validate_plan(_plan(sfx=[{"clip_ref": "c1", "at": 1.0, "sound": "nope"}]))


# ----- execute_plan -----


class FakeExecutor:
    """Records steps; optionally raises on the nth call (1-based)."""

    def __init__(self, folder: Path, fail_on_step: int | None = None):
        self.folder = folder
        self.fail_on_step = fail_on_step
        self.calls: list[str] = []

    def compilation_folder(self, compilation_id: str) -> Path:
        return self.folder

    def preflight(self, plan) -> list[str]:
        return []

    def current_version(self, compilation_id: str) -> str:
        return str(len(read_journal(self.folder)))

    def _step(self, name: str) -> None:
        if self.fail_on_step == len(self.calls) + 1:
            raise RuntimeError(f"boom on {name}")
        self.calls.append(name)
        append_journal(self.folder, {"clips": [], "after": name}, action=name)

    def apply_source_clips(self, plan):
        self._step("source_clips")

    def apply_order(self, plan):
        self._step("order")

    def apply_cards(self, plan):
        self._step("cards")

    def apply_effects(self, plan):
        self._step("effects")

    def apply_sfx(self, plan):
        self._step("sfx")

    def apply_music(self, plan):
        self._step("music")


def test_execute_runs_steps_in_order_and_records_progress(tmp_path: Path):
    append_journal(tmp_path, {"clips": [], "after": "start"}, action="compile")
    executor = FakeExecutor(tmp_path)
    plan = _plan()

    progress = execute_plan(plan, "comp-1", executor)

    assert executor.calls == ["source_clips", "order", "cards", "effects", "sfx", "music"]
    assert progress.completed_steps == len(STEPS)
    assert progress.snapshot_version == "1"
    assert load_progress(tmp_path) == progress


def test_failure_on_step_3_leaves_two_completed_and_resume_continues(tmp_path: Path):
    plan = _plan()
    failing = FakeExecutor(tmp_path, fail_on_step=3)
    with pytest.raises(RuntimeError, match="boom"):
        execute_plan(plan, "comp-1", failing)
    assert load_progress(tmp_path).completed_steps == 2

    resumed = FakeExecutor(tmp_path)
    progress = execute_plan(plan, "comp-1", resumed)

    assert resumed.calls == ["cards", "effects", "sfx", "music"]
    assert progress.completed_steps == len(STEPS)


def test_resume_keeps_the_original_snapshot(tmp_path: Path):
    append_journal(tmp_path, {"clips": [], "after": "start"}, action="compile")
    plan = _plan()
    with pytest.raises(RuntimeError):
        execute_plan(plan, "comp-1", FakeExecutor(tmp_path, fail_on_step=3))
    progress = execute_plan(plan, "comp-1", FakeExecutor(tmp_path))
    assert progress.snapshot_version == "1"


def test_a_different_plan_starts_fresh(tmp_path: Path):
    with pytest.raises(RuntimeError):
        execute_plan(_plan(), "comp-1", FakeExecutor(tmp_path, fail_on_step=3))
    other = FakeExecutor(tmp_path)
    execute_plan(_plan(order=["c1", "c2"]), "comp-1", other)
    assert len(other.calls) == len(STEPS)


def test_revert_to_snapshot_restores_pre_plan_spec(tmp_path: Path):
    append_journal(tmp_path, {"clips": [], "after": "start"}, action="compile")
    progress = execute_plan(_plan(), "comp-1", FakeExecutor(tmp_path))

    restored = revert_to_version(tmp_path, int(progress.snapshot_version))

    assert restored["after"] == "start"


def test_progress_model_fields():
    progress = PlanProgress(plan_hash="h", completed_steps=2, snapshot_version="4")
    assert progress.model_dump() == {
        "plan_hash": "h",
        "completed_steps": 2,
        "snapshot_version": "4",
    }


# ----- HTTP endpoint -----


@pytest.fixture
def compilation(client, asset, monkeypatch):
    """A finished two-clip compilation (DB row, folder, spec.json, journal)."""
    import asyncio
    import json
    import uuid
    from datetime import datetime, timezone

    from ai_video_editor.config import settings
    from ai_video_editor.database import get_db

    folder = settings.workspace_dir / "compilations" / "plan-folder"
    folder.mkdir(parents=True)
    spec = {
        "id": "spec-x",
        "asset_id": asset["id"],
        "clips": [
            {
                "id": cid,
                "asset_id": asset["id"],
                "asset_path": asset["path"],
                "start_seconds": start,
                "end_seconds": start + 10.0,
                "effects": [],
            }
            for cid, start in (("c-aaa", 100.0), ("c-bbb", 200.0))
        ],
    }
    (folder / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    (folder / "compilation.mp4").write_bytes(b"")
    append_journal(folder, spec, action="compile")
    comp_id = str(uuid.uuid4())

    async def seed():
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO compilations (id, asset_id, output_path, params, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    comp_id,
                    asset["id"],
                    (folder / "compilation.mp4").as_posix(),
                    "{}",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            await db.commit()
        finally:
            await db.close()

    asyncio.run(seed())
    monkeypatch.setattr(
        "ai_video_editor.routers.edits.render_spec",
        lambda spec, fld, dirty_clip_ids=None: {"output": (folder / "compilation.mp4").as_posix()},
    )
    return comp_id, folder


def _http_plan(**overrides) -> dict:
    base = {
        "format": "hype_montage",
        "source_clips": [
            {"clip_ref": "1", "start": 0.0, "end": 10.0, "intensity": 0.5},
            {"clip_ref": "2", "start": 0.0, "end": 10.0, "intensity": 0.9},
        ],
        "order": ["2", "1"],
        "effects": [{"clip_ref": "2", "kind": "zoom", "at": 3.0, "params": {"factor": 1.5}}],
    }
    return {**base, **overrides}


def test_plan_endpoint_executes_and_snapshots(client, compilation):
    import json

    comp_id, folder = compilation
    r = client.post(f"/api/v1/compilations/{comp_id}/plan", json=_http_plan())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["problems"] == []
    assert body["progress"]["completed_steps"] == len(STEPS)
    assert body["progress"]["snapshot_version"] == "1"
    spec = json.loads((folder / "spec.json").read_text(encoding="utf-8"))
    assert [c["id"] for c in spec["clips"]] == ["c-bbb", "c-aaa"]
    assert spec["clips"][0]["effects"][0]["kind"] == "zoom"
    assert (folder / "plan_progress.json").is_file()


def test_plan_endpoint_rejects_invalid_plan_before_rendering(client, compilation):
    comp_id, folder = compilation
    bad = _http_plan(effects=[{"clip_ref": "1", "kind": "slow_mo", "at": 1.0}])
    r = client.post(f"/api/v1/compilations/{comp_id}/plan", json=bad)
    assert r.status_code == 422
    assert "not renderable" in r.json()["detail"]["problems"][0]
    assert len(read_journal(folder)) == 1


def test_plan_with_cards_is_refused_before_anything_changes(client, compilation):
    comp_id, folder = compilation
    plan = _http_plan(cards=[{"text": "hi", "seconds": 2.5}])
    r = client.post(f"/api/v1/compilations/{comp_id}/plan", json=plan)
    assert r.status_code == 409
    assert r.json()["detail"]["unsupported"] == ["cards cannot be applied yet"]
    assert len(read_journal(folder)) == 1
    assert not (folder / "plan_progress.json").exists()


def test_plan_music_sets_music_path_on_the_spec(client, compilation, tmp_path):
    import json

    comp_id, folder = compilation
    song = tmp_path / "song.mp3"
    song.write_bytes(b"")
    plan = _http_plan(music={"song_id": str(song), "trim_to_peak": True})
    r = client.post(f"/api/v1/compilations/{comp_id}/plan", json=plan)
    assert r.status_code == 200, r.text
    spec = json.loads((folder / "spec.json").read_text(encoding="utf-8"))
    assert spec["music_path"] == song.as_posix()


def test_plan_music_resolves_a_library_song_id(client, compilation, tmp_path):
    import json

    from ai_video_editor.music.library import Library

    comp_id, folder = compilation
    song = tmp_path / "lib.mp3"
    song.write_bytes(b"")
    library_file = tmp_path / "library.json"
    library_file.write_text(
        json.dumps([{"song_id": "s-1", "audio_path": str(song), "tags": []}]), encoding="utf-8"
    )
    from ai_video_editor.routers.plan_executor import EditsPlanExecutor

    executor = EditsPlanExecutor(folder, comp_id, library=Library(path=library_file))
    assert executor.preflight(_plan(cards=[], sfx=[], music={"song_id": "s-1"})) == []
    assert executor.preflight(_plan(cards=[], sfx=[], music={"song_id": "missing"})) == [
        "music: song 'missing' not found"
    ]


def test_plan_on_a_compilation_without_journal_is_refused(client, compilation):
    comp_id, folder = compilation
    (folder / "spec_history.jsonl").unlink()
    r = client.post(f"/api/v1/compilations/{comp_id}/plan", json=_http_plan())
    assert r.status_code == 409
    assert "no journal entries" in r.json()["detail"]["unsupported"][0]


def test_reposting_a_completed_plan_is_a_noop_until_reverted(client, compilation):
    comp_id, folder = compilation
    url = f"/api/v1/compilations/{comp_id}/plan"
    first = client.post(url, json=_http_plan()).json()["progress"]
    journal_len = len(read_journal(folder))

    client.post(url, json=_http_plan())
    assert len(read_journal(folder)) == journal_len  # unchanged plan: no-op by design

    client.post(f"/api/v1/edit/compile/{comp_id}/revert", json={"to_version": 1})
    assert not (folder / "plan_progress.json").exists()
    again = client.post(url, json=_http_plan()).json()["progress"]
    assert again["completed_steps"] == len(STEPS)
    assert len(read_journal(folder)) > journal_len + 1
    assert first["snapshot_version"] == "1"


def test_execute_discards_progress_when_journal_is_shorter_than_snapshot(tmp_path: Path):
    for name in ("a", "b", "c"):
        append_journal(tmp_path, {"clips": []}, action=name)
    plan = _plan()
    with pytest.raises(RuntimeError):
        execute_plan(plan, "comp-1", FakeExecutor(tmp_path, fail_on_step=3))
    (tmp_path / "spec_history.jsonl").write_text("", encoding="utf-8")
    append_journal(tmp_path, {"clips": []}, action="rewound")

    fresh = FakeExecutor(tmp_path)
    progress = execute_plan(plan, "comp-1", fresh)

    assert len(fresh.calls) == len(STEPS)
    assert progress.snapshot_version == "1"


def test_preflight_refusal_changes_nothing(tmp_path: Path):
    class Refusing(FakeExecutor):
        def preflight(self, plan):
            return ["nope"]

    executor = Refusing(tmp_path)
    with pytest.raises(PlanNotExecutableError):
        execute_plan(_plan(), "comp-1", executor)
    assert executor.calls == []
    assert load_progress(tmp_path) is None
