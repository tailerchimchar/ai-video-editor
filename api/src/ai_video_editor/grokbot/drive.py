"""Google Drive uploader for the Grok Bot team (httpx, no Google SDK).

The PC pushes clips, previews and renders into one dedicated Drive folder
that the cloud bots read. Auth is an OAuth refresh token (obtained once via
`drive-auth`) exchanged for short-lived access tokens, cached until expiry.
Inert (``uploader_from_settings() -> None``) until fully configured.
"""

from __future__ import annotations

import json
import mimetypes
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import httpx

from ..config import settings
from .paths import inside

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
# Refresh a little early so a token never expires mid-request.
_EXPIRY_MARGIN = timedelta(seconds=60)


class DriveError(RuntimeError):
    """Any non-2xx answer from Google."""


class OwnerRecsMusicError(RuntimeError):
    """The render's music is owner_recs, unlicensed or of unknown provenance:
    never uploaded."""


_REFUSED_MUSIC = frozenset({"owner_recs", "unlicensed"})
_SIDECARS = ("index.json", "spec.json")  # shorts index / compilation spec


def _read_sidecar(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def check_music_provenance(path: Path) -> None:
    """R5 Drive guard, fail closed. Raises `OwnerRecsMusicError` when:

    - a sibling sidecar (`index.json` or `spec.json`) says `music_folder` is
      owner_recs or unlicensed (anywhere on disk);
    - a sidecar records music (`music_path` set, or a `music_folder` key) whose
      `music_folder` is not `royalty_free`: null passes only with no music;
    - the file is under `WORKSPACE/shorts/**` and its `index.json` is missing,
      unreadable, not a dict or lacks `music_folder`.

    Files elsewhere without a music-bearing sidecar (highlights clips, digests,
    previews) carry no music and pass.
    """
    path = Path(path)
    sides = {name: _read_sidecar(path.parent / name) for name in _SIDECARS}
    for side in sides.values():
        if not isinstance(side, dict):
            continue
        folder = side.get("music_folder")
        if folder in _REFUSED_MUSIC:
            raise OwnerRecsMusicError(f"refused: {folder} music ({path.name})")
        if ("music_folder" in side or side.get("music_path")) and not (
            folder == "royalty_free" or (folder is None and not side.get("music_path"))
        ):
            raise OwnerRecsMusicError(f"refused: unknown music provenance ({path.name})")
    index = sides["index.json"]
    if inside(path, settings.workspace_dir / "shorts") and not (
        isinstance(index, dict) and "music_folder" in index
    ):
        raise OwnerRecsMusicError(f"refused: unknown music provenance ({path.name})")


class Uploader(Protocol):
    def upload(self, path: Path, *, name: str, subfolder: str) -> str: ...

    def prune(self, *, subfolder: str, older_than_days: int) -> int: ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _check(resp: httpx.Response, what: str) -> httpx.Response:
    if not resp.is_success:
        raise DriveError(f"Drive {what} failed: HTTP {resp.status_code} {resp.text[:200]}")
    return resp


def _q_escape(value: str) -> str:
    """Escape a value for interpolation inside a quoted Drive `q` string."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def create_folder(
    access_token: str,
    name: str,
    parent: str | None = None,
    *,
    client: httpx.Client | None = None,
) -> str:
    """Create a Drive folder (JSON POST to /files) and return its id."""
    metadata: dict = {"name": name, "mimeType": FOLDER_MIME}
    if parent:
        metadata["parents"] = [parent]
    resp = _check(
        (client or httpx).post(
            FILES_URL, json=metadata, headers={"Authorization": f"Bearer {access_token}"}
        ),
        "folder create",
    )
    return resp.json()["id"]


class DriveUploader:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        folder_id: str,
        *,
        client: httpx.Client | None = None,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._folder_id = folder_id
        self._client = client or httpx.Client(timeout=300.0)
        self._now = now
        self._token: str | None = None
        self._token_expiry: datetime = datetime.min.replace(tzinfo=timezone.utc)
        self._subfolders: dict[str, str] = {}

    # -- auth ---------------------------------------------------------
    def _access_token(self) -> str:
        if self._token and self._now() < self._token_expiry:
            return self._token
        resp = _check(
            self._client.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            ),
            "token refresh",
        )
        body = resp.json()
        self._token = body["access_token"]
        expires_in = int(body.get("expires_in", 3600))
        self._token_expiry = self._now() + timedelta(seconds=expires_in) - _EXPIRY_MARGIN
        return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token()}"}

    # -- folders ------------------------------------------------------
    def _subfolder_id(self, path: str) -> str:
        """Folder id for `a/b/c` under the root, creating each level once."""
        parts = [seg for seg in path.split("/") if seg]
        if not parts:
            return self._folder_id
        parent, key = self._folder_id, ""
        for seg in parts:
            key = f"{key}/{seg}" if key else seg
            if key not in self._subfolders:
                self._subfolders[key] = self._child_folder_id(parent, seg)
            parent = self._subfolders[key]
        return parent

    def _child_folder_id(self, parent: str, name: str) -> str:
        q = (
            f"'{_q_escape(parent)}' in parents and mimeType='{FOLDER_MIME}' "
            f"and name='{_q_escape(name)}' and trashed=false"
        )
        resp = _check(
            self._client.get(
                FILES_URL, params={"q": q, "fields": "files(id)"}, headers=self._headers()
            ),
            "folder lookup",
        )
        files = resp.json().get("files", [])
        if files:
            return files[0]["id"]
        return create_folder(self._access_token(), name, parent, client=self._client)

    # -- upload -------------------------------------------------------
    def _multipart(self, metadata: dict, content: bytes, content_type: str) -> str:
        boundary = uuid.uuid4().hex
        body = (
            (
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{json.dumps(metadata)}\r\n"
                f"--{boundary}\r\nContent-Type: {content_type}\r\n\r\n"
            ).encode()
            + content
            + f"\r\n--{boundary}--".encode()
        )
        headers = self._headers()
        headers["Content-Type"] = f"multipart/related; boundary={boundary}"
        resp = _check(self._client.post(UPLOAD_URL, content=body, headers=headers), "upload")
        return resp.json()["id"]

    def upload(self, path: Path, *, name: str, subfolder: str) -> str:
        parent = self._subfolder_id(subfolder)
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return self._multipart(
            {"name": name, "parents": [parent]}, Path(path).read_bytes(), content_type
        )

    # -- prune --------------------------------------------------------
    def prune(self, *, subfolder: str, older_than_days: int) -> int:
        parent = self._subfolder_id(subfolder)
        cutoff = (self._now() - timedelta(days=older_than_days)).astimezone(timezone.utc)
        q = (
            f"'{_q_escape(parent)}' in parents and trashed=false "
            f"and createdTime < '{cutoff.strftime('%Y-%m-%dT%H:%M:%S')}Z'"
        )
        deleted = 0
        page_token: str | None = None
        while True:
            params = {"q": q, "fields": "nextPageToken, files(id)"}
            if page_token:
                params["pageToken"] = page_token
            body = _check(
                self._client.get(FILES_URL, params=params, headers=self._headers()), "list"
            ).json()
            for f in body.get("files", []):
                _check(
                    self._client.delete(f"{FILES_URL}/{f['id']}", headers=self._headers()),
                    "delete",
                )
                deleted += 1
            page_token = body.get("nextPageToken")
            if not page_token:
                return deleted


class GuardedUploader:
    """Every Drive upload passes `check_music_provenance` first: the one choke
    point that publish and the Grok runner both hit."""

    def __init__(self, inner: Uploader) -> None:
        self._inner = inner

    def upload(self, path: Path, *, name: str, subfolder: str) -> str:
        check_music_provenance(Path(path))
        return self._inner.upload(path, name=name, subfolder=subfolder)

    def prune(self, *, subfolder: str, older_than_days: int) -> int:
        return self._inner.prune(subfolder=subfolder, older_than_days=older_than_days)


def uploader_from_settings() -> Uploader | None:
    s = settings
    fields = (
        s.grokbot_drive_client_id,
        s.grokbot_drive_client_secret,
        s.grokbot_drive_refresh_token,
        s.grokbot_drive_folder_id,
    )
    if not all(fields):
        return None
    return GuardedUploader(DriveUploader(*fields))
