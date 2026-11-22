"""Mic-track probe / pick / owner transcript (spec 4.1, R1). No real ffmpeg."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from types import SimpleNamespace

import numpy as np

from ai_video_editor.candidates import mic_track, transcribe_backends
from ai_video_editor.candidates.mic_track import AudioStream, pick_mic_stream
from ai_video_editor.config import settings
from ai_video_editor.grokbot import voice


def _s(i, title=None, handler=None):
    return AudioStream(index=i, codec="aac", channels=2, title=title, handler=handler)


def test_pick_by_label(monkeypatch):
    monkeypatch.setattr(settings, "mic_stream_override", None)
    streams = [_s(0), _s(1, title="Microphone")]
    assert pick_mic_stream(streams, {0: 0.9, 1: 0.0}, min_activity=0.05) == (1, "label")


def test_pick_by_handler_label(monkeypatch):
    monkeypatch.setattr(settings, "mic_stream_override", None)
    streams = [_s(0), _s(1, handler="Voice Input")]
    assert pick_mic_stream(streams, {}, min_activity=0.05) == (1, "label")


def test_pick_by_activity_when_unlabelled(monkeypatch):
    monkeypatch.setattr(settings, "mic_stream_override", None)
    streams = [_s(0), _s(1), _s(2)]
    act = {0: 0.02, 1: 0.31, 2: 0.12}
    assert pick_mic_stream(streams, act, min_activity=0.05) == (1, "activity")


def test_pick_none_when_activity_flat(monkeypatch):
    monkeypatch.setattr(settings, "mic_stream_override", None)
    streams = [_s(0), _s(1)]
    assert pick_mic_stream(streams, {0: 0.20, 1: 0.18}, min_activity=0.05) == (None, "none")


def test_pick_none_single_stream(monkeypatch):
    monkeypatch.setattr(settings, "mic_stream_override", None)
    assert pick_mic_stream([_s(0)], {0: 0.4}, min_activity=0.05) == (None, "none")


def test_override_wins(monkeypatch):
    monkeypatch.setattr(settings, "mic_stream_override", 3)
    assert pick_mic_stream([_s(0), _s(1, title="Mic")], {}, min_activity=0.05) == (3, "override")


def test_extract_audio_maps_stream(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=1, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    transcribe_backends._extract_audio("v.mp4", tmp_path / "a.wav", stream_index=3)
    cmd = seen["cmd"]
    i = cmd.index("-map")
    assert cmd[i : i + 2] == ["-map", "0:a:3"]


def test_extract_audio_default_has_no_map(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=1, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    transcribe_backends._extract_audio("v.mp4", tmp_path / "a.wav")
    assert "-map" not in seen["cmd"]


def test_probe_audio_streams_parses_tags(monkeypatch):
    payload = {
        "streams": [
            {"index": 1, "codec_name": "aac", "channels": 2, "tags": {"title": "Game"}},
            {"index": 2, "codec_name": "aac", "channels": 1, "tags": {"handler_name": "Mic"}},
        ]
    }

    def fake_run(cmd, **kw):
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    got = mic_track.probe_audio_streams("v.mp4")
    # index is the audio-relative position (0:a:N), not ffprobe's global index
    assert [(s.index, s.title, s.handler) for s in got] == [(0, "Game", None), (1, None, "Mic")]


def test_probe_failure_returns_empty(monkeypatch):
    def boom(cmd, **kw):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    assert mic_track.probe_audio_streams("v.mp4") == []


def test_speech_activity_fraction(monkeypatch):
    loud = np.ones(16000, dtype=np.int16) * 8000
    quiet = np.zeros(16000, dtype=np.int16)
    pcm = np.concatenate([loud, quiet]).tobytes()  # half loud

    def fake_run(cmd, **kw):
        if "-show_entries" in cmd:
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"format": {"duration": "200"}}), stderr=""
            )
        return SimpleNamespace(returncode=0, stdout=pcm, stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert abs(mic_track.speech_activity("v.mp4", 1) - 0.5) < 0.01


def test_transcript_slice_filters_speaker(tmp_path):
    db = tmp_path / "t.db"
    c = sqlite3.connect(db)
    c.execute(
        "CREATE TABLE transcripts (id TEXT, video_id TEXT, start_seconds REAL, end_seconds REAL, "
        "text TEXT, sentiment_score REAL, words TEXT, created_at TEXT, speaker TEXT)"
    )
    c.execute("INSERT INTO transcripts VALUES ('1','v',0,5,'mixed words',NULL,NULL,'x',NULL)")
    c.execute("INSERT INTO transcripts VALUES ('2','v',0,5,'owner words',NULL,NULL,'x','owner')")
    c.commit()
    c.close()
    assert voice.transcript_slice(db, "v", 0, 5, speaker="owner") == "owner words"
    assert "mixed words" in voice.transcript_slice(db, "v", 0, 5)


def test_transcribe_mic_none_inserts_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    monkeypatch.setattr(settings, "mic_stream_override", None)
    monkeypatch.setattr(mic_track, "probe_audio_streams", lambda p: [_s(0)])
    segs, meta = mic_track.transcribe_mic({"id": "a1", "path": "v.mp4"}, 60.0)
    assert segs == [] and meta["method"] == "none" and meta["stream_index"] is None


def test_transcribe_mic_runs_backend(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    monkeypatch.setattr(settings, "mic_stream_override", None)
    monkeypatch.setattr(mic_track, "probe_audio_streams", lambda p: [_s(0), _s(1, title="Mic")])
    monkeypatch.setattr(mic_track, "speech_activity", lambda p, i, **kw: 0.2)
    seen = {}

    def fake_extract(path, wav, stream_index=None):
        seen["idx"] = stream_index
        wav.write_bytes(b"RIFF")
        return True

    class FakeBackend:
        name = "fake"

        def transcribe_raw(self, p, **kw):
            return [{"start_seconds": 1.0, "end_seconds": 2.0, "text": " hello ", "words": []}]

    monkeypatch.setattr(mic_track, "_extract_audio", fake_extract)
    monkeypatch.setattr(mic_track, "select_backend", lambda: FakeBackend())
    segs, meta = mic_track.transcribe_mic({"id": "a1", "path": "v.mp4"}, 60.0)
    assert seen["idx"] == 1
    assert segs[0]["text"] == "hello"
    assert meta["method"] == "label" and meta["stream_index"] == 1
    saved = json.loads((tmp_path / "mic" / "a1.json").read_text())
    assert saved["stream_index"] == 1
