import json
import shutil
from pathlib import Path

import pytest

from ai_video_editor.decisions.models import Answer
from ai_video_editor.grokbot import digest as dg
from ai_video_editor.taxonomy import Format

FIXTURE = Path(__file__).parent / "fixtures" / "panel_round"


@pytest.fixture
def fixture_round(tmp_path):
    dest = tmp_path / "2026-01-01_fixture"
    shutil.copytree(FIXTURE, dest)
    return dest


def test_build_digest_reads_clips_and_experts(fixture_round):
    d = dg.build_digest(fixture_round, game="lol")
    assert d.game == "lol"
    assert d.round == "2026-01-01_fixture"
    assert len(d.clips) == 3
    c = d.clips[0]
    assert c.ref == "14m32s"
    assert c.panel["gaming_quality"] == 4
    assert c.panel["hook_verdict"] == "pass"
    assert c.critic == "ship_it"
    story = json.loads((fixture_round / "gaming-expert.json").read_text())[0]["story_beats"][0]
    assert c.voice.startswith(story)
    assert c.copy["title"] == "Wobble Wizard Strikes"


def test_missing_expert_entry_falls_back(fixture_round):
    c = dg.build_digest(fixture_round, game="lol").clips[2]
    assert c.panel["gaming_quality"] is None
    assert c.panel["hook_verdict"] is None
    assert c.voice == ""
    assert c.copy is None


def test_digest_never_leaks_transcript(fixture_round):
    transcript = fixture_round / "transcript.json"
    assert dg.digest_leaks(dg.build_digest(fixture_round, game="lol"), transcript) == []
    d = dg.build_digest(fixture_round, game="lol")
    line = "Zorblax kited the wave perfectly before the flip"
    d.clips[0].voice = line
    assert line.casefold() in dg.digest_leaks(d, transcript)


def test_build_digest_never_opens_transcript(fixture_round, monkeypatch):
    real_open, real_read = Path.open, Path.read_text

    def guard(orig):
        def inner(self, *a, **k):
            assert "transcript.json" not in str(self)
            return orig(self, *a, **k)

        return inner

    monkeypatch.setattr(Path, "open", guard(real_open))
    monkeypatch.setattr(Path, "read_text", guard(real_read))
    dg.build_digest(fixture_round, game="lol")


def test_annotate_with_jev_uses_ask_many(fixture_round):
    d = dg.build_digest(fixture_round, game="lol")
    d.clips = d.clips[:1]

    def fake(items):
        return [
            Answer(value=Format.funny, probability=0.8, backend="jev")
            if q is dg._FORMAT_QUESTION
            else Answer(value=True, probability=0.9, backend="jev")
            for q, _ in items
        ]

    out = dg.annotate_with_jev(d, ask_many=fake)
    assert out.clips[0].panel["format"] == "funny"
    assert out.clips[0].panel["include_p"] == 0.9


def test_digest_leaks_catches_fragment(fixture_round):
    d = dg.build_digest(fixture_round, game="lol")
    d.clips[0].voice = "he said: kited the wave perf, wow"  # 15-char fragment "kited the wave "
    leaks = dg.digest_leaks(d, fixture_round / "transcript.json")
    assert any("kited the wave perf" in x for x in leaks)


def test_digest_leaks_catches_quoted_line(fixture_round):
    line = 'she yelled "absolutely unreal" at the screen'
    path = fixture_round / "transcript.json"
    path.write_text(json.dumps([{"start_seconds": 1, "end_seconds": 2, "text": line}]))
    d = dg.build_digest(fixture_round, game="lol")
    d.clips[0].voice = line
    assert line.casefold() in dg.digest_leaks(d, path)


def test_digest_carries_optional_compilation_fields():
    from ai_video_editor.grokbot.digest import Digest

    d = Digest(game="g", round="r", clips=[], built_at="t")
    assert d.compilation_id is None and d.compilation_version is None
    d2 = Digest(
        game="g", round="r", clips=[], built_at="t", compilation_id="c", compilation_version="2"
    )
    assert (d2.compilation_id, d2.compilation_version) == ("c", "2")


# --- digest v2: events, game state, voice, energy, confidence ---

from ai_video_editor.league.riot_timeline import (  # noqa: E402
    OwnerFrame,
    RiotTimeline,
    TimelineEvent,
)


def _timeline(confidence="high", offset_source=None, game_end_s=None):
    # Clip 1 spans recording 870.0-892.5; offset 10 s -> game time 860-882.5.
    events = [
        TimelineEvent("elite", 860.0, 870.0, ["ally Thresh"], {"monsterType": "DRAGON"}),
        TimelineEvent("kill", 870.0, 880.0, ["you (Jhin)", "enemy Diana"], {}),
        TimelineEvent("death", 890.0, 900.0, ["enemy Diana", "you (Jhin)"], {}),
        TimelineEvent("assist", 875.0, None, ["ally Thresh"], {}),
    ]
    frames = [OwnerFrame(m, 400 * m, 7 * m, min(18, m)) for m in range(0, 31)]
    return RiotTimeline(
        match_id="EUW1_1",
        confidence=confidence,
        offset_s=10.0,
        game_end_s=game_end_s,
        events=events,
        owner_frames=frames,
        offset_source=offset_source,
    )


def test_events_clipped_to_window(fixture_round):
    c = dg.build_digest(fixture_round, game="lol", timeline=_timeline()).clips[0]
    assert [(e.type, e.t_in_clip, e.actors) for e in c.events] == [
        ("elite", 0.0, ["ally Thresh"]),
        ("kill", 10.0, ["you (Jhin)", "enemy Diana"]),
    ]


