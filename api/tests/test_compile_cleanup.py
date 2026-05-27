"""Orphan-part cleanup: pure validity logic + IO sweep + safety guardrails.

Tests use a monkeypatched workspace dir + a `<workspace>/compilations/<id>/`
folder layout so the workspace-containment safety check passes naturally
(mirrors production). `render_spec` integration is covered by stubbing
the encode + concat steps so a single test exercises the full
"edit → render → auto-cleanup" wiring.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ai_video_editor import compile_cleanup as cleanup_mod
from ai_video_editor.compile_cleanup import (
    CleanupReport,
    CleanupSafetyError,
    cleanup_compilation,
    find_orphans,
    is_safe_compilation_folder,
    safe_cleanup_for_render,
    valid_part_filenames,
)

# ----- fixtures -----


@pytest.fixture
def comp_folder(tmp_path, monkeypatch):
    """A `<workspace>/compilations/<id>/` folder that passes the
    workspace-containment safety check. Tests can drop _parts/ files
    inside and call cleanup without `enforce_safety=False`."""
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    folder = tmp_path / "compilations" / "test-comp-01"
    folder.mkdir(parents=True)
    return folder


# ----- helpers -----


def _spec(*clip_ids: str) -> dict:
    """Minimal spec with N clips at fixed UUIDs (callers can pass 8-char ids)."""
    return {
        "id": "spec-x",
        "clips": [
            {
                "id": cid + ("0" * max(0, 36 - len(cid))),
                "asset_id": "a1",
                "asset_path": "/fake.mp4",
                "start_seconds": 0.0,
                "end_seconds": 5.0,
                "event_type": "clip",
                "caption_segments": [],
                "effects": [],
                "caption_mode": "segment",
            }
            for cid in clip_ids
        ],
    }


def _make_parts(folder: Path, *names: str) -> list[Path]:
    """Drop empty mp4 files into `<folder>/_parts/` and return their paths."""
    parts_dir = folder / "_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for n in names:
        p = parts_dir / n
        p.write_bytes(b"\x00" * 16)
        paths.append(p)
    return paths


# ----- valid_part_filenames (pure) -----


def test_valid_part_filenames_keeps_both_variants_per_clip():
    """Each clip keeps its no-label file AND its current-position label
    file. That's how the 'instant label flip' cache stays warm."""
    spec = _spec("aaaaaaaa", "bbbbbbbb")
    valid = valid_part_filenames(spec)
    assert valid == {
        "part_aaaaaaaa.mp4",
        "part_aaaaaaaa_n1.mp4",
        "part_bbbbbbbb.mp4",
        "part_bbbbbbbb_n2.mp4",
    }


def test_valid_part_filenames_empty_spec():
    assert valid_part_filenames({"clips": []}) == set()
    assert valid_part_filenames({}) == set()


def test_valid_part_filenames_skips_clips_without_id():
    spec = {"clips": [{"id": "aaaaaaaa"}, {}, {"id": ""}]}
    assert valid_part_filenames(spec) == {"part_aaaaaaaa.mp4", "part_aaaaaaaa_n1.mp4"}


# ----- find_orphans (IO) -----


def test_find_orphans_returns_files_not_in_valid_set(comp_folder: Path):
    spec = _spec("aaaaaaaa")
    _make_parts(
        comp_folder,
        "part_aaaaaaaa.mp4",
        "part_aaaaaaaa_n1.mp4",
        "part_bbbbbbbb.mp4",
        "part_aaaaaaaa_n7.mp4",
    )
    orphans, skipped = find_orphans(comp_folder, spec)
    assert {p.name for p in orphans} == {"part_bbbbbbbb.mp4", "part_aaaaaaaa_n7.mp4"}
    assert skipped == []


def test_find_orphans_missing_parts_dir_returns_empty(comp_folder: Path):
    orphans, skipped = find_orphans(comp_folder, _spec("aaaaaaaa"))
    assert orphans == []
    assert skipped == []


