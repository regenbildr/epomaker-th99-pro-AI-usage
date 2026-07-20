"""Synchronize the TH99 Pro's native-screen RTC with the Windows clock.

This is the one-report ``AA 34`` command captured from Epomaker's official
"Time Correction" action. It uses the MI_02 configuration interface, never
the MI_03 TFT interface, so it does not upload an image or write keymaps.
"""

from __future__ import annotations

from datetime import datetime

from th99_hid_transport import (
    WINDOWS_REPORT_SIZE,
    enumerate_hid_paths,
    normalize_input_report,
    open_hid,
    overlapped_io,
    th99_config_paths,
)


COMMAND = 0x34
REPORT_SIZE = 64
DEFAULT_TIMEOUT_MS = 5_000
MARKER = b"\x5a\x01\x5a"


def build_set_clock_packet(when: datetime) -> bytes:
    """Build the captured 64-byte set-clock request for local ``when``.

    The keyboard firmware expects plain-binary fields and an ISO weekday; it
    performs the BCD conversion needed by its RTC internally.
    """
    if not 2000 <= when.year <= 2099:
        raise ValueError("TH99 clock year must be between 2000 and 2099")
    packet = bytearray(REPORT_SIZE)
    packet[:8] = bytes((0xAA, COMMAND, 56, 0, 0, 0, 1, 0))
    packet[8:18] = MARKER + bytes(
        (
            when.year - 2000,
            when.month,
            when.day,
            when.hour,
            when.minute,
            when.second,
            when.isoweekday(),
        )
    )
    validate_set_clock_packet(bytes(packet), prefix=0xAA)
    return bytes(packet)


def validate_set_clock_packet(packet: bytes, *, prefix: int) -> None:
    """Validate the narrowly-scoped, observed ``AA/55 34`` clock packet."""
    if len(packet) != REPORT_SIZE:
        raise ValueError(f"clock packet must be {REPORT_SIZE} bytes")
    expected_header = bytes((prefix, COMMAND, 56, 0, 0, 0, 1, 0))
    if packet[:8] != expected_header:
        raise ValueError("clock packet has an unexpected header")
    if packet[8:11] != MARKER:
        raise ValueError("clock packet has an unexpected marker")
    if any(packet[18:]):
        raise ValueError("clock packet has nonzero padding")

    year, month, day, hour, minute, second, weekday = packet[11:18]
    if year > 99:
        raise ValueError("clock packet year is outside 2000 through 2099")
    try:
        actual_weekday = datetime(
            2000 + year, month, day, hour, minute, second
        ).isoweekday()
    except ValueError as error:
        raise ValueError("clock packet contains an invalid date or time") from error
    if weekday != actual_weekday:
        raise ValueError("clock packet weekday does not match its date")


def sync_keyboard_clock(
    *, now: datetime | None = None, timeout_ms: int = DEFAULT_TIMEOUT_MS
) -> datetime:
    """Set the keyboard RTC to the local computer clock and verify its ACK."""
    if timeout_ms <= 0:
        raise ValueError("clock-sync timeout must be positive")
    when = (now or datetime.now()).replace(microsecond=0)
    request = build_set_clock_packet(when)
    candidates = th99_config_paths(enumerate_hid_paths())
    if len(candidates) != 1:
        raise RuntimeError(
            f"expected exactly one wired TH99 Pro MI_02 interface, found {len(candidates)}"
        )

    kernel32, handle = open_hid(candidates[0])
    try:
        overlapped_io(kernel32, handle, b"\x00" + request, 0, timeout_ms)
        response = normalize_input_report(
            overlapped_io(kernel32, handle, None, WINDOWS_REPORT_SIZE, timeout_ms)
        )
    finally:
        kernel32.CloseHandle(handle)

    validate_set_clock_packet(response, prefix=0x55)
    expected = b"\x55" + request[1:]
    if response != expected:
        raise ValueError(
            f"unexpected clock-sync acknowledgement: {response[:18].hex(' ')}"
        )
    return when