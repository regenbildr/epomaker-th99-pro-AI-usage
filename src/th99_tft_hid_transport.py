"""Windows HID transport selection for the TH99 Pro MI_03 TFT interface."""

from __future__ import annotations

from th99_hid_transport import (
    PID,
    VID,
    WINDOWS_REPORT_SIZE,
    enumerate_hid_paths,
    normalize_input_report,
    open_hid,
    overlapped_io,
)


DISPLAY_INTERFACE_MARKER = "mi_03"
WINDOWS_TFT_OUTPUT_SIZE = 4105  # report ID 0 + 4104-byte AA50 report


def th99_display_paths(paths: list[str]) -> list[str]:
    vid_pid = f"vid_{VID:04x}&pid_{PID:04x}"
    return [
        path
        for path in paths
        if vid_pid in path.lower()
        and DISPLAY_INTERFACE_MARKER in path.lower()
    ]


__all__ = [
    "WINDOWS_REPORT_SIZE",
    "WINDOWS_TFT_OUTPUT_SIZE",
    "enumerate_hid_paths",
    "normalize_input_report",
    "open_hid",
    "overlapped_io",
    "th99_display_paths",
]
