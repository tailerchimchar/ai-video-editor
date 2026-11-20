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


class FailingUploader:
    """Uploader that fails on specified files."""

    def __init__(self, fail_on: set[str] | None = None, fail_digest: bool = False):
        self.uploads: list[tuple[str, str]] = []
        self.fail_on = fail_on or set()
        self.fail_digest = fail_digest

    def upload(self, path, *, name, subfolder):
        if self.fail_digest and "digest" in name:
            raise RuntimeError("simulated digest upload failure")
        if any(fail_name in name for fail_name in self.fail_on):
            raise RuntimeError(f"simulated upload failure for {name}")
        self.uploads.append((name, subfolder))
        return f"{subfolder}:{name}"

    def prune(self, *, subfolder, older_than_days):
        return 0


@pytest.fixture
def player_names_file(tmp_path):
    """Create a valid player-names file with placeholder names for tests."""
    names_file = tmp_path / "player-names.txt"
    # Use names that do NOT appear in the fixture digest
    names_file.write_text("InventedName1\nInventedName2\nPlaceholderPlayer\n")
    return names_file


@pytest.fixture
def round_dir(tmp_path, monkeypatch, player_names_file):
    # Clips must live under an allowed root to be uploaded.
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    dest = tmp_path / "2026-11-20_zeri"
    shutil.copytree(FIXTURE, dest)
    for c in json.loads((dest / "clips.json").read_text()):
        (dest / c["file"]).write_bytes(b"mp4")
    # Monkeypatch the default player names path to our fixture file
    monkeypatch.setattr(pub, "_default_player_names_path", lambda: player_names_file)
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


def run_expecting_failure(round_dir, **kw):
    """Like run() but expects PublishUploadError and returns events + exception."""
    events = []
    up = kw.pop("uploader", FailingUploader())
    defaults = dict(
        game="league", compilation_id="comp-1", api=FakeApi(), uploader=up, ask_many=False
    )
    defaults.update(kw)
    exc = None
    try:
        pub.publish_round(round_dir, emit=events.append, **defaults)
    except pub.PublishUploadError as e:
        exc = e
    return events, up, exc


