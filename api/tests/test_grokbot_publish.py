"""Publishing a panel round: digest + clips to Drive, ids filled, one event."""

import json
import shutil
from pathlib import Path

import pytest
from conftest import FakeUploader

from ai_video_editor.config import settings
from ai_video_editor.decisions.models import Answer
from ai_video_editor.grokbot import digest as dg
from ai_video_editor.grokbot import publish as pub

FIXTURE = Path(__file__).parent / "fixtures" / "panel_round"


@pytest.fixture
def round_dir(tmp_path, monkeypatch):
    # Clips must live under an allowed root to be uploaded.
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    dest = tmp_path / "2026-11-20_zeri"
    shutil.copytree(FIXTURE, dest)
    for c in json.loads((dest / "clips.json").read_text()):
        (dest / c["file"]).write_bytes(b"mp4")
    return dest


class FakeApi:
    def __init__(self, version="7"):
        self.version = version
        self.asked = []

    def plan_version(self, cid):
        self.asked.append(cid)
        return self.version


def ids(name, subfolder):
    return f"{subfolder}:{name}"


def run(round_dir, **kw):
    events = []
    up = kw.pop("uploader", FakeUploader(ids=ids))
    defaults = dict(
        game="league", compilation_id="comp-1", api=FakeApi(), uploader=up, ask_many=False
    )
    defaults.update(kw)
    payload = pub.publish_round(round_dir, emit=events.append, **defaults)
    return payload, events, up


def test_publish_uploads_digest_and_clips_and_emits(round_dir):
    payload, events, up = run(round_dir)
    assert sorted(up.uploads) == sorted(
        [
            ("01_kill_14m32s.mp4", "clips"),
            ("02_kill_21m05s.mp4", "clips"),
            ("03_kill_30m10s.mp4", "clips"),
            ("2026-11-20_zeri-digest.json", "digests"),
        ]
    )
    assert [e.event for e in events] == ["round.published"]
    assert events[0].payload == payload
    assert payload == {
        "round": "2026-11-20_zeri",
        "game": "league",
        "asset_id": None,
        "compilation_id": "comp-1",
        "compilation_version": "7",
        "drive": {
            "digest": "digests:2026-11-20_zeri-digest.json",
            "clips": {
                "14m32s": "clips:01_kill_14m32s.mp4",
                "21m05s": "clips:02_kill_21m05s.mp4",
                "30m10s": "clips:03_kill_30m10s.mp4",
            },
        },
        "clips": 3,
        "skipped_outside_roots": 0,
    }


def test_saved_digest_carries_ids_and_never_leaks(round_dir):
    run(round_dir, asset_id="asset-9")
    saved = dg.Digest.model_validate_json((round_dir / "digest.json").read_text())
    assert saved.compilation_id == "comp-1" and saved.compilation_version == "7"
    assert saved.asset_id == "asset-9"
    assert [c.drive for c in saved.clips] == [
        "clips:01_kill_14m32s.mp4",
        "clips:02_kill_21m05s.mp4",
        "clips:03_kill_30m10s.mp4",
    ]
    assert dg.digest_leaks(saved, round_dir / "transcript.json") == []


def test_leaky_digest_is_refused_before_any_upload(round_dir):
    line = json.loads((round_dir / "transcript.json").read_text())[0]["text"]
    gaming = json.loads((round_dir / "gaming-expert.json").read_text())
    gaming[0]["story_beats"] = [line]
    (round_dir / "gaming-expert.json").write_text(json.dumps(gaming))
    up = FakeUploader()
    with pytest.raises(pub.DigestLeakError):
        run(round_dir, uploader=up)
    assert up.uploads == []
    assert not (round_dir / "digest.json").exists()


def test_absolute_clip_paths_are_used_as_is(round_dir, tmp_path):
    elsewhere = tmp_path / "elsewhere" / "09_kill_05m00s.mp4"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(b"x")
    clips = json.loads((round_dir / "clips.json").read_text())
    clips[0]["file"] = str(elsewhere)
    (round_dir / "clips.json").write_text(json.dumps(clips))
    payload, _, up = run(round_dir)
    assert ("09_kill_05m00s.mp4", "clips") in up.uploads
    assert payload["drive"]["clips"]["05m00s"] == "clips:09_kill_05m00s.mp4"


