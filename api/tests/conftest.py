"""Shared test fixtures.

Each integration test gets a fresh temp SQLite DB and workspace dir.
Heavy outside-world calls (ffmpeg, Whisper, Anthropic, sqlite-vec
embeddings) are monkeypatched in the test that needs them — these
fixtures only set up the sandbox and pre-populate an asset.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

GROKBOT_SECRET_FIELDS = (
    "grokbot_webhook_url",
    "grokbot_webhook_key",
    "grokbot_drive_client_id",
    "grokbot_drive_client_secret",
    "grokbot_drive_refresh_token",
    "grokbot_drive_folder_id",
)


def isolate_grokbot(monkeypatch) -> None:
    """Blank every outbound Grok Bot credential and drop the cached webhook sink."""
    from ai_video_editor.config import settings
    from ai_video_editor.notify import sinks

    for field in GROKBOT_SECRET_FIELDS:
        monkeypatch.setattr(settings, field, "")
    # Cadence knobs are tuned in the live .env (daily heartbeat); tests pin the defaults.
    monkeypatch.setattr(settings, "grokbot_heartbeat_seconds", 300)
    monkeypatch.setattr(settings, "grokbot_runner_poll_seconds", 60)
    sinks.reset_sinks()


@pytest.fixture(autouse=True)
def _no_live_notify(monkeypatch):
    """Tests never reach the Lead's webhook or Drive, whatever `api/.env` holds."""
    from ai_video_editor.notify import sinks

    isolate_grokbot(monkeypatch)
    yield
    sinks.reset_sinks()


@pytest.fixture(autouse=True)
def _no_live_workspace_writes(tmp_path_factory, monkeypatch):
    """The Jev audit log and the corrections file never touch the real
    workspace, whatever `WORKSPACE_DIR` holds."""
    from ai_video_editor.config import settings

    root = tmp_path_factory.mktemp("isolated")
    monkeypatch.setattr(settings, "decisions_log_dir", root / "decisions")
    monkeypatch.setattr(settings, "game_hints_dir", root / "game_hints")


@pytest.fixture(autouse=True)
def _pinned_settings(monkeypatch):
    """Content-direction knobs are tuned in the live `.env` (and may be set in
    the shell); tests pin the code defaults so results never depend on them."""
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "shorts_beat_sync_enabled", False)
    monkeypatch.setattr(settings, "mic_stream_override", None)
    monkeypatch.setattr(settings, "hp_reader_shadow", False)
    monkeypatch.setattr(settings, "hp_reader_enabled", False)
    monkeypatch.setattr(settings, "story_mic_loudnorm", True)
    monkeypatch.setattr(settings, "story_budget_usd", 5.0)
    monkeypatch.setattr(settings, "game_understanding_budget_usd", 1.5)
    monkeypatch.setattr(settings, "plays_drive_cuts", False)
    monkeypatch.setattr(settings, "play_confirm_fps", 0.5)
    monkeypatch.setattr(settings, "play_confirm_model", "claude-sonnet-4-6")
    monkeypatch.setattr(settings, "play_sweep_fps", 0.25)
    monkeypatch.setattr(settings, "play_sweep_enabled", False)
    monkeypatch.setattr(settings, "story_transition_s", 0.25)
    monkeypatch.setattr(settings, "story_kill_sfx", True)
    monkeypatch.setattr(settings, "montage_jump_cut_gap_s", 3.0)


# ----- shared Grok Bot fakes -----


def env(kind, **kw):
    """A queue-item envelope of `kind` with a fresh id, plus `kw`."""
    d = {
        "id": str(uuid.uuid4()),
        "kind": kind,
        "author_bot": "lead",
        "created": datetime.now(timezone.utc).isoformat(),
        "schema_version": 1,
    }
    d.update(kw)
    return d


class FakeRepo:
    """In-memory queue repo that mirrors writes to disk under `root`."""

    push_ok = True
    retries = 0

    def __init__(self, root: Path):
        self.root = root
        self.files: dict[str, str] = {}
        self.pushed: list[str] = []
        self.pulls = 0

    def pull(self):
        self.pulls += 1

    def list_items(self):
        return sorted((p, t) for p, t in self.files.items() if p.startswith("queue/"))

    def write(self, relative_path, text):
        self.files[relative_path] = text
        target = self.root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def remove(self, relative_path):
        self.files.pop(relative_path, None)
        (self.root / relative_path).unlink(missing_ok=True)

    def commit_push(self, message):
        self.pushed.append(message)
        return self.push_ok

    def retry_push(self):
        self.retries += 1
        return self.push_ok

    def add(self, item_dict):
        self.files[f"queue/{item_dict['id']}.json"] = json.dumps(item_dict)


