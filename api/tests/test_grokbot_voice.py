"""Voice helpers for the digest: transcript slice, Haiku paraphrase, energy label."""

import sqlite3
from types import SimpleNamespace

import pytest

from ai_video_editor import anthropic_surface
from ai_video_editor.anthropic_surface import SpendCapError
from ai_video_editor.config import settings
from ai_video_editor.grokbot import voice

SYSTEM_RULE = (
    "One sentence describing what the player says or reacts to. No quotes, no names, no slurs."
)


class FakeMessages:
    def __init__(self, reply="The player cheers as the fight turns.", exc=None, stop="end_turn"):
        self.reply, self.exc, self.stop, self.calls = reply, exc, stop, []

    def create(self, **kw):
        self.calls.append(kw)
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.reply)],
            usage=SimpleNamespace(input_tokens=40, output_tokens=12),
            model=kw["model"],
            stop_reason=self.stop,
        )


class FakeClient:
    def __init__(self, **kw):
        self.messages = FakeMessages(**kw)


@pytest.fixture
def ledger(monkeypatch):
    seen = {"checked": [], "recorded": []}
    monkeypatch.setattr(voice, "check_budget", lambda a: seen["checked"].append(a))
    monkeypatch.setattr(voice, "record_spend", lambda *a, **k: seen["recorded"].append(a) or 0.0001)
    monkeypatch.setattr(anthropic_surface, "_calls", {})
    return seen


def test_paraphrase_one_haiku_call_with_the_rule(ledger):
    client = FakeClient()
    out = voice.voice_paraphrase("let's go that was insane", asset_id="a1", client=client)
    assert out == "The player cheers as the fight turns."
    (call,) = client.messages.calls
    assert call["model"] == settings.paraphrase_model == "claude-haiku-4-5"
    assert call["max_tokens"] == 60
    assert call["system"] == SYSTEM_RULE
    assert call["messages"] == [{"role": "user", "content": "let's go that was insane"}]
    assert ledger["checked"] == ["a1"]
    asset, feature, model, usage = ledger["recorded"][0]
    assert (asset, feature, model) == ("a1", "paraphrase", "claude-haiku-4-5")
    assert usage.output_tokens == 12


def test_paraphrase_strips_quote_marks(ledger):
    client = FakeClient(reply='  "The player laughs at the misplay."  ')
    assert voice.voice_paraphrase("haha", client=client) == "The player laughs at the misplay."


@pytest.mark.parametrize("text", ["", "   "])
def test_paraphrase_empty_input_makes_no_call(ledger, text):
    client = FakeClient()
    assert voice.voice_paraphrase(text, client=client) == ""
    assert client.messages.calls == []


def test_paraphrase_any_exception_is_empty(ledger):
    client = FakeClient(exc=RuntimeError("boom"))
    assert voice.voice_paraphrase("words", client=client) == ""
    assert ledger["recorded"] == []


def test_paraphrase_over_budget_is_empty_and_makes_no_call(ledger, monkeypatch):
    def capped(asset_id):
        raise SpendCapError("over")

    monkeypatch.setattr(voice, "check_budget", capped)
    client = FakeClient()
    assert voice.voice_paraphrase("words", asset_id="a1", client=client) == ""
    assert client.messages.calls == []


def test_ledger_failure_keeps_the_answer(ledger, monkeypatch):
    def broken(*a, **k):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(voice, "record_spend", broken)
    assert voice.voice_paraphrase("words", asset_id="a1", client=FakeClient()) != ""


def test_paraphrase_process_cap(ledger, monkeypatch):
    monkeypatch.setattr(settings, "paraphrase_max_calls", 1)
    client = FakeClient()
    assert voice.voice_paraphrase("one", client=client) != ""
    assert voice.voice_paraphrase("two", client=client) == ""
    assert len(client.messages.calls) == 1


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE transcripts (id TEXT, video_id TEXT, start_seconds REAL, "
        "end_seconds REAL, text TEXT, sentiment_score REAL)"
    )
    rows = [
        ("1", "a1", 5.0, 9.0, "before window", 0.1),
        ("2", "a1", 12.0, 15.0, "second line", 0.8),
        ("3", "a1", 9.0, 11.0, "first line", 0.4),
        ("4", "a1", 20.0, 25.0, "touches end", None),
        ("5", "a2", 12.0, 15.0, "other asset", 0.9),
    ]
    conn.executemany("INSERT INTO transcripts VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return path


def test_transcript_slice_overlap_in_time_order(db):
    assert voice.transcript_slice(db, "a1", 10.0, 20.0) == "first line second line"


def test_transcript_slice_empty_when_nothing_or_no_db(db, tmp_path):
    assert voice.transcript_slice(db, "a1", 100.0, 120.0) == ""
    assert voice.transcript_slice(tmp_path / "missing.db", "a1", 0.0, 10.0) == ""


def test_window_sentiment_mean_ignores_nulls(db):
    assert voice.window_sentiment(db, "a1", 10.0, 25.0) == pytest.approx(0.6)
    assert voice.window_sentiment(db, "a1", 21.0, 24.0) is None
    assert voice.window_sentiment(db, "a1", 100.0, 120.0) is None


@pytest.mark.parametrize(
    ("reply", "out"),
    [
        ("The player cheers. Then the player starts to", "The player cheers."),
        ("The player is shocked! And then", "The player is shocked!"),
        ("The player starts describing the whole", ""),
    ],
)
def test_truncated_paraphrase_keeps_last_full_sentence(ledger, reply, out):
    client = FakeClient(reply=reply, stop="max_tokens")
    assert voice.voice_paraphrase("words", client=client) == out


# energy_label: arousal is VADER arousal in [0, 1]; loudness is optional.
@pytest.mark.parametrize(
    ("loudness", "arousal", "text", "label"),
    [
        (None, 0.6, "", "hype_rising"),
        (-20.0, 0.9, "", "hype_rising"),
        (-25.0, 0.9, "", "unknown"),  # aroused but quiet: not hype
        (None, 0.59, "", "unknown"),
        (None, 0.2, "", "calm"),
        (-10.0, 0.0, "", "calm"),
        (-31.0, None, "", "calm"),
        (-31.0, 0.5, "", "calm"),
        (-30.0, 0.5, "", "unknown"),
        (None, None, "", "unknown"),
        (None, 0.4, "HAHAHA no way", "laughter"),
        (None, 0.9, "that was so bad lol", "laughter"),
        (None, 0.1, "lmao what", "laughter"),
        (None, None, "[laughter] okay", "laughter"),
        (None, 0.4, "lollipop solo", "unknown"),  # no word match
    ],
)
def test_energy_label_table(loudness, arousal, text, label):
    assert voice.energy_label(loudness, arousal, text) == label


def test_energy_never_panic():
    labels = {
        voice.energy_label(ld, a, t)
        for ld in (None, -40.0, -25.0, -10.0)
        for a in (None, 0.0, 0.3, 0.7, 1.0)
        for t in ("", "help help")
    }
    assert "panic" not in labels