def test_game_state_from_nearest_owner_frames(fixture_round):
    c = dg.build_digest(fixture_round, game="lol", timeline=_timeline()).clips[0]
    gs = c.game_state
    assert gs.game_time_s == 860.0
    assert (gs.gold_start, gs.cs_start) == (400 * 14, 7 * 14)  # minute 14 nearest 860 s
    assert (gs.gold_end, gs.cs_end, gs.level) == (400 * 15, 7 * 15, 15)  # minute 15
    assert gs.game_ended_in_clip is False
    assert c.match_confidence == "high"


def test_game_ended_in_clip(fixture_round):
    d = dg.build_digest(fixture_round, game="lol", timeline=_timeline(game_end_s=1810.0))
    assert [c.game_state.game_ended_in_clip for c in d.clips] == [False, False, True]


def test_game_state_without_owner_frames_is_unknown_numbers(fixture_round):
    tl = _timeline()
    tl.owner_frames = []
    gs = dg.build_digest(fixture_round, game="lol", timeline=tl).clips[0].game_state
    assert gs.game_time_s == 860.0
    assert (gs.gold_start, gs.gold_end, gs.cs_start, gs.cs_end, gs.level) == (None,) * 5


def test_low_confidence_has_no_game_state(fixture_round):
    c = dg.build_digest(fixture_round, game="lol", timeline=_timeline("low")).clips[0]
    assert c.game_state is None
    assert c.match_confidence == "low"


def test_sync_only_caps_confidence_at_medium(fixture_round):
    tl = _timeline("high", offset_source="sync_only")
    assert dg.build_digest(fixture_round, game="lol", timeline=tl).clips[0].match_confidence == (
        "medium"
    )


def test_no_timeline_defaults(fixture_round):
    c = dg.build_digest(fixture_round, game="lol").clips[0]
    assert (c.events, c.game_state, c.energy, c.match_confidence) == ([], None, "unknown", "none")


def test_voice_from_paraphrase_of_transcript_slice(fixture_round):
    asked = []

    def slices(s, e):
        asked.append((s, e))
        return "the words" if s == 870.0 else ""

    d = dg.build_digest(
        fixture_round,
        game="lol",
        transcript_slices=slices,
        paraphrase=lambda t: f"A sentence about {len(t)} chars.",
    )
    assert asked[0] == (870.0, 892.5)
    assert d.clips[0].voice == "A sentence about 9 chars."
    # empty slice -> no paraphrase; falls back to the story beat
    assert d.clips[1].voice == "Mumblewick turns the fight around"


def test_paraphrase_failure_falls_back_to_beat(fixture_round):
    d = dg.build_digest(
        fixture_round, game="lol", transcript_slices=lambda s, e: "x", paraphrase=lambda t: ""
    )
    assert d.clips[0].voice == "Quillfeather opens with a bold dive"


def test_paraphrase_that_quotes_its_slice_is_dropped(fixture_round):
    d = dg.build_digest(
        fixture_round,
        game="lol",
        transcript_slices=lambda s, e: "honestly unbelievable flash",
        paraphrase=lambda t: "The player shouts unbelievable flash.",
    )
    assert d.clips[0].voice == "Quillfeather opens with a bold dive"


def test_voice_reads_beat_text_from_dict_beats(fixture_round):
    path = fixture_round / "gaming-expert.json"
    data = json.loads(path.read_text())
    data[0]["story_beats"] = [{"t": 3.0, "beat": "A patient setup pays off"}]
    path.write_text(json.dumps(data))
    assert dg.build_digest(fixture_round, game="lol").clips[0].voice == "A patient setup pays off"


def test_voice_empty_when_nothing(fixture_round):
    path = fixture_round / "gaming-expert.json"
    data = json.loads(path.read_text())
    data[0]["story_beats"] = [{"t": 3.0}]
    data[1]["story_beats"] = []
    path.write_text(json.dumps(data))
    d = dg.build_digest(fixture_round, game="lol")
    assert [c.voice for c in d.clips] == ["", "", ""]


def test_energy_callable(fixture_round):
    labels = {870.0: "laughter", 1255.0: "nonsense"}
    d = dg.build_digest(fixture_round, game="lol", energy=lambda s, e: labels.get(s, "calm"))
    assert [c.energy for c in d.clips] == ["laughter", "unknown", "calm"]


def test_voice_leak_checked_at_eight_chars(fixture_round):
    path = fixture_round / "transcript.json"
    d = dg.build_digest(fixture_round, game="lol")
    d.clips[0].voice = "he said wave perf then left"  # 9-char fragment "wave perf"
    assert any("wave perf" in x for x in dg.digest_leaks(d, path))
    d.clips[0].voice = ""
    d.clips[0].copy = {"title": "wave perf"}  # other fields keep min_len=12
    assert dg.digest_leaks(d, path) == []


def test_queue_format_digest_example_parses():
    import re

    doc = Path(__file__).parents[2] / "docs" / "grokbot" / "queue-format.md"
    (block,) = re.findall(r"```json digest\n(.*?)```", doc.read_text(encoding="utf-8"), re.DOTALL)
    d = dg.Digest.model_validate_json(block)
    c = d.clips[0]
    assert c.events and c.game_state is not None and c.voice
    assert c.energy != "unknown" and c.match_confidence != "none"
    raw = json.loads(block)["clips"][0]
    assert set(raw) == set(dg.DigestClip.model_fields)  # the example shows every field
