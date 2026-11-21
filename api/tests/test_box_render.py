"""Box render job tests: unit, integration, and end-to-end.

Tests cover:
- Pure planning for both aspect ratios (unit)
- Integration with fake storage and testsrc clip (integration)
- End-to-end: outputs uploaded, event emitted, scratch cleanup (e2e)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_video_editor.config import settings
from ai_video_editor.grokbot.box_render import (
    RenderInput,
    RenderLock,
    _16x9_filter,
    _cpu_codec_opts,
    _probe_duration,
    _render_single_aspect,
    run_box_render,
)

# --- Unit tests: pure functions ---


class TestCpuCodecOpts:
    def test_returns_veryfast_preset(self):
        opts = _cpu_codec_opts()
        assert "-preset" in opts
        assert "veryfast" in opts

    def test_returns_crf_23(self):
        opts = _cpu_codec_opts()
        assert "-crf" in opts
        assert "23" in opts


class TestAspectFilter:
    def test_16x9_caps_resolution(self):
        vf = _16x9_filter()
        assert "1920" in vf
        assert "1080" in vf
        assert "scale" in vf

    def test_16x9_pads_to_exact_size(self):
        vf = _16x9_filter()
        assert "pad" in vf


class TestRenderLock:
    def test_acquire_and_release(self, tmp_path):
        lock_file = tmp_path / "test.lock"
        lock = RenderLock(str(lock_file))
        assert lock.acquire()
        lock.release()
        assert not lock_file.exists() or lock_file.read_text() != ""

    def test_lock_prevents_concurrent_acquisition(self, tmp_path):
        lock_file = tmp_path / "test.lock"
        lock1 = RenderLock(str(lock_file))
        lock2 = RenderLock(str(lock_file))

        assert lock1.acquire()
        assert not lock2.acquire()
        lock1.release()
        assert lock2.acquire()
        lock2.release()

    def test_context_manager(self, tmp_path):
        lock_file = tmp_path / "test.lock"
        with RenderLock(str(lock_file)) as lock:
            assert lock is not None
        lock2 = RenderLock(str(lock_file))
        assert lock2.acquire()
        lock2.release()


class TestRenderInput:
    def test_dataclass_fields(self):
        inp = RenderInput(
            edit_plan_id="plan-001",
            edit_plan_version="1",
            asset_id="asset-001",
            clips=[{"drive_id": "d1", "clip_ref": "1"}],
            trims=[{"clip_id": "1", "start_s": 0, "end_s": 10}],
            concat_order=["1"],
            game="league",
        )
        assert inp.edit_plan_id == "plan-001"
        assert inp.game == "league"
        assert inp.music_path is None
        assert inp.music_volume == 0.25


# --- Integration tests with testsrc ---


def _ffmpeg_available() -> bool:
    """Check if ffmpeg is available."""
    try:
        result = subprocess.run(
            [settings.ffmpeg_path, "-version"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.fixture
def test_clip(tmp_path):
    """Generate a 2-second test clip using ffmpeg lavfi testsrc."""
    if not _ffmpeg_available():
        pytest.skip("ffmpeg not available")

    clip_path = tmp_path / "test_clip.mp4"
    cmd = [
        settings.ffmpeg_path,
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=2:size=1920x1080:rate=30",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=2",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "28",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        str(clip_path),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=30)
    if result.returncode != 0:
        pytest.skip(f"failed to generate test clip: {result.stderr.decode()[:200]}")
    return clip_path


class TestRenderSingleAspect:
    def test_renders_16x9_output(self, tmp_path, test_clip):
        scratch = tmp_path / "scratch"
        scratch.mkdir()

        output, err = _render_single_aspect(
            scratch,
            [test_clip],
            [{"clip_id": "c1", "start_s": 0, "end_s": 1}],
            ["c1"],
            "16:9",
        )

        assert err is None
        assert output is not None
        assert output.exists()
        assert output.stat().st_size > 0

    def test_renders_9x16_output_with_hud_layout(self, tmp_path, test_clip):
        scratch = tmp_path / "scratch"
        scratch.mkdir()

        output, err = _render_single_aspect(
            scratch,
            [test_clip],
            [{"clip_id": "c1", "start_s": 0, "end_s": 1}],
            ["c1"],
            "9:16",
            game="league",
        )

        assert err is None
        assert output is not None
        assert output.exists()
        assert output.stat().st_size > 0

    def test_missing_clip_returns_error(self, tmp_path, test_clip):
        scratch = tmp_path / "scratch"
        scratch.mkdir()

        output, err = _render_single_aspect(
            scratch,
            [],
            [{"clip_id": "missing", "start_s": 0, "end_s": 1}],
            ["missing"],
            "16:9",
        )

        assert output is None
        assert "missing clip" in err


class TestProbeDuration:
    def test_probes_valid_file(self, test_clip):
        duration = _probe_duration(test_clip)
        assert 1.5 < duration < 2.5

    def test_returns_zero_for_missing_file(self, tmp_path):
        duration = _probe_duration(tmp_path / "nonexistent.mp4")
        assert duration == 0.0


# --- End-to-end tests with fake Drive ---


class FakeUploader:
    """Fake Drive uploader that records calls."""

    def __init__(self, download_data: dict[str, bytes] | None = None):
        self.uploads: list[tuple[str, str, str]] = []
        self.downloads: list[str] = []
        self._download_data = download_data or {}
        self._upload_counter = 0
        self._token = "fake-token"

    def _access_token(self) -> str:
        return self._token

    def upload(self, path: Path, *, name: str, subfolder: str) -> str:
        self.uploads.append((str(path), name, subfolder))
        self._upload_counter += 1
        return f"fake-drive-id-{self._upload_counter}"


class TestRunBoxRenderE2E:
    @pytest.fixture(autouse=True)
    def _enable_box_render(self, monkeypatch):
        monkeypatch.setattr(settings, "grokbot_box_render_enabled", True)

    def test_refuses_when_flag_off(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "grokbot_box_render_enabled", False)
        events: list = []
        uploader = FakeUploader()
        render_input = RenderInput(
            edit_plan_id="plan-disabled",
            edit_plan_version="1",
            asset_id="asset-disabled",
            clips=[{"drive_id": "d1", "clip_ref": "1"}],
            trims=[{"clip_id": "1", "start_s": 0, "end_s": 1}],
            concat_order=["1"],
        )
        result = run_box_render(
            uploader,
            render_input,
            emit_fn=lambda e: events.append(e),
            lock=RenderLock(str(tmp_path / "test.lock")),
        )
        assert not result.ok
        assert result.error == "box render disabled"
        failed = [e for e in events if e.event == "render.failed"]
        assert len(failed) == 1
        assert failed[0].payload["stage"] == "disabled"

    def test_emits_video_rendered_on_success(self, tmp_path, test_clip):
        events: list = []
        uploader = FakeUploader()

        scratch = tmp_path / "scratch"
        scratch.mkdir()
        local_clip = scratch / "clip_000.mp4"
        shutil.copy(test_clip, local_clip)

        lock = RenderLock(str(tmp_path / "test.lock"))

        with patch("ai_video_editor.grokbot.box_render.DriveStorage") as mock_drive_storage:
            mock_storage = MagicMock()
            mock_storage.scratch_dir = scratch
            mock_storage.download_clip.return_value = local_clip
            mock_storage.upload_file.side_effect = [
                "drive-9x16-id",
                "drive-16x9-id",
            ]
            mock_drive_storage.return_value = mock_storage

            render_input = RenderInput(
                edit_plan_id="plan-e2e",
                edit_plan_version="1",
                asset_id="asset-e2e",
                clips=[{"drive_id": "d1", "clip_ref": "1"}],
                trims=[{"clip_id": "1", "start_s": 0, "end_s": 1}],
                concat_order=["1"],
                game="league",
            )

            with patch("tempfile.mkdtemp", return_value=str(scratch)):
                result = run_box_render(
                    uploader,
                    render_input,
                    emit_fn=lambda e: events.append(e),
                    lock=lock,
                )

        assert result.ok
        assert len(result.outputs) == 2
        aspects = {o.aspect for o in result.outputs}
        assert aspects == {"9:16", "16:9"}

        video_rendered = [e for e in events if e.event == "video.rendered"]
        assert len(video_rendered) == 1
        payload = video_rendered[0].payload
        assert payload["edit_plan_id"] == "plan-e2e"
        assert len(payload["outputs"]) == 2

    def test_emits_render_failed_when_busy(self, tmp_path):
        events: list = []
        uploader = FakeUploader()

        lock = RenderLock(str(tmp_path / "test.lock"))
        lock.acquire()

        try:
            render_input = RenderInput(
                edit_plan_id="plan-busy",
                edit_plan_version="1",
                asset_id="asset-busy",
                clips=[{"drive_id": "d1", "clip_ref": "1"}],
                trims=[{"clip_id": "1", "start_s": 0, "end_s": 1}],
                concat_order=["1"],
            )

            result = run_box_render(
                uploader,
                render_input,
                emit_fn=lambda e: events.append(e),
                lock=RenderLock(str(tmp_path / "test.lock")),
            )

            assert not result.ok
            assert "locked" in result.error or "already running" in result.error

            failed = [e for e in events if e.event == "render.failed"]
            assert len(failed) == 1
            assert failed[0].payload["stage"] == "busy"
        finally:
            lock.release()

    def test_scratch_cleaned_only_on_success(self, tmp_path, test_clip):
        events: list = []
        uploader = FakeUploader()

        lock = RenderLock(str(tmp_path / "test.lock"))

        real_scratch = Path(tempfile.mkdtemp(prefix="box-render-"))
        local_clip = real_scratch / "clip_000.mp4"
        shutil.copy(test_clip, local_clip)

        with patch("ai_video_editor.grokbot.box_render.DriveStorage") as mock_drive_storage:
            mock_storage = MagicMock()
            mock_storage.scratch_dir = real_scratch
            mock_storage.download_clip.return_value = local_clip
            mock_storage.upload_file.side_effect = [
                "drive-9x16-id",
                "drive-16x9-id",
            ]
            mock_drive_storage.return_value = mock_storage

            render_input = RenderInput(
                edit_plan_id="plan-clean",
                edit_plan_version="1",
                asset_id="asset-clean",
                clips=[{"drive_id": "d1", "clip_ref": "1"}],
                trims=[{"clip_id": "1", "start_s": 0, "end_s": 1}],
                concat_order=["1"],
                game="league",
            )

            with patch("tempfile.mkdtemp", return_value=str(real_scratch)):
                result = run_box_render(
                    uploader,
                    render_input,
                    emit_fn=lambda e: events.append(e),
                    lock=lock,
                )

                assert result.ok
                assert result.scratch_cleaned

    def test_scratch_kept_on_failure(self, tmp_path):
        events: list = []
        uploader = FakeUploader()

        lock = RenderLock(str(tmp_path / "test.lock"))

        from ai_video_editor.storage import StorageError

        with patch("ai_video_editor.grokbot.box_render.DriveStorage") as mock_drive_storage:
            mock_storage = MagicMock()
            mock_storage.download_clip.side_effect = StorageError("download failed")
            mock_drive_storage.return_value = mock_storage

            render_input = RenderInput(
                edit_plan_id="plan-fail",
                edit_plan_version="1",
                asset_id="asset-fail",
                clips=[{"drive_id": "d1", "clip_ref": "1"}],
                trims=[{"clip_id": "1", "start_s": 0, "end_s": 1}],
                concat_order=["1"],
            )

            result = run_box_render(
                uploader,
                render_input,
                emit_fn=lambda e: events.append(e),
                lock=lock,
            )

            assert not result.ok
            assert not result.scratch_cleaned

            failed = [e for e in events if e.event == "render.failed"]
            assert len(failed) == 1
            assert failed[0].payload["stage"] == "download"

    def test_uses_injected_storage_for_download_and_upload(self, tmp_path, test_clip):
        """Test that run_box_render uses injected storage backend."""
        events: list = []
        uploader = FakeUploader()

        scratch = tmp_path / "scratch"
        scratch.mkdir()
        local_clip = scratch / "clip_000.mp4"
        shutil.copy(test_clip, local_clip)

        lock = RenderLock(str(tmp_path / "test.lock"))

        fake_storage = MagicMock()
        fake_storage.scratch_dir = scratch
        fake_storage.download_clip.return_value = local_clip
        fake_storage.upload_file.side_effect = [
            "injected-9x16-id",
            "injected-16x9-id",
        ]

        render_input = RenderInput(
            edit_plan_id="plan-injected",
            edit_plan_version="1",
            asset_id="asset-injected",
            clips=[{"drive_id": "d1", "clip_ref": "1"}],
            trims=[{"clip_id": "1", "start_s": 0, "end_s": 1}],
            concat_order=["1"],
            game="league",
        )

        result = run_box_render(
            uploader,
            render_input,
            emit_fn=lambda e: events.append(e),
            lock=lock,
            storage=fake_storage,
        )

        assert result.ok
        assert len(result.outputs) == 2

        fake_storage.download_clip.assert_called_once_with("d1", "clip_000.mp4")

        assert fake_storage.upload_file.call_count == 2
        upload_calls = fake_storage.upload_file.call_args_list
        upload_names = [call[0][1] for call in upload_calls]
        assert "plan-injected_9x16.mp4" in upload_names
        assert "plan-injected_16x9.mp4" in upload_names

        assert result.outputs[0].drive_id == "injected-9x16-id"
        assert result.outputs[1].drive_id == "injected-16x9-id"


# --- Queue item tests ---


class TestRenderRequestItem:
    def test_parse_valid_render_request(self):
        from ai_video_editor.grokbot.queue import parse_item

        item_json = json.dumps(
            {
                "id": "d4e5f6a7-b8c9-4d0e-1f2a-3b4c5d6e7f8a",
                "kind": "render_request",
                "author_bot": "editor",
                "created": "2026-11-21T02:30:00Z",
                "schema_version": 1,
                "asset_id": "asset-001",
                "edit_plan_id": "plan-001",
                "edit_plan_version": "1",
                "game": "league",
                "clips": [
                    {"clip_ref": "1", "drive_id": "d1", "start_s": 0.0, "end_s": 10.0},
                ],
                "concat_order": ["1"],
            }
        )

        item = parse_item(item_json)
        assert item.kind == "render_request"
        assert item.asset_id == "asset-001"
        assert len(item.clips) == 1

    def test_reject_empty_clips(self):
        from ai_video_editor.grokbot.queue import QueueRejected, parse_item

        item_json = json.dumps(
            {
                "id": "d4e5f6a7-b8c9-4d0e-1f2a-3b4c5d6e7f8a",
                "kind": "render_request",
                "author_bot": "editor",
                "created": "2026-11-21T02:30:00Z",
                "schema_version": 1,
                "asset_id": "asset-001",
                "edit_plan_id": "plan-001",
                "edit_plan_version": "1",
                "game": "league",
                "clips": [],
                "concat_order": [],
            }
        )

        with pytest.raises(QueueRejected) as exc:
            parse_item(item_json)
        assert "at least one clip" in str(exc.value)

    def test_reject_invalid_concat_order(self):
        from ai_video_editor.grokbot.queue import QueueRejected, parse_item

        item_json = json.dumps(
            {
                "id": "d4e5f6a7-b8c9-4d0e-1f2a-3b4c5d6e7f8a",
                "kind": "render_request",
                "author_bot": "editor",
                "created": "2026-11-21T02:30:00Z",
                "schema_version": 1,
                "asset_id": "asset-001",
                "edit_plan_id": "plan-001",
                "edit_plan_version": "1",
                "game": "league",
                "clips": [
                    {"clip_ref": "1", "drive_id": "d1", "start_s": 0.0, "end_s": 10.0},
                ],
                "concat_order": ["2"],
            }
        )

        with pytest.raises(QueueRejected) as exc:
            parse_item(item_json)
        assert "unknown clip_ref" in str(exc.value)


# --- Flag / endpoint tests ---


_RENDER_BODY = {
    "asset_id": "asset-001",
    "edit_plan_id": "plan-001",
    "edit_plan_version": "1",
    "game": "league",
    "clips": [
        {"clip_ref": "1", "drive_id": "d1", "start_s": 0.0, "end_s": 10.0},
    ],
    "concat_order": ["1"],
}


def test_box_render_flag_defaults_off(tmp_path):
    from ai_video_editor.config import Settings

    loaded = Settings(
        outplayed_media_dir=tmp_path,
        workspace_dir=tmp_path,
        _env_file=None,
    )
    assert loaded.grokbot_box_render_enabled is False


def test_submit_render_returns_503_when_flag_off(client, monkeypatch):
    monkeypatch.setattr(settings, "grokbot_box_render_enabled", False)
    r = client.post("/api/v1/grokbot/render", json=_RENDER_BODY)
    assert r.status_code == 503
    assert r.json()["detail"] == "box render disabled"


def test_submit_render_queues_when_flag_on(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "grokbot_box_render_enabled", True)
    queue_root = tmp_path / "queue-repo"
    (queue_root / "queue").mkdir(parents=True)
    monkeypatch.setattr(settings, "grokbot_queue_repo_dir", queue_root)

    r = client.post("/api/v1/grokbot/render", json=_RENDER_BODY)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued"
    assert "id" in body
    assert (queue_root / "queue" / f"{body['id']}.json").is_file()