class FakeUploader:
    """Records (name, subfolder) per upload and each prune call."""

    def __init__(self, ids=None):
        self.uploads: list[tuple[str, str]] = []
        self.prunes: list[tuple[str, int]] = []
        self._ids = ids

    def upload(self, path, *, name, subfolder):
        self.uploads.append((name, subfolder))
        if self._ids is not None:
            return self._ids(name, subfolder)
        return "fid1"

    def prune(self, *, subfolder, older_than_days):
        self.prunes.append((subfolder, older_than_days))
        return 0


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Temp DB + workspace, isolated per test."""
    from ai_video_editor.config import settings

    db = tmp_path / "test.db"
    ws = tmp_path / "ws"
    monkeypatch.setattr(settings, "database_path", str(db))
    monkeypatch.setattr(settings, "workspace_dir", ws)
    # Disable Langfuse during tests — no network, no surprises.
    monkeypatch.setattr(settings, "langfuse_public_key", "")
    monkeypatch.setattr(settings, "langfuse_secret_key", "")
    return tmp_path


@pytest.fixture
def client(sandbox):
    """TestClient with the lifespan run against the temp sandbox."""
    from ai_video_editor.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def asset(sandbox):
    """A fake asset row in the temp DB. The path doesn't have to exist
    on disk for the endpoints under test; we mock anything that probes
    it. Returns the asset dict."""
    import asyncio

    from ai_video_editor.database import get_db

    a = {
        "id": str(uuid.uuid4()),
        "filename": "League of Legends_05-15-2026_21-19-8-0.mp4",
        "path": str(sandbox / "fake.mp4"),
        "game": "League of Legends",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }

    async def _insert():
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO assets (id, filename, path, game, created_at, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (a["id"], a["filename"], a["path"], a["game"], a["created_at"], a["indexed_at"]),
            )
            await db.commit()
        finally:
            await db.close()

    asyncio.run(_insert())
    return a


@pytest.fixture
def poll():
    """Returns a helper that polls a job until terminal status."""
    import time

    def _poll(client: TestClient, job_id: str, attempts: int = 60) -> dict:
        last = None
        for _ in range(attempts):
            last = client.get(f"/api/v1/jobs/{job_id}").json()
            if last["status"] in ("completed", "failed"):
                return last
            time.sleep(0.05)
        raise AssertionError(f"job {job_id} did not finish: {last}")

    return _poll


@pytest.fixture
def compilation(client, asset, monkeypatch):
    """A finished two-clip compilation (DB row, folder, spec.json, journal)."""
    import asyncio
    import json
    import uuid
    from datetime import datetime, timezone

    from ai_video_editor.compile_journal import append_journal
    from ai_video_editor.config import settings
    from ai_video_editor.database import get_db

    folder = settings.workspace_dir / "compilations" / "plan-folder"
    folder.mkdir(parents=True)
    spec = {
        "id": "spec-x",
        "asset_id": asset["id"],
        "clips": [
            {
                "id": cid,
                "asset_id": asset["id"],
                "asset_path": asset["path"],
                "start_seconds": start,
                "end_seconds": start + 10.0,
                "effects": [],
            }
            for cid, start in (("c-aaa", 100.0), ("c-bbb", 200.0))
        ],
    }
    (folder / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    (folder / "compilation.mp4").write_bytes(b"")
    append_journal(folder, spec, action="compile")
    comp_id = str(uuid.uuid4())

    async def seed():
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO compilations (id, asset_id, output_path, params, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    comp_id,
                    asset["id"],
                    (folder / "compilation.mp4").as_posix(),
                    "{}",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            await db.commit()
        finally:
            await db.close()

    asyncio.run(seed())
    monkeypatch.setattr(
        "ai_video_editor.routers.edits.render_spec",
        lambda spec, fld, dirty_clip_ids=None: {"output": (folder / "compilation.mp4").as_posix()},
    )
    return comp_id, folder
