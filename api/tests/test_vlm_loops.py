"""Loop-mechanics tests with mocked backend + patched trim_clip.

No live Ollama, no ffmpeg. Verify:
- `pass` verdict exits immediately with clip written
- `false_positive` verdict skips the clip + deletes the file
- `fixable` verdicts iterate up to the cap and shift the window
- cap reached still keeps the last cut (not a drop)
- window fixes apply in the correct direction
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from ai_video_editor.vlm import loops
from ai_video_editor.vlm.loops import _apply_window_fix, validate_and_cut
from ai_video_editor.vlm.prompts import ClipVerdict


class _StubBackend:
    """Backend that yields a canned sequence of verdicts."""

    name = "stub"

    def __init__(self, verdicts: list[ClipVerdict]) -> None:
        self._verdicts = list(verdicts)
        self.calls = 0

    def health(self) -> dict:
        return {"ok": True, "model": "stub"}

    def call(self, **_kw) -> str:
        self.calls += 1
        return "unused — validate_clip is mocked"


def _patch_validate_clip(verdicts: list[ClipVerdict]):
    """Return a patch context manager that yields the given verdicts in
    order from `validate_clip`."""
    seq = iter(verdicts)

    def _fake(*_a, **_kw) -> ClipVerdict:
        return next(seq)

    return patch("ai_video_editor.vlm.loops.validate_clip", side_effect=_fake)


def _patch_trim_ok(created_files: list[Path]):
    """Patch trim_clip to touch an empty file at `out_path` and return
    (True, None). `created_files` gets appended so tests can inspect."""

    def _fake(source, out_path, start, end):
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")
        created_files.append(p)
        return True, None

    return patch("ai_video_editor.vlm.loops.trim_clip", side_effect=_fake)


def _patch_get_duration(seconds: float = 60.0):
    return patch(
        "ai_video_editor.vlm.loops.get_duration_seconds", return_value=seconds
    )


# ---------------------------------------------------------------------
# _apply_window_fix
# ---------------------------------------------------------------------


def test_apply_window_fix_extend_before() -> None:
    start, end = _apply_window_fix(
        start=10.0,
        end=20.0,
        duration=60.0,
        verdict=ClipVerdict(
            verdict="fixable", why="x", fix="extend_before", fix_seconds=2.0
        ),
    )
    assert start == 8.0
    assert end == 20.0


def test_apply_window_fix_extend_after_bounded_by_duration() -> None:
    start, end = _apply_window_fix(
        start=50.0,
        end=59.0,
        duration=60.0,
        verdict=ClipVerdict(
            verdict="fixable", why="x", fix="extend_after", fix_seconds=5.0
        ),
    )
    assert end == 60.0  # not 64
    assert start == 50.0


def test_apply_window_fix_extend_before_bounded_by_zero() -> None:
    start, _ = _apply_window_fix(
        start=1.0,
        end=10.0,
        duration=60.0,
        verdict=ClipVerdict(
            verdict="fixable", why="x", fix="extend_before", fix_seconds=5.0
        ),
    )
    assert start == 0.0


def test_apply_window_fix_trim_end_stays_positive_length() -> None:
    _, end = _apply_window_fix(
        start=10.0,
        end=12.0,
        duration=60.0,
        verdict=ClipVerdict(
            verdict="fixable", why="x", fix="trim_end", fix_seconds=5.0
        ),
    )
    # end can't be pulled below start + 0.5
    assert end == 10.5


def test_apply_window_fix_no_fix_is_noop() -> None:
    v = ClipVerdict(verdict="pass", why="ok")
    assert _apply_window_fix(10.0, 20.0, 60.0, v) == (10.0, 20.0)


# ---------------------------------------------------------------------
# validate_and_cut
# ---------------------------------------------------------------------


def test_validate_and_cut_pass_on_first_iter(tmp_path: Path) -> None:
    verdicts = [ClipVerdict(verdict="pass", why="clean")]
    out = tmp_path / "clip.mp4"
    created: list[Path] = []
    backend = _StubBackend([])

    with _patch_get_duration(60), _patch_trim_ok(created), _patch_validate_clip(verdicts):
        result = validate_and_cut(
            source_path="fake.mp4",
            out_path=out,
            start=10,
            end=20,
            game="league",
            event_type="kill",
            source="riot_api",
            anchor_seconds=15.0,
            backend=backend,
            max_iter=5,
            n_frames=4,
        )

    assert result.ok is True
    assert result.iterations == 1
    assert result.out_path == out
    assert result.final_verdict.verdict == "pass"
    assert out.is_file()


def test_validate_and_cut_false_positive_drops_clip(tmp_path: Path) -> None:
    verdicts = [ClipVerdict(verdict="false_positive", why="no fight")]
    out = tmp_path / "clip.mp4"
    created: list[Path] = []
    backend = _StubBackend([])

    with _patch_get_duration(60), _patch_trim_ok(created), _patch_validate_clip(verdicts):
        result = validate_and_cut(
            source_path="fake.mp4",
            out_path=out,
            start=10,
            end=20,
            game="league",
            event_type="kill",
            source="audio_peak",
            anchor_seconds=None,
            backend=backend,
            max_iter=5,
            n_frames=4,
        )

    assert result.ok is False
    assert result.out_path is None
    assert result.final_verdict.verdict == "false_positive"
    assert not out.is_file()  # unlinked


def test_validate_and_cut_fixable_shifts_window_then_passes(tmp_path: Path) -> None:
    verdicts = [
        ClipVerdict(
            verdict="fixable", why="no setup", fix="extend_before", fix_seconds=2.0
        ),
        ClipVerdict(verdict="pass", why="better now"),
    ]
    out = tmp_path / "clip.mp4"
    created: list[Path] = []
    backend = _StubBackend([])

    with _patch_get_duration(60), _patch_trim_ok(created), _patch_validate_clip(verdicts):
        result = validate_and_cut(
            source_path="fake.mp4",
            out_path=out,
            start=10,
            end=20,
            game="league",
            event_type="kill",
            source="riot_api",
            anchor_seconds=15.0,
            backend=backend,
            max_iter=5,
            n_frames=4,
        )

    assert result.ok is True
    assert result.iterations == 2
    # Window shifted by 2s backwards on iter 1 → final start = 8.0
    assert result.start_seconds == 8.0
    assert result.end_seconds == 20.0
    assert result.final_verdict.verdict == "pass"


def test_validate_and_cut_cap_reached_keeps_clip(tmp_path: Path) -> None:
    # All fixable, so we hit the cap
    verdicts = [
        ClipVerdict(
            verdict="fixable", why="still bad", fix="extend_after", fix_seconds=1.0
        )
    ] * 5
    out = tmp_path / "clip.mp4"
    created: list[Path] = []
    backend = _StubBackend([])

    with _patch_get_duration(60), _patch_trim_ok(created), _patch_validate_clip(verdicts):
        result = validate_and_cut(
            source_path="fake.mp4",
            out_path=out,
            start=10,
            end=20,
            game="league",
            event_type="kill",
            source="riot_api",
            anchor_seconds=15.0,
            backend=backend,
            max_iter=3,  # deliberately small
            n_frames=4,
        )

    assert result.ok is True
    assert result.iterations == 3
    assert out.is_file()
    assert result.final_verdict.verdict == "fixable"


def test_validate_and_cut_records_full_history(tmp_path: Path) -> None:
    verdicts = [
        ClipVerdict(
            verdict="fixable", why="tighten start", fix="trim_start", fix_seconds=1.0
        ),
        ClipVerdict(
            verdict="fixable", why="more setup", fix="extend_before", fix_seconds=2.0
        ),
        ClipVerdict(verdict="pass", why="ok"),
    ]
    out = tmp_path / "clip.mp4"
    created: list[Path] = []
    backend = _StubBackend([])

    with _patch_get_duration(60), _patch_trim_ok(created), _patch_validate_clip(verdicts):
        result = validate_and_cut(
            source_path="fake.mp4",
            out_path=out,
            start=10,
            end=20,
            game="league",
            event_type="kill",
            source="riot_api",
            anchor_seconds=15.0,
            backend=backend,
            max_iter=5,
            n_frames=4,
        )

    assert result.iterations == 3
    assert [v.verdict for v in result.verdict_history] == [
        "fixable",
        "fixable",
        "pass",
    ]


def test_validate_and_cut_trim_failure_returns_skip(tmp_path: Path) -> None:
    out = tmp_path / "clip.mp4"

    def _fake_trim(_src, _out, _s, _e):
        return False, "ffmpeg died"

    backend = _StubBackend([])
    with _patch_get_duration(60), patch(
        "ai_video_editor.vlm.loops.trim_clip", side_effect=_fake_trim
    ):
        result = validate_and_cut(
            source_path="fake.mp4",
            out_path=out,
            start=10,
            end=20,
            game=None,
            event_type=None,
            source=None,
            anchor_seconds=None,
            backend=backend,
            max_iter=5,
            n_frames=4,
        )

    assert result.ok is False
    assert result.out_path is None
    assert "trim_failed" in result.final_verdict.why


# ---------------------------------------------------------------------
# Whole-comp loop (review-only mode)
# ---------------------------------------------------------------------


def test_review_and_apply_compilation_cohesive_exits_first_pass(
    tmp_path: Path,
) -> None:
    comp_file = tmp_path / "comp.mp4"
    comp_file.write_bytes(b"stub")

    from ai_video_editor.vlm.prompts import CompilationReview

    def _fake_review(*_a, **_kw):
        return CompilationReview(is_cohesive=True, fixes=[])

    with patch(
        "ai_video_editor.vlm.loops.validate_compilation", side_effect=_fake_review
    ), _patch_get_duration(120):
        result = loops.review_and_apply_compilation(
            compilation_id="comp-1",
            compilation_path=comp_file,
            game="league",
            clip_count=5,
            backend=_StubBackend([]),
            max_passes=3,
            n_frames=10,
        )

    assert result.passes == 1
    assert result.applied_fixes == []
    assert result.final_review.is_cohesive is True


def test_review_and_apply_compilation_review_only_returns_fixes_unapplied(
    tmp_path: Path,
) -> None:
    comp_file = tmp_path / "comp.mp4"
    comp_file.write_bytes(b"stub")

    from ai_video_editor.vlm.prompts import CompilationFix, CompilationReview

    fix = CompilationFix(clip_ref="02", issue="too long", fix="trim_end", fix_seconds=1.5)

    def _fake_review(*_a, **_kw):
        return CompilationReview(is_cohesive=False, fixes=[fix])

    with patch(
        "ai_video_editor.vlm.loops.validate_compilation", side_effect=_fake_review
    ), _patch_get_duration(120):
        result = loops.review_and_apply_compilation(
            compilation_id="comp-1",
            compilation_path=comp_file,
            game="league",
            clip_count=5,
            backend=_StubBackend([]),
            max_passes=3,
            n_frames=10,
            apply_fixes_fn=None,  # review-only
        )

    assert result.passes == 1
    assert result.applied_fixes == []
    assert result.unapplied_fixes == [fix]


def test_review_and_apply_compilation_calls_apply_fixes_fn(
    tmp_path: Path,
) -> None:
    comp_file = tmp_path / "comp.mp4"
    comp_file.write_bytes(b"stub")

    from ai_video_editor.vlm.prompts import CompilationFix, CompilationReview

    fix1 = CompilationFix(clip_ref="03", issue="dupe", fix="remove_clip")
    review_sequence = iter(
        [
            CompilationReview(is_cohesive=False, fixes=[fix1]),
            CompilationReview(is_cohesive=True, fixes=[]),
        ]
    )

    def _fake_review(*_a, **_kw):
        return next(review_sequence)

    apply_calls: list = []

    def _fake_apply(comp_id: str, fixes: list[CompilationFix]) -> dict:
        apply_calls.append((comp_id, fixes))
        return {"applied": fixes, "unapplied": [], "clip_count": 4}

    with patch(
        "ai_video_editor.vlm.loops.validate_compilation", side_effect=_fake_review
    ), _patch_get_duration(120):
        result = loops.review_and_apply_compilation(
            compilation_id="comp-1",
            compilation_path=comp_file,
            game="league",
            clip_count=5,
            backend=_StubBackend([]),
            max_passes=3,
            n_frames=10,
            apply_fixes_fn=_fake_apply,
        )

    assert result.passes == 2
    assert len(result.applied_fixes) == 1
    assert result.applied_fixes[0].fix == "remove_clip"
    assert result.final_review.is_cohesive is True
    assert apply_calls == [("comp-1", [fix1])]
