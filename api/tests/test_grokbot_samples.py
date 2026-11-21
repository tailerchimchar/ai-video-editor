"""Validate the sample fixtures in docs and tests match the actual schemas."""

import json
from pathlib import Path

from ai_video_editor.grokbot.digest import Digest
from ai_video_editor.notify.models import Event

SAMPLES_DIR = Path(__file__).parent / "fixtures" / "grokbot_samples"


def test_round_published_sample_is_valid_event():
    """The sample round.published event matches the Event schema."""
    sample = json.loads((SAMPLES_DIR / "round_published_sample.json").read_text())
    event = Event.model_validate(sample)

    assert event.event == "round.published"
    assert event.payload["round"] == "2026-11-20_dryrun"
    assert event.payload["game"] == "league"
    assert event.payload["clips"] == 3
    assert len(event.payload["drive"]["clips"]) == 3


def test_digest_sample_is_valid_digest():
    """The sample digest matches the Digest schema."""
    sample = json.loads((SAMPLES_DIR / "digest_sample.json").read_text())
    digest = Digest.model_validate(sample)

    assert digest.game == "league"
    assert digest.round == "2026-11-20_dryrun"
    assert len(digest.clips) == 3

    # Verify padding fields are present
    for clip in digest.clips:
        assert clip.padded_start_s is not None
        assert clip.padded_end_s is not None
        assert clip.moment_start_s == clip.start_s
        assert clip.moment_end_s == clip.end_s


def test_digest_sample_clips_have_correct_padding():
    """The sample digest clips have correct 10-second padding."""
    sample = json.loads((SAMPLES_DIR / "digest_sample.json").read_text())
    digest = Digest.model_validate(sample)

    clip = digest.clips[0]
    # First clip: 870.0 - 10 = 860.0, 892.5 + 10 = 902.5
    assert clip.padded_start_s == 860.0
    assert clip.padded_end_s == 902.5


def test_round_published_sample_has_all_documented_fields():
    """The sample has all the fields documented in queue-format.md."""
    sample = json.loads((SAMPLES_DIR / "round_published_sample.json").read_text())
    payload = sample["payload"]

    required_fields = [
        "round",
        "game",
        "asset_id",
        "compilation_id",
        "compilation_version",
        "drive",
        "clips",
        "skipped_outside_roots",
    ]
    for field in required_fields:
        assert field in payload, f"missing field: {field}"

    assert "digest" in payload["drive"]
    assert "clips" in payload["drive"]
