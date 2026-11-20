"""Drive uploader: token refresh/caching, multipart upload, subfolders,
prune and error mapping. MockTransport only, no network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ai_video_editor.config import settings
from ai_video_editor.grokbot import drive
from ai_video_editor.grokbot.drive import DriveError, DriveUploader

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
FOLDER_MIME = "application/vnd.google-apps.folder"


def _uploader(handler, now=lambda: NOW):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return DriveUploader("cid", "sec", "rtok", "ROOT", client=client, now=now)


def _is_token(request: httpx.Request) -> bool:
    return request.url.host == "oauth2.googleapis.com"


def _base(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if _is_token(request):
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        if "uploadType=multipart" in str(request.url):
            if FOLDER_MIME.encode() in request.content:
                return httpx.Response(200, json={"id": "SUB"})
            return httpx.Response(200, json={"id": "f1"})
        return httpx.Response(200, json={})

    return handler


def test_upload_refreshes_token_then_posts_multipart(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"video")
    calls: list[httpx.Request] = []
    up = _uploader(_base(calls))
    assert up.upload(f, name="a.mp4", subfolder="clips") == "f1"
    tok = calls[0]
    assert _is_token(tok)
    assert b"grant_type=refresh_token" in tok.content
    assert b"refresh_token=rtok" in tok.content
    final = calls[-1]
    assert final.headers["Authorization"] == "Bearer AT"
    assert "uploadType=multipart" in str(final.url)
    body = final.content.decode(errors="ignore")
    assert "a.mp4" in body
    assert "SUB" in body  # parent is the subfolder created under ROOT


def test_access_token_is_cached_until_expiry(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"v")
    calls: list[httpx.Request] = []
    clock = [NOW]
    up = _uploader(_base(calls), now=lambda: clock[0])
    up.upload(f, name="a", subfolder="clips")
    up.upload(f, name="b", subfolder="clips")
    assert len([c for c in calls if _is_token(c)]) == 1
    clock[0] = NOW + timedelta(hours=2)
    up.upload(f, name="c", subfolder="clips")
    assert len([c for c in calls if _is_token(c)]) == 2


def test_subfolder_created_once_and_reused(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"v")
    calls: list[httpx.Request] = []
    up = _uploader(_base(calls))
    up.upload(f, name="a", subfolder="renders")
    up.upload(f, name="b", subfolder="renders")
    creates = [c for c in calls if c.method == "POST" and FOLDER_MIME.encode() in c.content]
    lists = [c for c in calls if c.method == "GET"]
    assert len(creates) == 1
    assert len(lists) == 1
    assert "name='renders'" in lists[0].url.params["q"]
    assert "'ROOT' in parents" in lists[0].url.params["q"]


def test_prune_deletes_only_older_files():
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        if _is_token(request):
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
        if request.method == "GET":
            q = request.url.params["q"]
            if f"mimeType='{FOLDER_MIME}'" in q:
                return httpx.Response(200, json={"files": [{"id": "SUB"}]})
            return httpx.Response(200, json={"files": [{"id": "old1"}]})
        return httpx.Response(204)

    up = _uploader(handler)
    assert up.prune(subfolder="previews", older_than_days=7) == 1
    deletes = [c for c in calls if c.method == "DELETE"]
    assert len(deletes) == 1
    assert str(deletes[0].url).endswith("/drive/v3/files/old1")
    file_q = [c for c in calls if c.method == "GET"][-1].url.params["q"]
    assert "'SUB' in parents" in file_q
    cutoff = (NOW - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    assert f"createdTime < '{cutoff}" in file_q


def test_non_2xx_raises_drive_error(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"v")

    def handler(request):
        if _is_token(request):
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
        return httpx.Response(403, text="nope")

    with pytest.raises(DriveError):
        _uploader(handler).upload(f, name="a", subfolder="clips")


def test_token_failure_raises_drive_error(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"v")
    up = _uploader(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(DriveError):
        up.upload(f, name="a", subfolder="clips")


def test_uploader_from_settings_none_when_unconfigured(monkeypatch):
    for k in ("client_id", "client_secret", "refresh_token", "folder_id"):
        monkeypatch.setattr(settings, f"grokbot_drive_{k}", "x")
    assert isinstance(drive.uploader_from_settings(), DriveUploader)
    monkeypatch.setattr(settings, "grokbot_drive_refresh_token", "")
    assert drive.uploader_from_settings() is None


def test_drive_auth_help_needs_no_network(capsys):
    from ai_video_editor.grokbot import drive_auth

    with pytest.raises(SystemExit) as e:
        drive_auth.main(["--help"])
    assert e.value.code == 0
    assert "GROKBOT_DRIVE_REFRESH_TOKEN" in capsys.readouterr().out
