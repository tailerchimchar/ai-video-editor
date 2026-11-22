import json

from ai_video_editor.grokbot import digest as dg


def _round(tmp_path):
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
        json.dumps([{"clip_ref": "23m05s", "story_beats": [{"beat": "Neeko pick"}]}]),
        encoding="utf-8",
    )
    return tmp_path


def _rows(n, start=100.0):
    return [
        {
            "start_seconds": start + i * 50,
            "end_seconds": start + i * 50 + 6,
            "text": f"I am planning thing number {i} right now",
            "sentiment_score": i / 10,
        }
        for i in range(n)
    ]


def test_digest_commentary_section_capped_at_5(tmp_path):
    d = dg.build_digest(_round(tmp_path), game="lol", owner_rows=_rows(8))
    assert len(d.commentary) == 5
    assert len(d.clips) == 1  # commentary never lands in clips
    assert d.commentary[0].ref == "7m30s"  # row 7 (arousal 0.7) starts at 450s


def test_commentary_voice_is_paraphrase_or_energy_plus_beat(tmp_path):
    rd = _round(tmp_path)
    plain = dg.build_digest(
        rd, game="lol", owner_rows=_rows(1, start=1400.0), energy=lambda a, b: "calm"
    )
    item = plain.commentary[0]
    assert item.energy == "calm"
    assert item.attached_beat == "Neeko pick"
    assert item.voice == "calm: Neeko pick"

    bare = dg.build_digest(rd, game="lol", owner_rows=_rows(1, start=5000.0))
    assert bare.commentary[0].attached_beat == "Neeko pick"  # nearest clip's beat

    d = dg.build_digest(
        rd,
        game="lol",
        owner_rows=_rows(1, start=1400.0),
        transcript_slices=lambda a, b: "I am planning thing number 0 right now",
        paraphrase=lambda s: "Talks through an upcoming plan",
    )
    assert d.commentary[0].voice == "Talks through an upcoming plan"


def test_commentary_without_clips_uses_energy_alone(tmp_path):
    (tmp_path / "clips.json").write_text("[]", encoding="utf-8")
    d = dg.build_digest(tmp_path, game="lol", owner_rows=_rows(1), energy=lambda a, b: "panic")
    assert d.commentary[0].attached_beat is None
    assert d.commentary[0].voice == "panic"


def test_commentary_leak_check(tmp_path):
    rd = _round(tmp_path)
    (rd / "transcript.json").write_text(
        json.dumps([{"text": "Zorblax kited the wave perfectly"}]), encoding="utf-8"
    )
    d = dg.build_digest(rd, game="lol", owner_rows=_rows(1, start=1400.0))
    assert dg.digest_leaks(d, rd / "transcript.json") == []
    d.commentary[0].voice = "he said zorblax kited the wave perfectly"
    assert dg.digest_leaks(d, rd / "transcript.json") != []
