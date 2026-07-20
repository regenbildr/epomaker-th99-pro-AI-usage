"""Read Claude and Codex usage, preserving unavailable quota windows as N/A.

Codex is queried through the locally installed ``codex app-server`` JSON-RPC
interface in read-only/untrusted mode. Claude is queried through Anthropic's
official OAuth usage endpoint using the existing ``~/.claude/.credentials.json``
access token. Tokens are held in memory only, sent only to their provider, and
never printed, copied, cached, or written by this module. It has no keyboard,
HID, or USB imports.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
from typing import Any
import urllib.error
import urllib.request

import claude_oauth_refresh


CODEX_TIMEOUT_SECONDS = 30
CLAUDE_TIMEOUT_SECONDS = 20
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


@dataclass(frozen=True)
class UsageWindow:
    available: bool
    used_percent: int | None
    window_minutes: int
    resets_at: int | str | None


@dataclass(frozen=True)
class ProviderUsage:
    source: str
    five_hour: UsageWindow
    seven_day: UsageWindow


def bounded_percent(value: Any, label: str) -> int:
    """Validate provider utilization and floor it to the displayed whole percent."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} utilization is not numeric")
    raw_percent = float(value)
    if not 0 <= raw_percent <= 100:
        raise ValueError(f"{label} utilization {value!r} is outside 0..100")
    return math.floor(raw_percent)


def unavailable(minutes: int) -> UsageWindow:
    return UsageWindow(False, None, minutes, None)


def available(window: dict[str, Any], minutes: int, label: str) -> UsageWindow:
    return UsageWindow(
        True,
        bounded_percent(window.get("usedPercent"), label),
        minutes,
        window.get("resetsAt"),
    )


class CodexAppServer:
    def __init__(self, executable: str, timeout: int = CODEX_TIMEOUT_SECONDS):
        self.timeout = timeout
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(
            [executable, "-s", "read-only", "-a", "untrusted", "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        self.messages: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self.stderr_lines: list[str] = []
        self.next_id = 1
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                line = line.strip()
                if line:
                    self.messages.put(json.loads(line))
        except BaseException as error:
            self.messages.put(error)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            # Retain only a small bounded diagnostic tail. It should never
            # contain the contents of auth.json or bearer credentials.
            self.stderr_lines.append(line.strip())
            del self.stderr_lines[:-20]

    def send(self, message: dict[str, Any]) -> None:
        if self.process.poll() is not None:
            raise RuntimeError("Codex app-server exited before the request")
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: Any) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self.send({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Codex RPC {method} timed out")
            try:
                message = self.messages.get(timeout=remaining)
            except queue.Empty as error:
                raise TimeoutError(f"Codex RPC {method} timed out") from error
            if isinstance(message, BaseException):
                raise RuntimeError("failed to read Codex app-server output") from message
            if message.get("id") != request_id:
                # Notifications are expected and do not contain request results.
                continue
            if "error" in message:
                rpc_error = message["error"]
                code = rpc_error.get("code") if isinstance(rpc_error, dict) else None
                detail = rpc_error.get("message") if isinstance(rpc_error, dict) else str(rpc_error)
                raise RuntimeError(f"Codex RPC {method} failed ({code}): {detail}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"Codex RPC {method} returned no object result")
            return result

    def initialize(self) -> None:
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "th99_usage_probe",
                    "title": "TH99 Usage Probe",
                    "version": "0.1.0",
                }
            },
        )
        self.send({"method": "initialized", "params": {}})

    def close(self) -> None:
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)

    def __enter__(self) -> "CodexAppServer":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def codex_usage() -> ProviderUsage:
    executable = shutil.which("codex")
    if executable is None:
        raise FileNotFoundError("codex executable was not found on PATH")
    with CodexAppServer(executable) as server:
        server.initialize()
        result = server.request("account/rateLimits/read", None)

    snapshots: list[dict[str, Any]] = []
    by_id = result.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        preferred = by_id.get("codex")
        if isinstance(preferred, dict):
            snapshots.append(preferred)
        snapshots.extend(
            value
            for key, value in by_id.items()
            if key != "codex" and isinstance(value, dict)
        )
    legacy = result.get("rateLimits")
    if isinstance(legacy, dict):
        snapshots.append(legacy)

    five_hour = unavailable(300)
    seven_day = unavailable(10080)
    seen: set[tuple[object, object, object]] = set()
    for snapshot in snapshots:
        for name in ("primary", "secondary"):
            window = snapshot.get(name)
            if not isinstance(window, dict):
                continue
            identity = (
                window.get("windowDurationMins"),
                window.get("usedPercent"),
                window.get("resetsAt"),
            )
            if identity in seen:
                continue
            seen.add(identity)
            minutes = window.get("windowDurationMins")
            if minutes == 300:
                five_hour = available(window, 300, "Codex 5H")
            elif minutes == 10080:
                seven_day = available(window, 10080, "Codex 7D")

    if not five_hour.available and not seven_day.available:
        raise ValueError("Codex app-server returned no recognized 5H or 7D window")
    return ProviderUsage("codex_app_server", five_hour, seven_day)


