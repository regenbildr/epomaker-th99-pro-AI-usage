"""Direct OAuth refresh for the Claude Code credential on Windows.

Claude Code stores its OAuth credential in plaintext at
``~/.claude/.credentials.json`` on Windows (unlike macOS, where it lives in the
login Keychain). Because the refresh token is fully readable and the file is a
single source of truth we can own, we perform the same refresh-token grant that
Claude Code performs internally, then write the rotated credential back
atomically. This replaces the delegated-``/status`` ConPTY experiments.

The request shape matches Claude Code's own ``cli.js`` (version 2.1.37):

    POST https://platform.claude.com/v1/oauth/token
    Content-Type: application/json
    {"grant_type": "refresh_token", "refresh_token": ..., "client_id": ...,
     "scope": "<space-joined scopes>"}

Response: {access_token, refresh_token?, expires_in, scope?}. If the response
omits ``refresh_token`` we keep the existing one (Claude Code does the same).

Safety:
- Tokens are never printed, logged, or copied outside the credential file.
- A file lock prevents racing Claude Code's own refresh.
- The complete credential object is preserved; only the rotated fields change.
- The write is atomic (temp file + os.replace).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request

# Authoritative values, extracted from the installed Claude Code cli.js.
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

# Refresh when fewer than this many seconds remain before expiry.
REFRESH_SKEW_SECONDS = 300
REFRESH_TIMEOUT_SECONDS = 30

# A plain urllib User-Agent is blocked by Cloudflare (error 1010); identify as
# the CLI so the request reaches the OAuth handler.
_USER_AGENT = "claude-cli/2.1.37 (external, oauth-refresh)"

# Lock parameters guard against concurrent refreshes with Claude Code.
_LOCK_TIMEOUT_SECONDS = 20
_LOCK_STALE_SECONDS = 60


def credentials_path() -> Path:
    return Path.home() / ".claude" / ".credentials.json"


def _lock_path() -> Path:
    return credentials_path().with_name(".credentials.refresh.lock")


class _FileLock:
    """Best-effort cross-process lock using an exclusive lock file.

    Breaks a stale lock older than ``_LOCK_STALE_SECONDS`` so a crashed refresh
    never wedges the monitor permanently.
    """

    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "_FileLock":
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                    if age > _LOCK_STALE_SECONDS:
                        self.path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError("Could not acquire Claude refresh lock")
                time.sleep(0.2)

    def __exit__(self, *_: Any) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.path.unlink(missing_ok=True)


def _load() -> tuple[dict[str, Any], dict[str, Any]]:
    path = credentials_path()
    if not path.is_file():
        raise FileNotFoundError(f"Claude credentials were not found at {path}")
    document = json.loads(path.read_text(encoding="utf-8"))
    oauth = document.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        raise ValueError("Claude credentials contain no claudeAiOauth object")
    return document, oauth


def _seconds_remaining(oauth: dict[str, Any]) -> float:
    expires_at = oauth.get("expiresAt")
    if not isinstance(expires_at, (int, float)):
        return -1.0
    return expires_at / 1000.0 - time.time()


def _atomic_write(document: dict[str, Any]) -> None:
    path = credentials_path()
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(document, indent=2), encoding="utf-8")
    os.replace(tmp, path)  # atomic on Windows and POSIX


def _post_refresh(refresh_token: str, scopes: list[str]) -> dict[str, Any]:
    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "scope": " ".join(scopes),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REFRESH_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        # Never surface the response body; it may echo the submitted token.
        raise RuntimeError(f"Claude token refresh returned HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Claude token refresh connection failed: {error.reason}") from error


def refresh_now() -> None:
    """Perform a refresh and write the rotated credential back atomically."""
    with _FileLock(_lock_path()):
        # Re-read under the lock so we rotate the newest token, not a stale one.
        document, oauth = _load()
        refresh_token = oauth.get("refreshToken")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise ValueError("Claude OAuth refresh token is missing")
        scopes = oauth.get("scopes")
        if not isinstance(scopes, list):
            scopes = []

        data = _post_refresh(refresh_token, [s for s in scopes if isinstance(s, str)])

        access_token = data.get("access_token")
        expires_in = data.get("expires_in")
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("Refresh response missing access_token")
        if not isinstance(expires_in, (int, float)):
            raise ValueError("Refresh response missing expires_in")

        oauth["accessToken"] = access_token
        # refresh_token may rotate; keep the old one if the server omits it.
        new_refresh = data.get("refresh_token")
        if isinstance(new_refresh, str) and new_refresh:
            oauth["refreshToken"] = new_refresh
        oauth["expiresAt"] = int(time.time() * 1000 + float(expires_in) * 1000)
        scope = data.get("scope")
        if isinstance(scope, str) and scope:
            oauth["scopes"] = scope.split()

        document["claudeAiOauth"] = oauth
        _atomic_write(document)


def ensure_valid_access_token(force: bool = False) -> str:
    """Return a valid access token, refreshing first if it is near expiry.

    Reads the credential file fresh so a token Claude Code already refreshed is
    used as-is without an unnecessary rotation.
    """
    _, oauth = _load()
    if force or _seconds_remaining(oauth) < REFRESH_SKEW_SECONDS:
        refresh_now()
        _, oauth = _load()
    access_token = oauth.get("accessToken")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("Claude OAuth access token is missing after refresh")
    return access_token


def main() -> int:
    _, oauth = _load()
    remaining = _seconds_remaining(oauth)
    print(f"Token valid for {remaining / 3600:.2f}h before refresh")
    ensure_valid_access_token(force="--force" in sys.argv)
    _, oauth = _load()
    print(f"Token now valid for {_seconds_remaining(oauth) / 3600:.2f}h")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