def test_publish_uploads_digest_and_clips_and_emits(round_dir):
    payload, events, up = run(round_dir)
    # Clips are now named <round>-<ref>.mp4 for uniqueness in the flat Drive folder
    assert sorted(up.uploads) == sorted(
        [
            ("2026-11-20_zeri-14m32s.mp4", "clips"),
            ("2026-11-20_zeri-21m05s.mp4", "clips"),
            ("2026-11-20_zeri-30m10s.mp4", "clips"),
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
                "14m32s": "clips:2026-11-20_zeri-14m32s.mp4",
                "21m05s": "clips:2026-11-20_zeri-21m05s.mp4",
                "30m10s": "clips:2026-11-20_zeri-30m10s.mp4",
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
        "clips:2026-11-20_zeri-14m32s.mp4",
        "clips:2026-11-20_zeri-21m05s.mp4",
        "clips:2026-11-20_zeri-30m10s.mp4",
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
    # Uses the round-ref naming now
    assert ("2026-11-20_zeri-05m00s.mp4", "clips") in up.uploads
    assert payload["drive"]["clips"]["05m00s"] == "clips:2026-11-20_zeri-05m00s.mp4"


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
    assert ("2026-11-20_zeri-05m00s.mp4", "clips") not in up.uploads
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
    assert ("2026-11-20_zeri-05m00s.mp4", "clips") not in up.uploads
    assert payload["skipped_outside_roots"] == 1
    assert ("2026-11-20_zeri-21m05s.mp4", "clips") in up.uploads


def test_retention_days_must_be_at_least_one():
    from pydantic import ValidationError

    from ai_video_editor.config import Settings

    with pytest.raises(ValidationError):
        Settings(grokbot_drive_preview_retention_days=0)
    assert (
        Settings(grokbot_drive_preview_retention_days=1).grokbot_drive_preview_retention_days == 1
    )


# --- Upload failure tests ---


def test_failed_clip_upload_emits_publish_failed_not_published(round_dir):
    """When a clip upload fails, round.publish_failed is emitted and round.published is NOT."""
    up = FailingUploader(fail_on={"21m05s"})
    events, _, exc = run_expecting_failure(round_dir, uploader=up)

    assert exc is not None
    assert exc.round_name == "2026-11-20_zeri"
    assert "21m05s" in exc.failed_clips
    assert not exc.digest_failed

    event_names = [e.event for e in events]
    assert "round.publish_failed" in event_names
    assert "round.published" not in event_names

    fail_event = next(e for e in events if e.event == "round.publish_failed")
    assert fail_event.payload["round"] == "2026-11-20_zeri"
    assert fail_event.payload["step"] == "clips"
    assert "21m05s" in fail_event.payload["failed_clips"]


def test_failed_digest_upload_emits_publish_failed(round_dir):
    """When the digest upload fails, round.publish_failed is emitted."""
    up = FailingUploader(fail_digest=True)
    events, _, exc = run_expecting_failure(round_dir, uploader=up)

    assert exc is not None
    assert exc.digest_failed

    event_names = [e.event for e in events]
    assert "round.publish_failed" in event_names
    assert "round.published" not in event_names

    fail_event = next(e for e in events if e.event == "round.publish_failed")
    assert fail_event.payload["step"] == "digest"
    assert fail_event.payload["digest_failed"] is True


def test_no_uploader_configured_allows_null_ids(round_dir):
    """When uploader is None, null ids are OK and round.published is emitted."""
    payload, events, _ = run(round_dir, uploader=None)

    assert [e.event for e in events] == ["round.published"]
    assert payload["drive"]["digest"] is None
    assert all(v is None for v in payload["drive"]["clips"].values())


def test_multiple_clip_failures_all_reported(round_dir):
    """Multiple clip upload failures are all reported in the failure event."""
    up = FailingUploader(fail_on={"14m32s", "30m10s"})
    _events, _, exc = run_expecting_failure(round_dir, uploader=up)

    assert exc is not None
    assert len(exc.failed_clips) == 2
    assert "14m32s" in exc.failed_clips
    assert "30m10s" in exc.failed_clips


# --- Player name check tests ---


def test_player_name_in_digest_blocks_publishing(round_dir, tmp_path):
    """When a player name appears in digest, publishing is blocked before upload."""
    names_file = tmp_path / "names.txt"
    names_file.write_text("Quillfeather\nMumblewick\n")

    up = FakeUploader(ids=ids)
    with pytest.raises(pub.PlayerNameLeakError):
        pub.publish_round(
            round_dir,
            game="league",
            compilation_id="comp-1",
            api=FakeApi(),
            uploader=up,
            ask_many=False,
            player_names_path=names_file,
            emit=lambda e: None,
        )

    # No uploads should have happened
    assert up.uploads == []
    # digest.json should not exist
    assert not (round_dir / "digest.json").exists()


def test_missing_player_names_file_blocks_publishing(round_dir, tmp_path):
    """When player names file is missing, publishing is blocked (fail closed)."""
    missing_file = tmp_path / "does_not_exist.txt"

    events = []
    with pytest.raises(pub.PlayerNamesFileError) as exc_info:
        pub.publish_round(
            round_dir,
            game="league",
            compilation_id="comp-1",
            api=FakeApi(),
            uploader=FakeUploader(ids=ids),
            ask_many=False,
            player_names_path=missing_file,
            emit=events.append,
        )

    # Should fail with names_file_missing reason
    assert exc_info.value.reason == "names_file_missing"

    # round.publish_failed should be emitted, not round.published
    event_names = [e.event for e in events]
    assert "round.publish_failed" in event_names
    assert "round.published" not in event_names

    fail_event = next(e for e in events if e.event == "round.publish_failed")
    assert fail_event.payload["step"] == "names_file"
    assert fail_event.payload["reason"] == "names_file_missing"


def test_empty_player_names_file_allows_publishing(round_dir, tmp_path):
    """An empty player names file allows all digests through."""
    names_file = tmp_path / "names.txt"
    names_file.write_text("")

    _payload, events, _ = run(round_dir, player_names_path=names_file)

    assert [e.event for e in events] == ["round.published"]


def test_player_name_check_is_case_insensitive(round_dir, tmp_path):
    """Player name matching ignores case."""
    names_file = tmp_path / "names.txt"
    names_file.write_text("QUILLFEATHER\n")  # uppercase

    # Digest has "Quillfeather" in gaming-expert.json story_beats
    with pytest.raises(pub.PlayerNameLeakError):
        pub.publish_round(
            round_dir,
            game="league",
            compilation_id="comp-1",
            api=FakeApi(),
            uploader=FakeUploader(ids=ids),
            ask_many=False,
            player_names_path=names_file,
            emit=lambda e: None,
        )


# --- Digest padding tests ---


def test_digest_clips_have_padded_windows(round_dir):
    """Digest clips include both padded and moment windows."""
    run(round_dir)
    saved = dg.Digest.model_validate_json((round_dir / "digest.json").read_text())

    for clip in saved.clips:
        # Padded windows should be set
        assert clip.padded_start_s is not None
        assert clip.padded_end_s is not None
        # Moment windows should equal the original start/end
        assert clip.moment_start_s == clip.start_s
        assert clip.moment_end_s == clip.end_s
        # Padded start should be <= moment start (padding goes backwards)
        assert clip.padded_start_s <= clip.moment_start_s
        # Padded end should be >= moment end (padding goes forwards)
        assert clip.padded_end_s >= clip.moment_end_s


def test_digest_padding_is_about_10_seconds(round_dir):
    """Padding is approximately 10 seconds on each side."""
    run(round_dir)
    saved = dg.Digest.model_validate_json((round_dir / "digest.json").read_text())

    clip = saved.clips[0]
    # First clip at 870.0 should pad to 860.0 (870 - 10)
    assert clip.padded_start_s == 860.0
    # End at 892.5 should pad to 902.5 (892.5 + 10)
    assert clip.padded_end_s == 902.5


def test_digest_padding_clamps_to_zero(round_dir):
    """Padding does not go below 0."""
    # Modify clips to have a very early start
    clips = json.loads((round_dir / "clips.json").read_text())
    clips[0]["start_seconds"] = 5.0  # Will pad to max(0, 5-10) = 0
    clips[0]["end_seconds"] = 20.0
    (round_dir / "clips.json").write_text(json.dumps(clips))

    run(round_dir)
    saved = dg.Digest.model_validate_json((round_dir / "digest.json").read_text())

    clip = saved.clips[0]
    assert clip.padded_start_s == 0.0  # Clamped to 0


def test_cli_accepts_player_names_arg(round_dir, monkeypatch, tmp_path, capsys):
    """CLI accepts --player-names argument."""
    names_file = tmp_path / "names.txt"
    names_file.write_text("Nonexistent\n")

    seen = {}

    def fake(rd, **kw):
        seen.update(kw, round_dir=rd)
        return {"ok": True}

    monkeypatch.setattr(pub, "publish_round", fake)
    pub.main([str(round_dir), "--game", "league", "--player-names", str(names_file)])
    assert seen["player_names_path"] == names_file


def test_cli_exits_3_on_player_name_leak(round_dir, monkeypatch, capsys, tmp_path):
    """CLI exits with code 3 when a player name is found."""
    from ai_video_editor.grokbot import drive

    names_file = tmp_path / "names.txt"
    names_file.write_text("Quillfeather\n")

    monkeypatch.setattr(drive, "uploader_from_settings", lambda: FakeUploader(ids=ids))

    with pytest.raises(SystemExit) as e:
        pub.main([str(round_dir), "--game", "league", "--player-names", str(names_file)])
    assert e.value.code == 3


def test_cli_exits_4_on_upload_failure(round_dir, monkeypatch, capsys):
    """CLI exits with code 4 when an upload fails."""
    from ai_video_editor.grokbot import drive

    monkeypatch.setattr(drive, "uploader_from_settings", lambda: FailingUploader(fail_digest=True))

    with pytest.raises(SystemExit) as e:
        pub.main([str(round_dir), "--game", "league"])
    assert e.value.code == 4


def test_cli_exits_5_on_missing_names_file(round_dir, monkeypatch, capsys, tmp_path):
    """CLI exits with code 5 when player names file is missing."""
    from ai_video_editor.grokbot import drive

    missing_file = tmp_path / "does_not_exist.txt"

    monkeypatch.setattr(drive, "uploader_from_settings", lambda: FakeUploader(ids=ids))

    with pytest.raises(SystemExit) as e:
        pub.main([str(round_dir), "--game", "league", "--player-names", str(missing_file)])
    assert e.value.code == 5


def test_unreadable_names_file_blocks_publishing(round_dir, tmp_path, monkeypatch):
    """When player names file exists but is unreadable, publishing is blocked."""
    import os

    names_file = tmp_path / "names.txt"
    names_file.write_text("Quillfeather\n")

    # Make file unreadable (Unix only)
    if os.name != "nt":
        names_file.chmod(0o000)
        try:
            events = []
            with pytest.raises(pub.PlayerNamesFileError) as exc_info:
                pub.publish_round(
                    round_dir,
                    game="league",
                    compilation_id="comp-1",
                    api=FakeApi(),
                    uploader=FakeUploader(ids=ids),
                    ask_many=False,
                    player_names_path=names_file,
                    emit=events.append,
                )

            assert exc_info.value.reason == "names_file_unreadable"

            event_names = [e.event for e in events]
            assert "round.publish_failed" in event_names
            assert "round.published" not in event_names

            fail_event = next(e for e in events if e.event == "round.publish_failed")
            assert fail_event.payload["reason"] == "names_file_unreadable"
        finally:
            names_file.chmod(0o644)


def test_unset_names_path_uses_default_and_blocks_if_missing(tmp_path, monkeypatch):
    """When player_names_path is None, the default path is used and must exist."""
    # Set up a round dir without monkeypatching the default path
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    dest = tmp_path / "2026-11-20_zeri"
    shutil.copytree(FIXTURE, dest)
    for c in json.loads((dest / "clips.json").read_text()):
        (dest / c["file"]).write_bytes(b"mp4")

    # Point default path to a non-existent file
    missing_default = tmp_path / "nonexistent-player-names.txt"
    monkeypatch.setattr(pub, "_default_player_names_path", lambda: missing_default)

    events = []
    with pytest.raises(pub.PlayerNamesFileError) as exc_info:
        pub.publish_round(
            dest,
            game="league",
            compilation_id="comp-1",
            api=FakeApi(),
            uploader=FakeUploader(ids=ids),
            ask_many=False,
            player_names_path=None,  # Use default
            emit=events.append,
        )

    assert exc_info.value.reason == "names_file_missing"
    event_names = [e.event for e in events]
    assert "round.publish_failed" in event_names
    assert "round.published" not in event_names