def test_find_orphans_ignores_unrelated_filenames(comp_folder: Path):
    """Files that don't match the strict `part_<id8>(_nN)?.mp4` shape
    are left alone — they could be user notes, debris, or future
    cache shapes we don't own yet."""
    _make_parts(
        comp_folder,
        "part_aaaaaaaa.mp4",  # valid: kept
        "notes.txt",  # ignored shape
        "compilation.mp4",  # ignored shape
        "part_short.mp4",  # ignored — id8 must be 8 hex chars
        "part_ZZZZZZZZ.mp4",  # ignored — non-hex id
        "part_aaaaaaaa.txt",  # ignored — wrong extension
    )
    orphans, skipped = find_orphans(comp_folder, _spec("aaaaaaaa"))
    assert orphans == []
    assert skipped == []


def test_find_orphans_never_recurses(comp_folder: Path):
    """A subdir under `_parts/` (legacy, user-created, whatever) must
    not be descended into — we only operate on direct children."""
    parts_dir = comp_folder / "_parts"
    parts_dir.mkdir()
    (parts_dir / "subdir").mkdir()
    (parts_dir / "subdir" / "part_zzzzzzzz.mp4").write_bytes(b"\x00")
    (parts_dir / "part_aaaaaaaa.mp4").write_bytes(b"\x00")
    orphans, _ = find_orphans(comp_folder, _spec("aaaaaaaa"))
    assert orphans == []


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks require admin on Windows")
def test_find_orphans_skips_symlinks(comp_folder: Path, tmp_path: Path):
    """A symlink-shaped 'orphan' is never deleted — that's the
    standard symlink-into-system-files defense."""
    target = tmp_path / "target.mp4"
    target.write_bytes(b"\x00")
    parts_dir = comp_folder / "_parts"
    parts_dir.mkdir()
    link = parts_dir / "part_zzzzzzzz.mp4"
    link.symlink_to(target)
    orphans, skipped = find_orphans(comp_folder, _spec("aaaaaaaa"))
    assert orphans == []
    assert link.name in skipped[0].name


# ----- cleanup_compilation (IO) -----


def test_cleanup_deletes_orphans_keeps_valid(comp_folder: Path):
    spec = _spec("aaaaaaaa", "bbbbbbbb")
    _make_parts(
        comp_folder,
        "part_aaaaaaaa.mp4",
        "part_aaaaaaaa_n1.mp4",
        "part_bbbbbbbb.mp4",
        "part_bbbbbbbb_n2.mp4",
        "part_cccccccc.mp4",  # orphan
        "part_aaaaaaaa_n9.mp4",  # orphan
    )
    report = cleanup_compilation(comp_folder, spec)
    assert isinstance(report, CleanupReport)
    assert set(report.deleted_files) == {"part_cccccccc.mp4", "part_aaaaaaaa_n9.mp4"}
    assert report.kept_files == 4
    assert report.deleted_bytes > 0
    assert report.dry_run is False
    surviving = {p.name for p in (comp_folder / "_parts").iterdir()}
    assert surviving == {
        "part_aaaaaaaa.mp4",
        "part_aaaaaaaa_n1.mp4",
        "part_bbbbbbbb.mp4",
        "part_bbbbbbbb_n2.mp4",
    }


def test_cleanup_dry_run_lists_but_does_not_delete(comp_folder: Path):
    spec = _spec("aaaaaaaa")
    _make_parts(comp_folder, "part_aaaaaaaa.mp4", "part_deadbeef.mp4")
    report = cleanup_compilation(comp_folder, spec, dry_run=True)
    assert report.deleted_files == ["part_deadbeef.mp4"]
    assert report.dry_run is True
    assert (comp_folder / "_parts" / "part_deadbeef.mp4").exists()


def test_cleanup_is_idempotent(comp_folder: Path):
    """Second call has nothing to do — important because render_spec
    runs it on EVERY render and we don't want spurious work or errors."""
    spec = _spec("aaaaaaaa")
    _make_parts(comp_folder, "part_aaaaaaaa.mp4", "part_deadbee0.mp4")
    cleanup_compilation(comp_folder, spec)
    second = cleanup_compilation(comp_folder, spec)
    assert second.deleted_files == []
    assert second.errors == []


