"""One-time Google Drive OAuth helper (`uv run drive-auth`).

Runs the installed-app loopback flow on http://127.0.0.1:8765/, exchanges
the code for tokens and prints the refresh token once. Stdlib server only.
"""

from __future__ import annotations

import argparse
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .drive import DriveError, create_folder

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive.file"
HOST, PORT = "127.0.0.1", 8765
REDIRECT_URI = f"http://{HOST}:{PORT}/"
DEFAULT_FOLDER_NAME = "ai-video-editor-bots"

_DESCRIPTION = (
    "Authorize Drive access once, create the root bot folder, and print a "
    "refresh token and folder id. Put them in api/.env as "
    "GROKBOT_DRIVE_REFRESH_TOKEN and GROKBOT_DRIVE_FOLDER_ID. Needs "
    "GROKBOT_DRIVE_CLIENT_ID / GROKBOT_DRIVE_CLIENT_SECRET (or pass them)."
)


def _wait_for_code(state: str) -> str:
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            q = parse_qs(urlparse(self.path).query)
            if q.get("state", [""])[0] == state and "code" in q:
                result["code"] = q["code"][0]
            else:
                result["error"] = q.get("error", ["missing code"])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab.")
            done.set()

        def log_message(self, *args) -> None:
            pass

    server = HTTPServer((HOST, PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        done.wait(timeout=300)
    finally:
        server.shutdown()
        server.server_close()
    if "code" not in result:
        raise SystemExit(f"Authorization failed: {result.get('error', 'timed out')}")
    return result["code"]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="drive-auth", description=_DESCRIPTION)
    parser.add_argument("--client-id", default=None)
    parser.add_argument("--client-secret", default=None)
    parser.add_argument(
        "--folder-name",
        default=DEFAULT_FOLDER_NAME,
        help=f"Name of the root Drive folder to create (default: {DEFAULT_FOLDER_NAME}).",
    )
    args = parser.parse_args(argv)

    from ..config import settings

    client_id = args.client_id or settings.grokbot_drive_client_id
    client_secret = args.client_secret or settings.grokbot_drive_client_secret
    if not (client_id and client_secret):
        sys.exit("Set GROKBOT_DRIVE_CLIENT_ID and GROKBOT_DRIVE_CLIENT_SECRET first.")

    state = secrets.token_urlsafe(16)
    url = (
        AUTH_URL
        + "?"
        + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "scope": SCOPE,
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
            }
        )
    )
    print("Opening browser to authorize Drive access...\nIf it does not open, visit:\n" + url)
    webbrowser.open(url)
    code = _wait_for_code(state)

    try:
        refresh, folder_id = exchange_and_create_folder(
            code, client_id, client_secret, args.folder_name
        )
    except DriveError as e:
        sys.exit(str(e))
    print("\nShown once. Put these two lines in api/.env:\n")
    print("GROKBOT_DRIVE_REFRESH_TOKEN=" + refresh)
    print("GROKBOT_DRIVE_FOLDER_ID=" + folder_id)


def exchange_and_create_folder(
    code: str,
    client_id: str,
    client_secret: str,
    folder_name: str = DEFAULT_FOLDER_NAME,
    *,
    client: httpx.Client | None = None,
) -> tuple[str, str]:
    """Trade the auth code for tokens, then create the root bot folder.

    The `drive.file` scope only sees files this app created, so the root
    folder must be created here rather than by hand in the Drive UI.
    Returns (refresh_token, folder_id).
    """
    http = client or httpx.Client(timeout=30.0)
    resp = http.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
    )
    if not resp.is_success:
        raise DriveError(f"Token exchange failed: HTTP {resp.status_code} {resp.text[:200]}")
    body = resp.json()
    refresh = body.get("refresh_token")
    if not refresh:
        raise DriveError("No refresh token returned; revoke the app's access and retry.")
    folder_id = create_folder(body["access_token"], folder_name, client=http)
    return refresh, folder_id


if __name__ == "__main__":
    main()
