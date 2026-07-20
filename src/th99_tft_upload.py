"""Dry-run-first TH99 Pro TFT uploader for the existing MI_03 firmware path.

Default mode constructs a two-frame black/white protocol-test container in
memory and validates all AA 50 reports without opening a HID handle.  Live mode
is separately gated, pinned to the reviewed USB capture and known payload
hashes, and accepts only the constant acknowledgement observed for every packet
in the official web-driver upload.

This utility does not contain reset, firmware, keymap, macro, or lighting
commands.  The keyboard offers no confirmed TFT read-back command, so a live
upload can be protocol-verified by acknowledgements but must be visually
confirmed by the user.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys

from th99_tft_hid_transport import (
    WINDOWS_REPORT_SIZE,
    WINDOWS_TFT_OUTPUT_SIZE,
    enumerate_hid_paths,
    normalize_input_report,
    open_hid,
    overlapped_io,
    th99_display_paths,
)
from th99_tft_protocol import (
    EXPECTED_ACK,
    REPORT_SIZE,
    build_reports,
    capture_upload,
    inspect_payload,
    make_test_payload,
    sha256,
)


KNOWN_CAPTURE_FILE_HASH = (
    "c03a85db089fb3efb209aaa970b031441653e26ddd58f1511a6a0eb017d06630"
)
KNOWN_CAPTURE_PAYLOAD_HASH = (
    "22f11ef5647622eb08284a02f3ab513c5a347a6ea2b0a32f94c8cca658192bbc"
)
KNOWN_CAPTURE_REPORT_STREAM_HASH = (
    "7ee97b72e809ad4297c3d84d486c17c167544ec759103f5267f1fecdccbd6d4f"
)
KNOWN_TEST_PAYLOAD_HASH = (
    "011c095ca758331d268314eedfbdd65f90f6c1c390a3686fca232213598f53fb"
)
KNOWN_TEST_REPORT_STREAM_HASH = (
    "21dd1fa93fda1412fe33c70fca86a3ac5914e8d961875a3dcaf7ef804975f093"
)
ACKNOWLEDGEMENTS = {
    "test": "UPLOAD_AA50_TWO_FRAME_TEST_V2",
    "captured": "REPLAY_AA50_CAPTURED_GIF_V2",
}
TIMEOUT_MIN = 500
TIMEOUT_MAX = 30000


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def select_payload(mode: str, captured_payload: bytes) -> bytes:
    if mode == "captured":
        return captured_payload
    if mode == "test":
        return make_test_payload(captured_payload)
    raise ValueError(f"unsupported mode {mode}")


def expected_hashes(mode: str) -> tuple[str, str]:
    if mode == "captured":
        return KNOWN_CAPTURE_PAYLOAD_HASH, KNOWN_CAPTURE_REPORT_STREAM_HASH
    if mode == "test":
        return KNOWN_TEST_PAYLOAD_HASH, KNOWN_TEST_REPORT_STREAM_HASH
    raise ValueError(f"unsupported mode {mode}")


def enforce_known_inputs(
    capture: Path,
    mode: str,
    payload: bytes,
    reports: list[bytes],
) -> None:
    if file_sha256(capture) != KNOWN_CAPTURE_FILE_HASH:
        raise ValueError("live mode is pinned to the reviewed TFT capture file")
    expected_payload, expected_stream = expected_hashes(mode)
    if sha256(payload) != expected_payload:
        raise ValueError(f"{mode} TFT payload hash is not approved")
    if sha256(b"".join(reports)) != expected_stream:
        raise ValueError(f"{mode} AA 50 report-stream hash is not approved")


def print_dry_run(
    capture: Path,
    mode: str,
    captured_reports: list[bytes],
    payload: bytes,
    reports: list[bytes],
    candidates: list[str],
) -> None:
    metadata = inspect_payload(payload)
    print("TH99 Pro TFT uploader v2 - DRY RUN")
    print("No HID handle was opened and no report was sent.\n")
    print(f"Reviewed capture: {capture.resolve()}")
    print(f"Capture SHA-256: {file_sha256(capture)}")
    print(f"Mode: {mode}")
    print("Target: VID 0C45 / PID 800A / MI_03 only")
    if candidates:
        for index, path in enumerate(candidates):
            print(f"Detected candidate [{index}]: {path}")
    else:
        print("Detected candidate: none (offline validation still completed)")
    print()
    print(
        f"Captured upload: {len(captured_reports)} reports; exact regeneration "
        "validated"
    )
    print(
        f"Selected payload: {metadata['size']} bytes; "
        f"SHA-256={metadata['sha256']}"
    )
    print(
        f"Frames: {metadata['frame_count']} at 160x96 RGB565 LE; "
        f"unique={metadata['unique_frames']}; padding={metadata['padding_size']}"
    )
    print(
        f"AA 50 reports: {len(reports)} x {REPORT_SIZE} bytes; "
        f"stream SHA-256={sha256(b''.join(reports))}"
    )
    print(f"First header: {reports[0][:8].hex(' ')}")
    print(f"Last header:  {reports[-1][:8].hex(' ')}")
    print(f"Required acknowledgement per report: {EXPECTED_ACK[:4].hex(' ')} + zeros")
    if mode == "test":
        print()
        print("TEST HYPOTHESIS: the captured preamble is reusable when its frame-count")
        print("byte is changed from 180 to 2. A live result requires visual confirmation.")
        print("Expected visual: alternating solid black and solid white frames.")
    print()
    print("Only AA 50 display reports are constructed. No reset or keymap command exists here.")


def send_upload(kernel32, handle, reports: list[bytes], timeout_ms: int) -> None:
    for sequence, report in enumerate(reports):
        if len(report) != REPORT_SIZE or report[:2] != b"\xAA\x50":
            raise ValueError(f"report {sequence} failed the AA 50 live whitelist")
        windows_report = b"\x00" + report
        if len(windows_report) != WINDOWS_TFT_OUTPUT_SIZE:
            raise ValueError("unexpected Windows TFT output-report size")
        overlapped_io(kernel32, handle, windows_report, 0, timeout_ms)
        response = normalize_input_report(
            overlapped_io(
                kernel32,
                handle,
                None,
                WINDOWS_REPORT_SIZE,
                timeout_ms,
            )
        )
        if response != EXPECTED_ACK:
            raise ValueError(
                f"unexpected TFT acknowledgement after sequence {sequence}: "
                f"{response[:16].hex(' ')}"
            )
        if (sequence + 1) % 50 == 0 or sequence + 1 == len(reports):
            print(f"Acknowledged {sequence + 1}/{len(reports)} reports", flush=True)


CAPTURES = Path(__file__).resolve().parent.parent / "data" / "captures"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--capture",
        type=Path,
        default=CAPTURES / "th99-upload.pcap",
    )
    parser.add_argument(
        "--mode",
        choices=("test", "captured"),
        default="test",
        help="test: two solid frames; captured: exact original 180-frame upload",
    )
    parser.add_argument(
        "--export-payload",
        type=Path,
        help="write the selected container to a file without uploading it",
    )
    parser.add_argument(
        "--execute-upload",
        action="store_true",
        help="open MI_03 and send the selected AA 50 upload",
    )
    parser.add_argument(
        "--acknowledge",
        default="",
        help="live mode requires the exact mode-specific token printed in dry run/help",
    )
    parser.add_argument("--timeout-ms", type=int, default=5000)
    args = parser.parse_args()

    captured_reports, _, captured_payload = capture_upload(args.capture)
    if build_reports(captured_payload) != captured_reports:
        raise ValueError("captured AA 50 upload failed exact regeneration")
    payload = select_payload(args.mode, captured_payload)
    reports = build_reports(payload)
    expected_payload, expected_stream = expected_hashes(args.mode)
    if sha256(payload) != expected_payload:
        raise ValueError(f"unexpected {args.mode} payload hash")
    if sha256(b"".join(reports)) != expected_stream:
        raise ValueError(f"unexpected {args.mode} report-stream hash")

    candidates = th99_display_paths(enumerate_hid_paths())

    if args.export_payload is not None:
        if args.export_payload.exists():
            parser.error(f"refusing to overwrite existing file: {args.export_payload}")
        args.export_payload.write_bytes(payload)
        print(f"Payload written to {args.export_payload.resolve()}")

    if not args.execute_upload:
        print_dry_run(
            args.capture,
            args.mode,
            captured_reports,
            payload,
            reports,
            candidates,
        )
        return 0

    required_acknowledgement = ACKNOWLEDGEMENTS[args.mode]
    if args.acknowledge != required_acknowledgement:
        parser.error(
            f"live {args.mode} mode requires --acknowledge "
            f"{required_acknowledgement}"
        )
    if not TIMEOUT_MIN <= args.timeout_ms <= TIMEOUT_MAX:
        parser.error(
            f"--timeout-ms must be between {TIMEOUT_MIN} and {TIMEOUT_MAX}"
        )
    if len(candidates) != 1:
        parser.error(
            f"expected exactly one TH99 Pro MI_03 interface, found {len(candidates)}"
        )
    enforce_known_inputs(args.capture, args.mode, payload, reports)

    kernel32, handle = open_hid(candidates[0])
    try:
        send_upload(kernel32, handle, reports, args.timeout_ms)
    finally:
        kernel32.CloseHandle(handle)

    print(
        f"Upload transport completed: {len(reports)} AA 50 reports received "
        "the confirmed acknowledgement."
    )
    print("Inspect the keyboard screen to confirm the visual result.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        raise SystemExit(130)
