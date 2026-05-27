"""Spec history journal: append, read, revert primitives.

The journal is best-effort by design — failures are swallowed so a
broken filesystem never breaks an edit. Tests assert the happy path
and verify revert math (step-back semantics, version targeting).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_video_editor.compile_journal import (
    RevertError,
    append_journal,
    journal_path,
    read_journal,
    revert_steps,
    revert_to_version,
    spec_path,
    summarise_journal,
)


def _spec(*clip_ids: str) -> dict:
    return {
        "id": "spec-x",
        "clips": [{"id": cid, "asset_id": "a1"} for cid in clip_ids],
    }


# ----- append + read -----


def test_append_creates_journal_file(tmp_path: Path):
    append_journal(tmp_path, _spec("a"), action="test")
    assert journal_path(tmp_path).is_file()
    entries = read_journal(tmp_path)
    assert len(entries) == 1
    assert entries[0]["action"] == "test"
    assert entries[0]["spec"]["clips"][0]["id"] == "a"


def test_append_is_append_only(tmp_path: Path):
    """Each append adds a line; earlier entries are preserved."""
    append_journal(tmp_path, _spec("a"), action="v1")
    append_journal(tmp_path, _spec("a", "b"), action="v2")
    append_journal(tmp_path, _spec("a", "b", "c"), action="v3")
    entries = read_journal(tmp_path)
    assert [e["action"] for e in entries] == ["v1", "v2", "v3"]
    assert [len(e["spec"]["clips"]) for e in entries] == [1, 2, 3]


def test_read_journal_missing_returns_empty(tmp_path: Path):
    """A folder with no journal yet shouldn't crash — initial compiles
    that haven't written their first entry, plus brand-new folders."""
    assert read_journal(tmp_path) == []


def test_read_journal_skips_corrupt_lines(tmp_path: Path):
    """A partial write or a hand-edited journal with broken lines
    shouldn't take down history — skip and move on."""
    path = journal_path(tmp_path)
    path.write_text(
        '{"action": "ok1", "spec": {"clips": []}}\n'
        "this is not json\n"
        '{"action": "ok2", "spec": {"clips": []}}\n',
        encoding="utf-8",
    )
    entries = read_journal(tmp_path)
    assert [e["action"] for e in entries] == ["ok1", "ok2"]


# ----- summarise -----


def test_summarise_returns_compact_view(tmp_path: Path):
    append_journal(tmp_path, _spec("a"), action="initial_compile", details={"order": "hook"})
    append_journal(tmp_path, _spec("a", "b"), action="insert_clip", details={"asset_id": "x"})
    summary = summarise_journal(tmp_path)
    assert len(summary) == 2
    assert summary[0]["version"] == 1
    assert summary[0]["action"] == "initial_compile"
    assert summary[0]["clip_count"] == 1
    assert summary[1]["clip_count"] == 2
    assert summary[1]["details"]["asset_id"] == "x"
    # No full spec dump in summary — keep payload small for LLM
    assert "spec" not in summary[0]


# ----- revert -----


def test_revert_to_version_writes_spec_to_disk(tmp_path: Path):
    append_journal(tmp_path, _spec("a"), action="v1")
    append_journal(tmp_path, _spec("a", "b"), action="v2")
    restored = revert_to_version(tmp_path, version=1)
    assert restored["clips"][0]["id"] == "a"
    assert len(restored["clips"]) == 1
    # Verify spec.json was written
    on_disk = spec_path(tmp_path).read_text(encoding="utf-8")
    assert "a" in on_disk
    assert '"b"' not in on_disk


def test_revert_steps_goes_back_n(tmp_path: Path):
    """`steps=1` restores the second-most-recent entry (current state
    is the most recent; one step back is the previous spec)."""
    append_journal(tmp_path, _spec("a"), action="v1")
    append_journal(tmp_path, _spec("a", "b"), action="v2")
    append_journal(tmp_path, _spec("a", "b", "c"), action="v3")
    restored, target = revert_steps(tmp_path, steps=1)
    assert target == 2
    assert len(restored["clips"]) == 2


def test_revert_steps_to_beginning(tmp_path: Path):
    append_journal(tmp_path, _spec("a"), action="v1")
    append_journal(tmp_path, _spec("a", "b"), action="v2")
    append_journal(tmp_path, _spec("a", "b", "c"), action="v3")
    restored, target = revert_steps(tmp_path, steps=2)
    assert target == 1
    assert restored["clips"][0]["id"] == "a"
    assert len(restored["clips"]) == 1


def test_revert_errors_when_no_history(tmp_path: Path):
    with pytest.raises(RevertError, match="no journal"):
        revert_steps(tmp_path, steps=1)


def test_revert_errors_when_going_too_far_back(tmp_path: Path):
    append_journal(tmp_path, _spec("a"), action="v1")
    with pytest.raises(RevertError, match="can't go back"):
        revert_steps(tmp_path, steps=5)


def test_revert_to_invalid_version_errors(tmp_path: Path):
    append_journal(tmp_path, _spec("a"), action="v1")
    with pytest.raises(RevertError, match="out of range"):
        revert_to_version(tmp_path, version=99)


# ----- safety: append never raises -----


def test_append_swallows_oserror(tmp_path: Path, monkeypatch):
    """If the journal file is unwritable, append must NOT raise —
    the user's edit already completed."""
    import builtins

    real_open = builtins.open

    def fail_open(path, *a, **kw):
        if str(path).endswith("spec_history.jsonl"):
            raise OSError("simulated disk full")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", fail_open)
    # Should not raise
    append_journal(tmp_path, _spec("a"), action="test")
