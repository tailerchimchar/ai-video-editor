import json

from ai_video_editor.grokbot import digest as dg


def test_digest_voice_falls_back_to_beat_with_expert_ref(tmp_path):
    (tmp_path / "clips.json").write_text(
        json.dumps(
            [
                {
                    "file": "06_kill_23m05s.mp4",
                    "event": "kill",
                    "start_seconds": 1380.0,
                    "end_seconds": 1395.0,
                }
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "gaming-expert.json").write_text(
        json.dumps([{"clip_ref": "06_kill_23m05s", "story_beats": [{"beat": "Neeko pick"}]}]),
        encoding="utf-8",
    )
    clip = dg.build_digest(tmp_path, game="lol").clips[0]
    assert clip.ref == "23m05s"
    assert clip.voice == "Neeko pick"


# --- I6a: the beat fallback is self-checked against the clip's own speech ---

_QUOTING_BEAT = (
    "setup: team is moving to take the enemy MissFortune; a teammate notes she has no flash"
)
_SPEECH = "go go he has no flash"  # shares the 12-char "has no flash" with the beat


def _quoting_round(tmp_path):
    (tmp_path / "clips.json").write_text(
        json.dumps([{"file": "04_kill_18m42s.mp4", "event": "kill",
                     "start_seconds": 1122.0, "end_seconds": 1154.0}]),
        encoding="utf-8",
    )  # fmt: skip
    (tmp_path / "gaming-expert.json").write_text(
        json.dumps([{"clip_ref": "04_kill_18m42s", "story_beats": [{"beat": _QUOTING_BEAT}]}]),
        encoding="utf-8",
    )
    (tmp_path / "transcript.json").write_text(json.dumps([{"text": _SPEECH}]), encoding="utf-8")
    return tmp_path


def test_beat_quoting_the_mixed_slice_drops_to_energy(tmp_path):
    rd = _quoting_round(tmp_path)
    d = dg.build_digest(
        rd,
        game="lol",
        transcript_slices=lambda s, e: _SPEECH if s == 1122.0 else "",
        energy=lambda s, e: "hype_rising",
    )
    assert d.clips[0].voice == "hype_rising"
    assert dg.digest_leaks(d, rd / "transcript.json") == []


def test_beat_quoting_an_owner_row_drops_to_energy(tmp_path):
    rd = _quoting_round(tmp_path)
    owner = [{"text": _SPEECH, "start_seconds": 1130.0, "end_seconds": 1133.0,
              "sentiment_score": 0.1}]  # fmt: skip
    d = dg.build_digest(rd, game="lol", transcript_slices=lambda s, e: "", owner_rows=owner)
    assert d.clips[0].voice == "unknown"


def test_clean_beat_is_still_used(tmp_path):
    rd = _quoting_round(tmp_path)
    d = dg.build_digest(rd, game="lol", transcript_slices=lambda s, e: "totally different words")
    assert d.clips[0].voice == _QUOTING_BEAT


def test_commentary_beat_quoting_speech_drops_to_energy(tmp_path):
    rd = _quoting_round(tmp_path)
    owner = [{"text": "I am planning thing number one right now", "start_seconds": 1170.0,
              "end_seconds": 1176.0, "sentiment_score": 0.5}]  # fmt: skip
    d = dg.build_digest(
        rd,
        game="lol",
        owner_rows=owner,
        transcript_slices=lambda s, e: _SPEECH if s == 1122.0 else "",
        energy=lambda s, e: "calm",
    )
    [item] = d.commentary
    assert item.voice == "calm"
    assert item.attached_beat is None
    assert dg.digest_leaks(d, rd / "transcript.json") == []
