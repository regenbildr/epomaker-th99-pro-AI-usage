"""Fetch Claude/Codex usage and safely update the TH99 Pro TFT.

Default mode creates a preview only. Live mode writes only to the confirmed
TH99 Pro MI_03 TFT interface and requires an explicit acknowledgement phrase.
Watch mode polls providers periodically, but uploads only when a displayed
normalized whole-number provider utilization value or layout changes and the minimum write interval has
elapsed. A detected USB disconnect is the narrow exception: the next confirmed
reconnect gets one recovery upload because the keyboard returns to its native
screen after power loss.

The Watcher loop is shared by the CLI (``main``) and the tray app
(``build_watcher``). Previews are written atomically (temp file + replace) so a
reader never sees a half-drawn image.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import math
from pathlib import Path
import sys
import threading
import time

from provider_usage_probe import ProviderUsage, collect_usage, display_percent
from th99_four_bar_renderer import (
    DISPLAY_MODE_PROGRESS_BAR,
    DISPLAY_MODE_RESET_TIMER,
    DISPLAY_MODES,
    pixels_to_rgb565,
    render_pixels,
)
from th99_four_bar_renderer import write_preview as _render_write_preview
from th99_tft_container import build_container, parse_container
from th99_tft_hid_transport import enumerate_hid_paths, open_hid, th99_display_paths
from th99_tft_protocol import build_reports, capture_upload, sha256
from th99_tft_upload import KNOWN_CAPTURE_FILE_HASH, file_sha256, send_upload


ACKNOWLEDGEMENT = "UPLOAD_LIVE_USAGE"
TIMING_FROM_OFFICIAL_TWO_FRAME_CAPTURE = b"\x32"

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CAPTURE = REPO_ROOT / "data" / "captures" / "th99-upload.pcap"
DEFAULT_PREVIEW = REPO_ROOT / "assets" / "th99-live-usage-current.bmp"
PREVIEW_PATH = DEFAULT_PREVIEW


def write_preview(
    path: Path,
    values: tuple[int | None, ...],
    *,
    display_mode: str = DISPLAY_MODE_PROGRESS_BAR,
    reset_timers: tuple[str | None, ...] | None = None,
) -> None:
    """Replace the preview only after a complete new BMP is ready."""
    temporary = path.with_name(f".{path.name}.new")
    if temporary.exists():
        temporary.unlink()
    _render_write_preview(
        temporary,
        values,
        display_mode=display_mode,
        reset_timers=reset_timers,
    )
    temporary.replace(path)


def value(usage: ProviderUsage, window_name: str) -> int | None:
    window = getattr(usage, window_name)
    return window.used_percent if window.available else None


def values_from_providers(
    providers: dict[str, ProviderUsage],
) -> tuple[int | None, int | None, int | None, int | None]:
    claude = providers["claude"]
    codex = providers["codex"]
    return (
        value(claude, "five_hour"),
        value(claude, "seven_day"),
        value(codex, "five_hour"),
        value(codex, "seven_day"),
    )


def _parse_reset_time(value: int | str | None) -> datetime | None:
    """Normalize provider reset timestamps to timezone-aware UTC datetimes."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if abs(seconds) > 10_000_000_000:  # tolerate epoch milliseconds
            seconds /= 1000
        try:
            return datetime.fromtimestamp(seconds, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return _parse_reset_time(float(raw))
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_reset_timer(
    resets_at: int | str | None,
    *,
    now: datetime | None = None,
) -> str | None:
    """Return ``0D 03H 09M`` until reset, or ``None`` when no reset is known."""
    reset_time = _parse_reset_time(resets_at)
    if reset_time is None:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    remaining_minutes = max(0, math.ceil((reset_time - current).total_seconds() / 60))
    days, remainder = divmod(remaining_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    return f"{days}D {hours:02d}H {minutes:02d}M"


def reset_timers_from_providers(
    providers: dict[str, ProviderUsage],
    *,
    now: datetime | None = None,
) -> tuple[str | None, str | None, str | None, str | None]:
    claude = providers["claude"]
    codex = providers["codex"]
    windows = (claude.five_hour, claude.seven_day, codex.five_hour, codex.seven_day)
    return tuple(
        format_reset_timer(window.resets_at, now=now) if window.available else None
        for window in windows
    )  # type: ignore[return-value]

def display_guard_tuple(
    values: tuple[int | None, int | None, int | None, int | None],
    display_mode: str,
) -> tuple[object, ...]:
    """Write guard: layout plus four normalized whole provider utilizations only."""
    if display_mode not in DISPLAY_MODES:
        raise ValueError(f"unknown display mode: {display_mode}")
    return (display_mode, *values)

def build_display_reports(
    values: tuple[int | None, int | None, int | None, int | None],
    *,
    display_mode: str = DISPLAY_MODE_PROGRESS_BAR,
    reset_timers: tuple[str | None, ...] | None = None,
) -> tuple[bytes, bytes, list[bytes]]:
    pixels = render_pixels(
        *values,
        display_mode=display_mode,
        reset_timers=reset_timers,
    )
    frame = pixels_to_rgb565(pixels)
    payload = build_container([frame, frame], TIMING_FROM_OFFICIAL_TWO_FRAME_CAPTURE)
    parsed = parse_container(payload)
    if parsed.frames != (frame, frame):
        raise ValueError("live payload does not contain two identical frames")
    if parsed.metadata[:3] != b"\x02\x32\x00":
        raise ValueError("live payload metadata is not the confirmed layout")
    reports = build_reports(payload)
    if len(reports) != 16 or any(len(report) != 4104 for report in reports):
        raise ValueError("live payload must produce 16 reports of 4104 bytes")
    return frame, payload, reports


def validate_reference_capture(path: Path) -> None:
    captured_reports, _, captured_payload = capture_upload(path)
    if build_reports(captured_payload) != captured_reports:
        raise ValueError("reviewed official upload does not regenerate exactly")


class TftDeviceUnavailable(RuntimeError):
    """Expected temporary absence of the TH99 Pro MI_03 interface."""


def find_display_path() -> str:
    candidates = th99_display_paths(enumerate_hid_paths())
    if not candidates:
        raise TftDeviceUnavailable("TH99 Pro MI_03 interface is not connected")
    if len(candidates) != 1:
        raise RuntimeError(
            f"expected exactly one TH99 Pro MI_03 interface, found {len(candidates)}"
        )
    return candidates[0]


def upload_reports(
    reports: list[bytes], timeout_ms: int, *, path: str | None = None
) -> None:
    """Send one approved report set to a selected MI_03 interface."""
    kernel32, handle = open_hid(path if path is not None else find_display_path())
    try:
        send_upload(kernel32, handle, reports, timeout_ms)
    finally:
        kernel32.CloseHandle(handle)


def describe(providers: dict[str, ProviderUsage]) -> None:
    claude = providers["claude"]
    codex = providers["codex"]
    print(
        "Claude: "
        f"5H={display_percent(claude.five_hour)} "
        f"7D={display_percent(claude.seven_day)}"
    )
    print(
        "Codex:  "
        f"5H={display_percent(codex.five_hour)} "
        f"7D={display_percent(codex.seven_day)}"
    )


class Watcher:
    """Runs the poll -> render -> (optional) upload cycle.

    Shared by the CLI (``main``) and the tray app. When ``verbose`` is true it
    prints the same output the CLI always has; ``on_status`` receives a small
    dict each cycle so a GUI can show the latest values without parsing stdout.
    """

    def __init__(
        self,
        *,
        execute_upload: bool = False,
        acknowledge: str = "",
        poll_seconds: int = 120,
        min_upload_seconds: int = 900,
        display_mode: str = DISPLAY_MODE_PROGRESS_BAR,
        timeout_ms: int = 5000,
        capture: Path = DEFAULT_CAPTURE,
        preview: Path = DEFAULT_PREVIEW,
        verbose: bool = False,
        on_status=None,
    ):
        self.execute_upload = execute_upload
        self.acknowledge = acknowledge
        self.poll_seconds = poll_seconds
        self.min_upload_seconds = min_upload_seconds
        self.display_mode = display_mode
        self.timeout_ms = timeout_ms
        self.capture = capture
        self.preview = preview
        self.verbose = verbose
        self.on_status = on_status
        self._last_displayed: tuple[object, ...] | None = None
        self._last_upload_time = 0.0
        # A successful upload describes only the current powered keyboard
        # session. On a detected absence (or an I/O failure during reconnect),
        # invalidate it so unchanged usage still restores the custom screen.
        self._screen_state_unknown = False
        self._last_recovery_attempt_time = 0.0

    def _log(self, message: str, *, error: bool = False) -> None:
        if self.verbose:
            print(message, file=sys.stderr if error else sys.stdout)

    def _emit(self, **status) -> None:
        if self.on_status is not None:
            self.on_status(status)

    def validate(self) -> None:
        """Raise ValueError if the configuration is unsafe for live upload."""
        if self.poll_seconds < 60:
            raise ValueError("poll_seconds must be at least 60")
        if self.min_upload_seconds < 300:
            raise ValueError("min_upload_seconds must be at least 300")
        if not 500 <= self.timeout_ms <= 30000:
            raise ValueError("timeout_ms must be between 500 and 30000")
        if self.display_mode not in DISPLAY_MODES:
            raise ValueError(f"unknown display mode: {self.display_mode}")
        if self.execute_upload:
            if self.acknowledge != ACKNOWLEDGEMENT:
                raise ValueError(f"live mode requires acknowledgement {ACKNOWLEDGEMENT}")
            if file_sha256(self.capture) != KNOWN_CAPTURE_FILE_HASH:
                raise ValueError("live mode is pinned to the reviewed TFT capture")
            validate_reference_capture(self.capture)

    def set_display_mode(self, display_mode: str) -> None:
        """Apply a layout choice without bypassing the screen-update limit."""
        if display_mode not in DISPLAY_MODES:
            raise ValueError(f"unknown display mode: {display_mode}")
        self.display_mode = display_mode

    def _mark_screen_state_unknown(self) -> None:
        """Require one recovery upload after a keyboard reconnect."""
        if not self._screen_state_unknown:
            self._screen_state_unknown = True
            self._last_recovery_attempt_time = 0.0

    def _emit_device_unavailable(
        self,
        values: tuple[int | None, ...],
        *,
        status: str,
        reason: Exception,
        attempted_upload: bool = False,
    ) -> None:
        self._mark_screen_state_unknown()
        if attempted_upload:
            # A failed transfer may have delivered some reports before Windows
            # noticed the disconnect. Keep retries inside the normal write
            # interval rather than repeatedly sending a partial container.
            self._last_recovery_attempt_time = time.monotonic()
        self._log(
            f"TH99 display {status}: {reason}; recovery will retry when eligible.",
            error=True,
        )
        self._emit(values=values, errors={}, uploaded=False, device_status=status)

    def run_cycle(self) -> tuple[tuple[int | None, ...] | None, dict[str, str]]:
        """Run one poll/render/upload cycle. Returns (values, errors)."""
        providers, errors = collect_usage()
        if errors:
            for name, error in errors.items():
                self._log(f"{name} fetch error: {error}", error=True)
            self._emit(values=None, errors=errors, uploaded=False)
            return None, errors

        values = values_from_providers(providers)
        display_path: str | None = None
        if self.execute_upload:
            try:
                display_path = find_display_path()
            except TftDeviceUnavailable as error:
                self._emit_device_unavailable(
                    values, status="disconnected", reason=error
                )
                return values, {}
            except OSError as error:
                # SetupAPI can race Windows' HID teardown/re-enumeration. It is
                # safe to retry, and no TFT packet was constructed or sent.
                self._emit_device_unavailable(
                    values, status="reconnecting", reason=error
                )
                return values, {}

        display_state = display_guard_tuple(values, self.display_mode)
        changed = self._screen_state_unknown or display_state != self._last_displayed
        now = time.monotonic()
        enough_time = now - self._last_upload_time >= self.min_upload_seconds
        recovery_due = self._screen_state_unknown and (
            self._last_recovery_attempt_time == 0.0
            or now - self._last_recovery_attempt_time >= self.min_upload_seconds
        )
        if self.execute_upload and self._screen_state_unknown:
            # Recovery-pending state takes priority over the normal "first
            # upload" exception, so a failed transfer cannot retry every poll.
            should_update_image = recovery_due
        else:
            should_update_image = changed and (
                not self.execute_upload
                or self._last_displayed is None
                or enough_time
            )
        if self.verbose:
            describe(providers)

        uploaded = False
        if should_update_image:
            # The timer is remaining time until the provider's reported reset.
            # It is calculated only after the existing percentage/layout guard
            # has authorized a new image, so countdown ticks cannot add work or
            # consume a TFT flash-write cycle.
            reset_timers = (
                reset_timers_from_providers(providers)
                if self.display_mode == DISPLAY_MODE_RESET_TIMER
                else None
            )
            frame, payload, reports = build_display_reports(
                values,
                display_mode=self.display_mode,
                reset_timers=reset_timers,
            )
            write_preview(
                self.preview,
                values,
                display_mode=self.display_mode,
                reset_timers=reset_timers,
            )
            self._log(f"Preview: {self.preview.resolve()}")
            self._log(f"Frame SHA-256: {sha256(frame)}")
            self._log(f"Payload SHA-256: {sha256(payload)}")

            if not self.execute_upload:
                self._log("DRY RUN: no HID handle was opened and no report was sent.")
                self._last_displayed = display_state
            else:
                try:
                    upload_reports(reports, self.timeout_ms, path=display_path)
                except (TftDeviceUnavailable, OSError, TimeoutError) as error:
                    # A cable/dock reconnect may disappear after the preflight
                    # enumeration. Do not turn that expected race into a fatal
                    # watcher failure; no successful upload state is recorded.
                    self._emit_device_unavailable(
                        values,
                        status="reconnecting",
                        reason=error,
                        attempted_upload=True,
                    )
                    return values, {}
                self._last_displayed = display_state
                self._last_upload_time = time.monotonic()
                self._screen_state_unknown = False
                self._last_recovery_attempt_time = 0.0
                uploaded = True
                self._log("TFT upload completed with 16/16 reports acknowledged.")
        elif not self.execute_upload:
            self._log("Preview unchanged; skipped render.")
        elif self._screen_state_unknown:
            remaining = round(
                self.min_upload_seconds
                - (time.monotonic() - self._last_recovery_attempt_time)
            )
            self._log(
                f"TFT recovery pending; retrying in {max(0, remaining)} seconds."
            )
        elif not changed:
            self._log("TFT unchanged; skipped upload.")
        else:
            remaining = round(
                self.min_upload_seconds - (time.monotonic() - self._last_upload_time)
            )
            self._log(f"Value changed; deferring upload for {max(0, remaining)} seconds.")

        self._emit(
            values=values,
            errors={},
            uploaded=uploaded,
            device_status=(
                "reconnecting"
                if self.execute_upload and self._screen_state_unknown
                else ("connected" if self.execute_upload else "preview")
            ),
        )
        return values, errors

    def run_forever(self, stop_event: "threading.Event | None" = None) -> int:
        """Poll until ``stop_event`` is set. Sleeps interruptibly between polls."""
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            self.run_cycle()
            self._log(f"Next provider poll in {self.poll_seconds} seconds. Ctrl+C to stop.")
            if stop_event.wait(self.poll_seconds):
                break
        return 0


def build_watcher(*, on_status=None, execute_upload: bool = True, **kwargs) -> "Watcher":
    """A Watcher wired to the production acknowledgement (used by the tray app)."""
    return Watcher(
        execute_upload=execute_upload,
        acknowledge=ACKNOWLEDGEMENT,
        on_status=on_status,
        verbose=False,
        **kwargs,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-upload", action="store_true")
    parser.add_argument("--acknowledge", default="")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=120)
    parser.add_argument("--min-upload-seconds", type=int, default=900)
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument(
        "--display-mode", choices=DISPLAY_MODES, default=DISPLAY_MODE_PROGRESS_BAR
    )
    parser.add_argument("--capture", type=Path, default=DEFAULT_CAPTURE)
    parser.add_argument("--preview", type=Path, default=DEFAULT_PREVIEW)
    args = parser.parse_args()

    watcher = Watcher(
        execute_upload=args.execute_upload,
        acknowledge=args.acknowledge,
        poll_seconds=args.poll_seconds,
        min_upload_seconds=args.min_upload_seconds,
        display_mode=args.display_mode,
        timeout_ms=args.timeout_ms,
        capture=args.capture,
        preview=args.preview,
        verbose=True,
    )
    try:
        watcher.validate()
    except ValueError as error:
        parser.error(str(error))

    print("TH99 live usage")
    print(
        "Mode: "
        + ("LIVE TFT upload" if args.execute_upload else "PREVIEW ONLY; no HID handle")
    )

    if not args.watch:
        _, errors = watcher.run_cycle()
        return 0 if not errors else 1
    return watcher.run_forever()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Stopped.")
        raise SystemExit(130)
