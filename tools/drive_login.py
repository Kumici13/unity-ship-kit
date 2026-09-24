#!/usr/bin/env python3
"""One-time Google sign-in for DRIVE_MODE=personal (builds go to a normal Google Drive).

    python3 tools/drive_login.py ~/Downloads/client_secret_XXXX.json

Opens a browser, you pick the Google account, and a refresh token is saved to
DRIVE_OAUTH_TOKEN (default ~/.config/shipkit/drive-token.json, chmod 600). The bot
only gets the drive.file scope: it sees the files it created, nothing else in the Drive.
Re-run it if Google ever revokes the token (password change, removed access).
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import ssl
import sys
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

import certifi

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ship  # noqa: E402

SCOPE = "https://www.googleapis.com/auth/drive.file"


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    client = json.loads(Path(sys.argv[1]).expanduser().read_text())
    client = client.get("installed") or sys.exit("Expected a 'Desktop app' OAuth client JSON.")
    out = Path(ship.load_config().get("DRIVE_OAUTH_TOKEN")
               or "~/.config/shipkit/drive-token.json").expanduser()

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if q.get("state") == state:
                got.update(q)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Done - you can close this tab and go back to the terminal.")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print(f"Opening the browser. If it doesn't open, visit:\n{url}\n")
    webbrowser.open(url)
    while not got:
        server.handle_request()
    if "code" not in got:
        sys.exit(f"Sign-in failed: {got.get('error', 'no code returned')}")

    body = urllib.parse.urlencode({
        "code": got["code"], "client_id": client["client_id"],
        "client_secret": client["client_secret"], "redirect_uri": redirect,
        "grant_type": "authorization_code", "code_verifier": verifier}).encode()
    ctx = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen("https://oauth2.googleapis.com/token", body, context=ctx) as r:
        tok = json.loads(r.read())
    if "refresh_token" not in tok:
        sys.exit("Google returned no refresh token. Remove the app's access at "
                 "myaccount.google.com/permissions and run this again.")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"client_id": client["client_id"],
                               "client_secret": client["client_secret"],
                               "refresh_token": tok["refresh_token"]}))
    os.chmod(out, 0o600)
    print(f"Saved {out}. Set DRIVE_MODE=personal in config.env and re-run ./setup.sh.")


if __name__ == "__main__":
    main()
