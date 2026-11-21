"""Google Drive uploader for the Grok Bot team (httpx, no Google SDK).

The PC pushes clips, previews and renders into one dedicated Drive folder
that the cloud bots read. Auth is an OAuth refresh token (obtained once via
`drive-auth`) exchanged for short-lived access tokens, cached until expiry.
Inert (``uploader_from_settings() -> None``) until fully configured.
"""

from __future__ import annotations

import json
import mimetypes
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import httpx

from ..config import settings

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
# Refresh a little early so a token never expires mid-request.
_EXPIRY_MARGIN = timedelta(seconds=60)
# Drive file ids are opaque; reject anything that could alter the URL path.
DRIVE_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,128}$")
# Cap a single download so a large file cannot fill the box's RAM/disk.
DEFAULT_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB


class DriveError(RuntimeError):
    """Any non-2xx answer from Google."""


class Uploader(Protocol):
    def upload(self, path: Path, *, name: str, subfolder: str) -> str: ...

    def prune(self, *, subfolder: str, older_than_days: int) -> int: ...

    def download(
        self,
        file_id: str,
        dest: Path,
        *,
        max_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        timeout: float = 300.0,
    ) -> Path: ...


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
    def _subfolder_id(self, name: str) -> str:
        if name in self._subfolders:
            return self._subfolders[name]
        q = (
            f"'{_q_escape(self._folder_id)}' in parents and mimeType='{FOLDER_MIME}' "
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
            folder_id = files[0]["id"]
        else:
            folder_id = create_folder(
                self._access_token(), name, self._folder_id, client=self._client
            )
        self._subfolders[name] = folder_id
        return folder_id

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

    # -- download -----------------------------------------------------
    def download(
        self,
        file_id: str,
        dest: Path,
        *,
        max_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        timeout: float = 300.0,
    ) -> Path:
        """Stream a Drive file to ``dest`` with an id check and byte cap."""
        if not DRIVE_FILE_ID_RE.fullmatch(file_id):
            raise DriveError("invalid Drive file id")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = f"{FILES_URL}/{file_id}"
        written = 0
        try:
            with self._client.stream(
                "GET",
                url,
                params={"alt": "media"},
                headers=self._headers(),
                follow_redirects=True,
                timeout=timeout,
            ) as resp:
                if not resp.is_success:
                    raise DriveError(
                        f"Drive download failed: HTTP {resp.status_code} {resp.text[:200]}"
                    )
                with dest.open("wb") as out:
                    for chunk in resp.iter_bytes():
                        written += len(chunk)
                        if written > max_bytes:
                            out.close()
                            dest.unlink(missing_ok=True)
                            raise DriveError("download exceeds size cap")
                        out.write(chunk)
        except DriveError:
            raise
        except httpx.HTTPError as e:
            dest.unlink(missing_ok=True)
            raise DriveError(f"Drive download network error: {e}") from e
        except OSError as e:
            dest.unlink(missing_ok=True)
            raise DriveError(f"Drive download IO error: {e}") from e
        return dest

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
    return DriveUploader(*fields)
