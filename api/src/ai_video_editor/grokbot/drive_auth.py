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

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive.file"
HOST, PORT = "127.0.0.1", 8765
REDIRECT_URI = f"http://{HOST}:{PORT}/"

_DESCRIPTION = (
    "Authorize Drive access once and print a refresh token. Put the printed "
    "token in api/.env as GROKBOT_DRIVE_REFRESH_TOKEN. Needs "
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

    resp = httpx.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=30.0,
    )
    if not resp.is_success:
        sys.exit(f"Token exchange failed: HTTP {resp.status_code} {resp.text[:200]}")
    refresh = resp.json().get("refresh_token")
    if not refresh:
        sys.exit("No refresh token returned; revoke the app's access and retry.")
    print("\nRefresh token (shown once):\n" + refresh)
    print("\nPut it in api/.env as:\nGROKBOT_DRIVE_REFRESH_TOKEN=" + refresh)


if __name__ == "__main__":
    main()