def test_no_api_no_uploader_still_saves_and_emits(round_dir):
    payload, events, _ = run(round_dir, api=None, compilation_id=None, uploader=None)
    assert payload["compilation_version"] is None
    assert payload["drive"] == {
        "digest": None,
        "clips": dict.fromkeys(["14m32s", "21m05s", "30m10s"]),
    }
    assert (round_dir / "digest.json").is_file()
    assert events[0].event == "round.published"


def test_jev_annotation_used_when_given(round_dir):
    def ask_many(items):
        return [Answer(value="hype", probability=0.8, backend="jev") for _ in items]

    run(round_dir, ask_many=ask_many)
    saved = json.loads((round_dir / "digest.json").read_text())
    assert saved["clips"][0]["panel"]["format"] == "hype"


def test_default_ask_many_is_off_for_rules_backend(monkeypatch):
    monkeypatch.setattr(settings, "decisions_backend", "rules")
    assert pub._default_ask_many() is None
    monkeypatch.setattr(settings, "decisions_backend", "jev")
    assert pub._default_ask_many() is not None


def test_failing_jev_does_not_stop_publishing(round_dir):
    def boom(items):
        raise RuntimeError("jev down")

    payload, events, _ = run(round_dir, ask_many=boom)
    assert events and payload["clips"] == 3


def test_malformed_expert_json_does_not_crash(round_dir):
    (round_dir / "gaming-expert.json").write_text("{not json")
    (round_dir / "viewer-expert.json").write_text('{"clips": [{"no_ref": 1}, 3]}')
    payload, _, _ = run(round_dir)
    assert payload["clips"] == 3


def test_cli_refuses_folder_without_clips(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        pub.main([str(tmp_path), "--game", "league"])
    assert e.value.code == 1


def test_cli_publishes_with_no_jev(round_dir, monkeypatch, capsys):
    seen = {}

    def fake(rd, **kw):
        seen.update(kw, round_dir=rd)
        return {"ok": True}

    monkeypatch.setattr(pub, "publish_round", fake)
    pub.main([str(round_dir), "--game", "league", "--no-jev"])
    assert seen["ask_many"] is False and seen["api"] is None
    assert seen["compilation_id"] is None
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def _point_first_clip_at(round_dir, file):
    clips = json.loads((round_dir / "clips.json").read_text())
    clips[0]["file"] = file
    (round_dir / "clips.json").write_text(json.dumps(clips))


def test_absolute_clip_outside_roots_is_skipped(round_dir, tmp_path_factory, monkeypatch):
    monkeypatch.setattr(settings, "outplayed_media_dir", round_dir / "media")
    outside = tmp_path_factory.mktemp("outside") / "09_kill_05m00s.mp4"
    outside.write_bytes(b"private")
    _point_first_clip_at(round_dir, str(outside))
    payload, _, up = run(round_dir)
    assert ("09_kill_05m00s.mp4", "clips") not in up.uploads
    assert payload["drive"]["clips"]["05m00s"] is None
    assert payload["skipped_outside_roots"] == 1


def test_dotdot_relative_clip_escaping_roots_is_skipped(round_dir, monkeypatch):
    # The workspace is the round folder itself: "../x" climbs out of it.
    monkeypatch.setattr(settings, "workspace_dir", round_dir)
    monkeypatch.setattr(settings, "outplayed_media_dir", round_dir / "media")
    escaped = round_dir.parent / "09_kill_05m00s.mp4"
    escaped.write_bytes(b"private")
    _point_first_clip_at(round_dir, "../09_kill_05m00s.mp4")
    payload, _, up = run(round_dir)
    assert ("09_kill_05m00s.mp4", "clips") not in up.uploads
    assert payload["skipped_outside_roots"] == 1
    assert ("02_kill_21m05s.mp4", "clips") in up.uploads


def test_retention_days_must_be_at_least_one():
    from pydantic import ValidationError

    from ai_video_editor.config import Settings

    with pytest.raises(ValidationError):
        Settings(grokbot_drive_preview_retention_days=0)
    assert (
        Settings(grokbot_drive_preview_retention_days=1).grokbot_drive_preview_retention_days == 1
    )