def claude_credentials_path() -> Path:
    return claude_oauth_refresh.credentials_path()


def _claude_usage_request(access_token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        CLAUDE_USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "th99-usage-probe/0.1",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=CLAUDE_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def claude_usage() -> ProviderUsage:
    # Proactively refresh a near-expired token so we don't spend a guaranteed
    # 401 per poll; on an unexpected 401 (e.g. server-side revocation) force one
    # refresh and retry exactly once.
    access_token = claude_oauth_refresh.ensure_valid_access_token()
    try:
        document = _claude_usage_request(access_token)
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise RuntimeError(f"Claude usage API returned HTTP {error.code}") from error
        access_token = claude_oauth_refresh.ensure_valid_access_token(force=True)
        try:
            document = _claude_usage_request(access_token)
        except urllib.error.HTTPError as retry_error:
            raise RuntimeError(
                f"Claude usage API returned HTTP {retry_error.code} after refresh"
            ) from retry_error
        except urllib.error.URLError as retry_error:
            raise RuntimeError(
                f"Claude usage API connection failed: {retry_error.reason}"
            ) from retry_error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Claude usage API connection failed: {error.reason}") from error

    five_hour = document.get("five_hour")
    seven_day = document.get("seven_day")
    if not isinstance(five_hour, dict) or not isinstance(seven_day, dict):
        raise ValueError("Claude five_hour or seven_day usage window is missing")
    return ProviderUsage(
        "anthropic_oauth_usage_api",
        UsageWindow(
            True,
            bounded_percent(five_hour.get("utilization"), "Claude 5H"),
            300,
            five_hour.get("resets_at"),
        ),
        UsageWindow(
            True,
            bounded_percent(seven_day.get("utilization"), "Claude 7D"),
            10080,
            seven_day.get("resets_at"),
        ),
    )


def safe_error(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def collect_usage() -> tuple[dict[str, ProviderUsage], dict[str, str]]:
    providers: dict[str, ProviderUsage] = {}
    errors: dict[str, str] = {}
    for name, fetcher in (("claude", claude_usage), ("codex", codex_usage)):
        try:
            providers[name] = fetcher()
        except Exception as error:
            errors[name] = safe_error(error)
    return providers, errors


def display_percent(window: UsageWindow) -> str:
    return f"{window.used_percent}%" if window.available else "N/A"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit safe structured output")
    args = parser.parse_args()
    providers, errors = collect_usage()

    if args.json:
        output = {
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "read_only": True,
            "keyboard_access": False,
            "providers": {name: asdict(usage) for name, usage in providers.items()},
            "errors": errors,
        }
        print(json.dumps(output, indent=2))
    else:
        print("Provider usage probe - READ ONLY (no keyboard access)")
        for name, title in (("claude", "Claude"), ("codex", "Codex")):
            usage = providers.get(name)
            if usage is None:
                print(f"{title:6} ERROR: {errors[name]}")
                continue
            print(f"{title:6} 5H: {display_percent(usage.five_hour):>4} used")
            print(f"{title:6} 7D: {display_percent(usage.seven_day):>4} used")
    return 0 if not errors else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        raise SystemExit(130)