def test_cleanup_loads_spec_from_disk_when_not_supplied(comp_folder: Path):
    """The MCP tool only knows the compilation folder, so cleanup
    must be able to read `spec.json` itself when called externally."""
    spec = _spec("aaaaaaaa")
    (comp_folder / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    _make_parts(comp_folder, "part_aaaaaaaa.mp4", "part_deadbee0.mp4")
    report = cleanup_compilation(comp_folder)
    assert report.deleted_files == ["part_deadbee0.mp4"]


def test_cleanup_no_parts_dir_is_noop(comp_folder: Path):
    spec = _spec("aaaaaaaa")
    report = cleanup_compilation(comp_folder, spec)
    assert report.deleted_files == []
    assert report.kept_files == 0
    assert report.errors == []


def test_cleanup_accepts_string_path(comp_folder: Path):
    spec = _spec("aaaaaaaa")
    _make_parts(comp_folder, "part_deadbee0.mp4")
    report = cleanup_compilation(str(comp_folder), spec)
    assert report.deleted_files == ["part_deadbee0.mp4"]


# ----- SAFETY: workspace containment -----


def test_cleanup_refuses_folder_outside_workspace(tmp_path: Path, monkeypatch):
    """The big guardrail: passing a path outside `<workspace>/compilations/`
    raises CleanupSafetyError. A bug or attacker can't make cleanup
    touch `C:\\Windows` or the user's home dir."""
    from ai_video_editor.config import settings

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "workspace_dir", workspace)

    rogue = tmp_path / "definitely-not-the-workspace"
    rogue.mkdir()
    (rogue / "_parts").mkdir()
    (rogue / "_parts" / "part_aaaaaaaa.mp4").write_bytes(b"\x00")

    with pytest.raises(CleanupSafetyError, match="containment"):
        cleanup_compilation(rogue, _spec("bbbbbbbb"))


def test_cleanup_refuses_compilations_root_itself(tmp_path: Path, monkeypatch):
    """Cleaning the root would sweep across every reel — refused
    explicitly. Only single-reel folders are valid targets."""
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    (tmp_path / "compilations").mkdir()

    with pytest.raises(CleanupSafetyError):
        cleanup_compilation(tmp_path / "compilations", _spec())


def test_cleanup_refuses_subdir_of_a_compilation(tmp_path: Path, monkeypatch):
    """Passing `<workspace>/compilations/<id>/_parts/` directly would
    work in practice but bypasses the folder-level safety. Refuse
    anything deeper than one level under compilations/."""
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    folder = tmp_path / "compilations" / "reel-1"
    folder.mkdir(parents=True)
    parts = folder / "_parts"
    parts.mkdir()

    with pytest.raises(CleanupSafetyError):
        cleanup_compilation(parts, _spec())


def test_cleanup_enforce_safety_false_bypasses_check(tmp_path: Path):
    """Escape hatch for tests / scripts that want to operate on a
    tmp dir. Production code paths leave the default (True)."""
    folder = tmp_path / "random-folder"
    folder.mkdir()
    spec = _spec("aaaaaaaa")
    _make_parts(folder, "part_aaaaaaaa.mp4", "part_deadbee0.mp4")
    report = cleanup_compilation(folder, spec, enforce_safety=False)
    assert report.deleted_files == ["part_deadbee0.mp4"]


def test_is_safe_compilation_folder_predicate(tmp_path: Path, monkeypatch):
    """The predicate itself, exercised independently of cleanup_compilation
    so other modules can reuse it."""
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    comps = tmp_path / "compilations"
    comps.mkdir()
    (comps / "reel-1").mkdir()
    (comps / "reel-1" / "_parts").mkdir()

    assert is_safe_compilation_folder(comps / "reel-1") is True
    assert is_safe_compilation_folder(comps) is False  # root rejected
    assert is_safe_compilation_folder(comps / "reel-1" / "_parts") is False  # too deep
    assert is_safe_compilation_folder(tmp_path) is False  # outside
    assert is_safe_compilation_folder(Path("C:/Windows")) is False  # absolute outside


