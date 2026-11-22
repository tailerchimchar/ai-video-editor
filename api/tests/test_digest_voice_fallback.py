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
