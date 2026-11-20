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
