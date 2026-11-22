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
            ("01_kill_14m32s.mp4", "clips/2026-11-20_zeri"),
            ("02_kill_21m05s.mp4", "clips/2026-11-20_zeri"),
            ("03_kill_30m10s.mp4", "clips/2026-11-20_zeri"),
            ("2026-11-20_zeri-digest.json", "digests/2026-11-20"),
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
            "digest": "digests/2026-11-20:2026-11-20_zeri-digest.json",
            "clips": {
                "14m32s": "clips/2026-11-20_zeri:01_kill_14m32s.mp4",
                "21m05s": "clips/2026-11-20_zeri:02_kill_21m05s.mp4",
                "30m10s": "clips/2026-11-20_zeri:03_kill_30m10s.mp4",
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
        "clips/2026-11-20_zeri:01_kill_14m32s.mp4",
        "clips/2026-11-20_zeri:02_kill_21m05s.mp4",
        "clips/2026-11-20_zeri:03_kill_30m10s.mp4",
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
    assert ("09_kill_05m00s.mp4", "clips/2026-11-20_zeri") in up.uploads
    assert payload["drive"]["clips"]["05m00s"] == "clips/2026-11-20_zeri:09_kill_05m00s.mp4"


def test_clips_dir_resolves_relative_clip_files(round_dir, tmp_path):
    """clips.json names bare files; they live in the highlights session folder."""
    clips_dir = tmp_path / "highlights" / "sess"
    clips_dir.mkdir(parents=True)
    for name in ("01_kill_14m32s.mp4", "02_kill_21m05s.mp4", "03_kill_30m10s.mp4"):
        (clips_dir / name).write_bytes(b"x")
        (round_dir / name).unlink(missing_ok=True)
    payload, _, up = run(round_dir, clips_dir=clips_dir)
    assert ("01_kill_14m32s.mp4", "clips/2026-11-20_zeri") in up.uploads
    assert payload["drive"]["clips"]["14m32s"] == "clips/2026-11-20_zeri:01_kill_14m32s.mp4"


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
    assert ("09_kill_05m00s.mp4", "clips/2026-11-20_zeri") not in up.uploads
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
    assert ("09_kill_05m00s.mp4", "clips/2026-11-20_zeri") not in up.uploads
    assert payload["skipped_outside_roots"] == 1
    assert ("02_kill_21m05s.mp4", "clips/2026-11-20_zeri") in up.uploads


def test_retention_days_must_be_at_least_one():
    from pydantic import ValidationError

    from ai_video_editor.config import Settings

    with pytest.raises(ValidationError):
        Settings(grokbot_drive_preview_retention_days=0)
    assert (
        Settings(grokbot_drive_preview_retention_days=1).grokbot_drive_preview_retention_days == 1
    )


def test_digest_with_a_key_is_refused_before_upload(round_dir):
    gaming = json.loads((round_dir / "gaming-expert.json").read_text())
    gaming[0]["story_beats"] = ["oops sk-ant-" + "a1B2c3D4e5F6g7H8i9J0k1"]
    (round_dir / "gaming-expert.json").write_text(json.dumps(gaming))
    up = FakeUploader()
    with pytest.raises(pub.SecretLeakError):
        run(round_dir, uploader=up)
    assert [u for u in up.uploads if "digest" in str(u)] == []


def test_secret_in_digest_uploads_nothing_and_names_pattern(round_dir):
    gaming = json.loads((round_dir / "gaming-expert.json").read_text())
    gaming[0]["story_beats"] = ["oops sk-ant-" + "a1B2c3D4e5F6g7H8i9J0k1"]
    (round_dir / "gaming-expert.json").write_text(json.dumps(gaming))
    up = FakeUploader()
    with pytest.raises(pub.SecretLeakError, match=r"anthropic@\d+") as ei:
        run(round_dir, uploader=up)
    assert up.uploads == []
    assert "sk-ant-" not in str(ei.value)


# --- digest v2 wiring ---

from ai_video_editor.league.riot_timeline import RiotTimeline, TimelineEvent  # noqa: E402


def _fake_timeline():
    return RiotTimeline(
        match_id="m",
        confidence="medium",
        offset_s=10.0,
        game_end_s=None,
        events=[TimelineEvent("kill", 870.0, 880.0, ["you (Jhin)", "enemy Diana"], {})],
        owner_frames=[],
    )


def test_publish_passes_injected_digest_inputs(round_dir):
    sliced = []

    def slices(s, e):
        sliced.append((s, e))
        return "some speech"

    run(
        round_dir,
        timeline=_fake_timeline(),
        transcript_slices=slices,
        paraphrase=lambda t: "The player reacts to the turn.",
        energy=lambda s, e: "laughter",
    )
    saved = dg.Digest.model_validate_json((round_dir / "digest.json").read_text())
    c = saved.clips[0]
    assert c.voice == "The player reacts to the turn."
    assert [(e.type, e.t_in_clip) for e in c.events] == [("kill", 10.0)]
    assert c.match_confidence == "medium" and c.energy == "laughter"
    assert sliced == [(870.0, 892.5), (1255.0, 1280.0), (1800.0, 1815.0)]


def test_paraphrase_leaking_transcript_is_refused(round_dir):
    up = FakeUploader()
    with pytest.raises(pub.DigestLeakError):
        run(
            round_dir,
            uploader=up,
            transcript_slices=lambda s, e: "unrelated words",
            paraphrase=lambda t: "they cried kited the wave perf at it",
        )
    assert up.uploads == []


def _cli(round_dir, monkeypatch, *extra):
    seen = {}
    monkeypatch.setattr(pub, "publish_round", lambda rd, **kw: seen.update(kw) or {})
    monkeypatch.setattr(pub, "_load_timeline", lambda asset_id, game: ("tl", asset_id, game))
    pub.main([str(round_dir), "--game", "league", "--no-jev", *extra])
    return seen


def test_cli_with_asset_id_wires_timeline_slicer_and_paraphrase(round_dir, monkeypatch):
    seen = _cli(round_dir, monkeypatch, "--asset-id", "a1")
    assert seen["timeline"] == ("tl", "a1", "league")
    assert callable(seen["transcript_slices"]) and callable(seen["paraphrase"])
    assert callable(seen["energy"])
    assert seen["paraphrase"].keywords == {"asset_id": "a1"}


def test_cli_no_paraphrase_skips_haiku(round_dir, monkeypatch):
    seen = _cli(round_dir, monkeypatch, "--asset-id", "a1", "--no-paraphrase")
    assert seen["paraphrase"] is None
    assert seen["timeline"] == ("tl", "a1", "league")


def test_cli_without_asset_id_adds_nothing(round_dir, monkeypatch):
    seen = _cli(round_dir, monkeypatch)
    assert [seen.get(k) for k in ("timeline", "transcript_slices", "paraphrase", "energy")] == [
        None
    ] * 4


@pytest.fixture
def asset_db(tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / "a.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE assets (id TEXT, filename TEXT, path TEXT, game TEXT, duration_seconds REAL)"
    )
    conn.execute("INSERT INTO assets VALUES ('a1', 'f.mp4', 'C:/x/f.mp4', 'lol', 1900.0)")
    conn.execute(
        "CREATE TABLE transcripts (video_id TEXT, start_seconds REAL, end_seconds REAL, "
        "text TEXT, sentiment_score REAL)"
    )
    conn.execute("INSERT INTO transcripts VALUES ('a1', 10.0, 12.0, 'haha', 0.7)")
    conn.execute("INSERT INTO transcripts VALUES ('a1', 14.0, 15.0, 'go go go', 0.8)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(settings, "database_path", path)
    return path


def test_load_timeline_binds_asset_row_and_duration(asset_db, monkeypatch):
    from ai_video_editor.league import riot_timeline
    from ai_video_editor.league.candidates import riot

    calls = {}

    def fake_inputs(asset, duration, **kw):
        calls["asset"], calls["duration"] = asset, duration
        return None

    def fake_load(asset_id, *, fetch, cache_dir=None):
        calls["asset_id"] = asset_id
        return ("timeline", fetch())

    monkeypatch.setattr(riot, "fetch_timeline_inputs", fake_inputs)
    monkeypatch.setattr(riot_timeline, "load_or_fetch", fake_load)
    assert pub._load_timeline("a1", "league") == ("timeline", None)
    assert calls["asset_id"] == "a1" and calls["duration"] == 1900.0
    assert calls["asset"]["path"] == "C:/x/f.mp4" and calls["asset"]["game"] == "lol"


def test_load_timeline_failure_is_none(asset_db, monkeypatch):
    from ai_video_editor.league import riot_timeline
    from ai_video_editor.league.candidates import riot

    def boom(*a, **k):
        raise RuntimeError("riot down")

    monkeypatch.setattr(riot, "fetch_timeline_inputs", boom)
    monkeypatch.setattr(riot_timeline, "load_or_fetch", lambda a, *, fetch, **k: fetch())
    assert pub._load_timeline("a1", "league") is None
    assert pub._load_timeline("missing", "league") is None
    monkeypatch.setattr(riot_timeline, "load_or_fetch", boom)
    assert pub._load_timeline("a1", "league") is None


def test_load_timeline_skips_non_league(monkeypatch):
    from ai_video_editor.league import riot_timeline

    monkeypatch.setattr(riot_timeline, "load_or_fetch", lambda *a, **k: pytest.fail("fetched"))
    assert pub._load_timeline("a1", "valorant") is None


def test_digest_inputs_energy_uses_window_sentiment(asset_db):
    inputs = pub._digest_inputs("a1", "league", paraphrase=False, load_timeline=lambda a, g: None)
    assert inputs["energy"](9.0, 13.0) == "laughter"  # "haha" in the slice
    assert inputs["energy"](13.5, 16.0) == "hype_rising"  # arousal 0.8, loudness unknown
    assert inputs["energy"](50.0, 60.0) == "unknown"
    assert inputs["transcript_slices"](9.0, 13.0) == "haha"
    assert inputs["paraphrase"] is None


def test_asset_row_never_creates_a_db(tmp_path, monkeypatch):
    missing = tmp_path / "nope.db"
    monkeypatch.setattr(settings, "database_path", missing)
    assert pub._asset_row("a1") is None
    assert not missing.exists()


def _e2e(round_dir, monkeypatch, asset_db, *extra):
    from ai_video_editor.grokbot import drive
    from ai_video_editor.notify import sinks

    paraphrased = []

    def fake_paraphrase(text, *, asset_id=None, client=None):
        paraphrased.append((text, asset_id))
        return "The player hypes up the team."

    monkeypatch.setattr(pub, "voice_paraphrase", fake_paraphrase)
    monkeypatch.setattr(pub, "_load_timeline", lambda asset_id, game: _fake_timeline())
    monkeypatch.setattr(drive, "uploader_from_settings", lambda: None)
    monkeypatch.setattr(sinks, "SINKS", {})
    import sqlite3

    conn = sqlite3.connect(asset_db)
    conn.execute("INSERT INTO transcripts VALUES ('a1', 871.0, 875.0, 'lets go push', 0.9)")
    conn.commit()
    conn.close()
    pub.main([str(round_dir), "--game", "league", "--no-jev", "--asset-id", "a1", *extra])
    saved = dg.Digest.model_validate_json((round_dir / "digest.json").read_text())
    return saved, paraphrased


def test_cli_end_to_end_builds_digest_v2(round_dir, monkeypatch, asset_db, capsys):
    saved, paraphrased = _e2e(round_dir, monkeypatch, asset_db)
    c = saved.clips[0]
    assert paraphrased == [("lets go push", "a1")]  # only clip 1 has speech
    assert c.voice == "The player hypes up the team."
    assert c.energy == "hype_rising" and c.match_confidence == "medium"
    assert [e.type for e in c.events] == ["kill"]
    assert saved.asset_id == "a1"
    assert saved.clips[1].voice == "Mumblewick turns the fight around"


def test_cli_end_to_end_no_paraphrase(round_dir, monkeypatch, asset_db, capsys):
    saved, paraphrased = _e2e(round_dir, monkeypatch, asset_db, "--no-paraphrase")
    assert paraphrased == []
    assert saved.clips[0].voice == "Quillfeather opens with a bold dive"
    assert saved.clips[0].energy == "hype_rising"


def test_leaky_digest_never_reaches_jev(round_dir):
    asked = []

    def ask_many(items):
        asked.append(items)
        return [Answer(value="hype", probability=0.8, backend="jev") for _ in items]

    with pytest.raises(pub.DigestLeakError):
        run(
            round_dir,
            ask_many=ask_many,
            transcript_slices=lambda s, e: "unrelated words",
            paraphrase=lambda t: "they cried kited the wave perf at it",
        )
    assert asked == []


def test_digest_rewritten_with_drive_ids_is_scanned_again(round_dir):
    def leaky_ids(name, subfolder):
        return "sk-ant-" + "a1B2c3D4e5F6g7H8i9J0k1" if name.endswith(".mp4") else ids(name, subfolder)

    up = FakeUploader(ids=leaky_ids)
    with pytest.raises(pub.SecretLeakError):
        run(round_dir, uploader=up)
    assert [u for u in up.uploads if "digest" in str(u)] == []
    assert not (round_dir / "digest.json").exists()