# ----- SAFETY: audit log -----


def test_cleanup_writes_audit_log(comp_folder: Path):
    """Every non-dry-run deletion is recorded in `_cleanup.log` so
    the user has a receipt of what was deleted and when."""
    spec = _spec("aaaaaaaa")
    _make_parts(comp_folder, "part_aaaaaaaa.mp4", "part_deadbee0.mp4")
    cleanup_compilation(comp_folder, spec)
    log = (comp_folder / "_cleanup.log").read_text(encoding="utf-8").strip().splitlines()
    assert len(log) == 1
    record = json.loads(log[0])
    assert record["deleted"] == ["part_deadbee0.mp4"]
    assert "ts" in record


def test_cleanup_dry_run_does_not_write_audit_log(comp_folder: Path):
    spec = _spec("aaaaaaaa")
    _make_parts(comp_folder, "part_deadbee0.mp4")
    cleanup_compilation(comp_folder, spec, dry_run=True)
    assert not (comp_folder / "_cleanup.log").exists()


def test_cleanup_no_deletions_no_audit_log_entry(comp_folder: Path):
    """A clean folder shouldn't litter the log with empty records."""
    spec = _spec("aaaaaaaa")
    _make_parts(comp_folder, "part_aaaaaaaa.mp4")
    cleanup_compilation(comp_folder, spec)
    assert not (comp_folder / "_cleanup.log").exists()


def test_cleanup_audit_log_appends_across_calls(comp_folder: Path):
    spec = _spec("aaaaaaaa")
    _make_parts(comp_folder, "part_deadbee1.mp4")
    cleanup_compilation(comp_folder, spec)
    _make_parts(comp_folder, "part_deadbee2.mp4")
    cleanup_compilation(comp_folder, spec)
    log_lines = (comp_folder / "_cleanup.log").read_text(encoding="utf-8").strip().splitlines()
    assert len(log_lines) == 2
    assert json.loads(log_lines[0])["deleted"] == ["part_deadbee1.mp4"]
    assert json.loads(log_lines[1])["deleted"] == ["part_deadbee2.mp4"]


# ----- safe_cleanup_for_render (render-time wrapper) -----


def test_safe_cleanup_returns_serialisable_dict(comp_folder: Path):
    spec = _spec("aaaaaaaa")
    _make_parts(comp_folder, "part_deadbee0.mp4")
    result = safe_cleanup_for_render(comp_folder, spec)
    assert result["ok"] is True
    assert result["deleted"] == 1
    json.dumps(result)


def test_safe_cleanup_catches_safety_error(tmp_path: Path, monkeypatch):
    """A misconfigured folder (outside workspace) must not crash the
    render. The wrapper catches CleanupSafetyError and surfaces it
    in the structured response."""
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()
    result = safe_cleanup_for_render(tmp_path / "elsewhere", {"clips": []})
    assert result["ok"] is False
    assert result["reason"] == "safety"
    assert "containment" in result["error"]


