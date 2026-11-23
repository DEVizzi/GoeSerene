"""Re-authorize the GeoSerene YouTube OAuth refresh token and push secrets to GitHub.

Why YouTube stopped posting Reels while Instagram continued:
- Google Cloud OAuth apps in "Testing" publishing status expire refresh tokens after 7 days
  (`invalid_grant: Token has been expired or revoked.`).
- First, in Google Cloud Console -> APIs & Services -> OAuth consent screen, click
  "PUBLISH APP" (move from "Testing" to "In production") so the new refresh token never expires!
- Then run this script once locally:
      python3 scripts/youtube_reauth.py --client-secrets ~/Downloads/client_secret_XXXX.json

Security guarantees:
- Listens strictly on loopback `127.0.0.1` (never `0.0.0.0`) with PKCE (S256) and CSRF `state`.
- Never prints or logs authorization codes, client secrets, or refresh tokens.
- Uploads `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, and `YOUTUBE_REFRESH_TOKEN` via stdin
  directly to `gh secret set --repo DEVizzi/GoeSerene`.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

DEFAULT_REPO = "DEVizzi/GoeSerene"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels?part=id,snippet&mine=true"
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)
_SAFE_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_LOOPBACK = re.compile(r"^http://(localhost|127\.0\.0\.1):(\d{2,5})/?$")
CALLBACK_TIMEOUT_S = 300


def load_client(path: Path) -> tuple[str, str, list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    kind = "installed" if "installed" in data else "web"
    block = data.get(kind) or {}
    cid = str(block.get("client_id") or "").strip()
    csec = str(block.get("client_secret") or "").strip()
    if not cid.endswith(".apps.googleusercontent.com") or not csec:
        raise ValueError("Client secrets JSON is missing a valid Google OAuth client_id / client_secret.")
    redirects = [str(u).strip() for u in (block.get("redirect_uris") or [])] if kind == "web" else []
    return cid, csec, redirects


def pick_redirect(redirects: list[str]) -> tuple[str, int] | None:
    for uri in redirects:
        m = _LOOPBACK.match(uri)
        if m:
            return uri, int(m.group(2))
    return None


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_auth_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(query)}"


def parse_callback(path: str, expected_state: str) -> str:
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
    if qs.get("error"):
        raise ValueError(f"OAuth consent was not granted ({qs['error'][0][:60]})")
    got_state = (qs.get("state") or [""])[0]
    if not secrets.compare_digest(got_state.encode("utf-8"), expected_state.encode("utf-8")):
        raise ValueError("OAuth state mismatch (CSRF check failed); aborting.")
    code = (qs.get("code") or [""])[0]
    if not code:
        raise ValueError("No authorization code received in OAuth callback.")
    return code


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if urllib.parse.urlparse(self.path).path != "/":
            self.send_response(404)
            self.end_headers()
            return
        self.server.callback_path = self.path  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(b"<h2>GeoSerene YouTube authorization received. You may close this tab.</h2>")

    def log_message(self, *_args) -> None:
        return  # Never log query parameters containing OAuth codes


def wait_for_callback(server: http.server.HTTPServer, timeout_s: float = CALLBACK_TIMEOUT_S) -> str:
    deadline = time.monotonic() + timeout_s
    server.timeout = 2
    while time.monotonic() < deadline:
        server.handle_request()
        path = getattr(server, "callback_path", None)
        if path:
            return str(path)
    raise TimeoutError("Timed out waiting for browser OAuth callback (5 minutes).")


def exchange_code(code: str, client_id: str, client_secret: str, redirect_uri: str, verifier: str) -> dict:
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }).encode("ascii")
    req = urllib.request.Request(TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - hardcoded Google OAuth HTTPS endpoint
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"OAuth token exchange failed with HTTP {exc.code}.") from None


def verify_youtube_channel(access_token: str) -> str:
    req = urllib.request.Request(CHANNELS_URL, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - hardcoded Google YouTube API HTTPS endpoint
        data = json.loads(resp.read().decode("utf-8"))
    items = data.get("items") or []
    if not items:
        raise RuntimeError("No YouTube channel found on the selected Google account.")
    ch = items[0]
    return f"{ch['snippet']['title']} ({ch['id']})"


def gh_secret_set(gh: str, repo: str, name: str, value: str) -> None:
    res = subprocess.run(
        [gh, "secret", "set", name, "--repo", repo],
        input=value,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"`gh secret set {name}` failed (exit {res.returncode}). Check `gh auth status`.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh GeoSerene YouTube OAuth secrets in GitHub.")
    parser.add_argument("--client-secrets", type=Path, required=True, help="Path to Google OAuth client_secret.json")
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"GitHub owner/repo (default: {DEFAULT_REPO})")
    args = parser.parse_args(argv)

    if not _SAFE_REPO.match(args.repo):
        raise ValueError(f"Invalid GitHub repository format: {args.repo}")
    gh = shutil.which("gh")
    if not gh:
        raise RuntimeError("GitHub CLI (`gh`) not found in PATH.")

    client_id, client_secret, redirects = load_client(args.client_secrets.expanduser().resolve())
    picked = pick_redirect(redirects)
    bind_port = picked[1] if picked else 0

    server = http.server.HTTPServer(("127.0.0.1", bind_port), _CallbackHandler)
    try:
        actual_port = server.server_address[1]
        redirect_uri = picked[0] if picked else f"http://127.0.0.1:{actual_port}/"
        state = secrets.token_urlsafe(32)
        verifier, challenge = pkce_pair()
        auth_url = build_auth_url(client_id, redirect_uri, state, challenge)

        print("1. Make sure Google Cloud OAuth Consent Screen is set to 'In production' (not 'Testing').")
        print("2. Opening Google consent screen in your browser — select the GeoSerene YouTube channel...")
        webbrowser.open(auth_url)
        cb_path = wait_for_callback(server)
    finally:
        server.server_close()

    code = parse_callback(cb_path, state)
    tok = exchange_code(code, client_id, client_secret, redirect_uri, verifier)
    refresh_token = str(tok.get("refresh_token") or "").strip()
    access_token = str(tok.get("access_token") or "").strip()
    if not refresh_token or not access_token:
        raise RuntimeError("Google did not return a refresh_token. Revoke prior app access and retry.")

    channel_label = verify_youtube_channel(access_token)
    print(f"✅ Verified YouTube channel: {channel_label}")

    gh_secret_set(gh, args.repo, "YOUTUBE_CLIENT_ID", client_id)
    gh_secret_set(gh, args.repo, "YOUTUBE_CLIENT_SECRET", client_secret)
    gh_secret_set(gh, args.repo, "YOUTUBE_REFRESH_TOKEN", refresh_token)
    print(f"✅ Updated YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, and YOUTUBE_REFRESH_TOKEN in {args.repo}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