def test_safe_cleanup_swallows_other_errors(comp_folder: Path, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("simulated cleanup failure")

    monkeypatch.setattr(cleanup_mod, "cleanup_compilation", boom)
    result = safe_cleanup_for_render(comp_folder, {"clips": []})
    assert result["ok"] is False
    assert "simulated" in result["error"]


# ----- render_spec integration: auto-cleanup wiring -----


def test_render_spec_auto_runs_cleanup_after_concat(comp_folder: Path, monkeypatch):
    """The whole point: every iterative edit reuses render_spec, and
    render_spec must prune orphans on the way out."""
    from ai_video_editor import compile as compile_mod

    spec = _spec("aaaaaaaa")
    _make_parts(comp_folder, "part_aaaaaaaa.mp4", "part_deadbee0.mp4")

    monkeypatch.setattr(
        compile_mod,
        "_render_clip_part",
        lambda clip, parts_dir, aspect, fade, label_number=None: (
            parts_dir / f"part_{clip['id'][:8]}.mp4",
            True,
            None,
        ),
    )
    monkeypatch.setattr(compile_mod, "_concat", lambda parts, out: (True, None))

    summary = compile_mod.render_spec(spec, comp_folder)
    assert summary["cleanup"] is not None
    assert summary["cleanup"]["ok"] is True
    assert "part_deadbee0.mp4" in summary["cleanup"]["deleted_files"]
    assert not (comp_folder / "_parts" / "part_deadbee0.mp4").exists()
    assert (comp_folder / "_parts" / "part_aaaaaaaa.mp4").exists()


def test_render_spec_skips_cleanup_on_concat_failure(comp_folder: Path, monkeypatch):
    """A failed render may be retried with the existing cache; we
    must not nuke parts that the retry would reuse."""
    from ai_video_editor import compile as compile_mod

    spec = _spec("aaaaaaaa")
    _make_parts(comp_folder, "part_deadbee0.mp4")

    monkeypatch.setattr(
        compile_mod,
        "_render_clip_part",
        lambda clip, parts_dir, aspect, fade, label_number=None: (
            parts_dir / f"part_{clip['id'][:8]}.mp4",
            True,
            None,
        ),
    )
    monkeypatch.setattr(compile_mod, "_concat", lambda parts, out: (False, "boom"))

    summary = compile_mod.render_spec(spec, comp_folder)
    assert summary["compiled"] is False
    assert summary["cleanup"] is None
    assert (comp_folder / "_parts" / "part_deadbee0.mp4").exists()


def test_render_spec_cleanup_opt_out(comp_folder: Path, monkeypatch):
    from ai_video_editor import compile as compile_mod

    spec = _spec("aaaaaaaa")
    _make_parts(comp_folder, "part_deadbee0.mp4")

    monkeypatch.setattr(
        compile_mod,
        "_render_clip_part",
        lambda clip, parts_dir, aspect, fade, label_number=None: (
            parts_dir / f"part_{clip['id'][:8]}.mp4",
            True,
            None,
        ),
    )
    monkeypatch.setattr(compile_mod, "_concat", lambda parts, out: (True, None))

    summary = compile_mod.render_spec(spec, comp_folder, cleanup=False)
    assert summary["cleanup"] is None
    assert (comp_folder / "_parts" / "part_deadbee0.mp4").exists()


# ----- realistic edit sequence -----


def test_remove_then_render_cleans_dead_clip(comp_folder: Path, monkeypatch):
    """End-to-end shape: a clip gets removed from the spec, render is
    triggered, the removed clip's cached part is reaped automatically."""
    from ai_video_editor import compile as compile_mod
    from ai_video_editor.compile import remove_clip

    spec = _spec("aaaaaaaa", "bbbbbbbb")
    _make_parts(
        comp_folder,
        "part_aaaaaaaa.mp4",
        "part_aaaaaaaa_n1.mp4",
        "part_bbbbbbbb.mp4",
        "part_bbbbbbbb_n2.mp4",
    )

    monkeypatch.setattr(
        compile_mod,
        "_render_clip_part",
        lambda clip, parts_dir, aspect, fade, label_number=None: (
            parts_dir / f"part_{clip['id'][:8]}.mp4",
            True,
            None,
        ),
    )
    monkeypatch.setattr(compile_mod, "_concat", lambda parts, out: (True, None))

    new_spec, dirty = remove_clip(spec, 1)
    compile_mod.render_spec(new_spec, comp_folder, dirty_clip_ids=dirty)

    surviving = {p.name for p in (comp_folder / "_parts").iterdir()}
    assert "part_bbbbbbbb.mp4" not in surviving
    assert "part_bbbbbbbb_n2.mp4" not in surviving
    assert "part_aaaaaaaa.mp4" in surviving
    assert "part_aaaaaaaa_n1.mp4" in surviving
