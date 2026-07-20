"""Restore the captured TH99 Pro keymaps without performing a factory reset.

The default mode is an offline/dry-run validation.  It extracts the final
accepted AA 22 and AA 26 transactions from ``th99-key-remap.pcap``, validates
them against the corrected AA 12 and AA 16 read-back capture, and proves that
rebuilding the packets produces a byte-for-byte match.

Live mode is deliberately gated by an exact acknowledgement token.  It opens
only VID 0C45 / PID 800A / MI_02, reads the current maps first, sends only the
captured AA 22 and/or AA 26 transaction that is needed, and verifies the full
512-byte maps with AA 12 and AA 16 reads afterward.  It never sends reset,
firmware, lighting, macro, or TFT commands.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from th99_hid_transport import (
    WINDOWS_REPORT_SIZE,
    enumerate_hid_paths,
    normalize_input_report,
    open_hid,
    overlapped_io,
    th99_config_paths,
)
from th99_keymap_protocol import (
    READ_TO_WRITE,
    TABLE_NAMES,
    TABLE_SIZE,
    build_table_packets,
    complete_batches_from_capture,
    differing_record_indices,
    nonzero_record_indices,
    record,
    sha256,
    validate_packet,
)


ACKNOWLEDGEMENT = "RESTORE_CAPTURED_AA22_AA26_V2"
TIMEOUT_MIN = 500
TIMEOUT_MAX = 30000

# These hashes pin live mode to the two captures that were inspected in this
# project.  A different capture remains usable for offline analysis, but cannot
# be sent by this version without a conscious source-code review/update.
KNOWN_CAPTURE_HASHES = {
    "restore": "fb636a9e9af3c9e91f5bc6f0a70984d71fa0d9b1d4888a8a502f47b35846e78f",
    "verification": "e4b17ab7dbad48507e24e901763f0f1d58c231139e56b918b00618fd98d1c006",
}
KNOWN_WRITE_TABLE_HASHES = {
    0x22: "44cfb3b35023ffa72f29c0c4709ab68b323ce8d38dd212918a44be14ead6fa73",
    0x26: "2c8a1a4bcfb7d44c5b40325ebae2359738bdc47ec3d6904661c5946af0c78450",
}
KNOWN_TARGET_TABLE_HASHES = {
    0x12: "20daf180476806842d0160177bcc17ca679a43fedcdba68de8fcad16c408b8be",
    0x16: "8be3cae99c8558118b5e010a93b933d08be11155ca4fa202a1d9933b99442c57",
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def final_write_transactions(path: Path) -> dict[int, tuple[list[bytes], bytes]]:
    result = {}
    for command in READ_TO_WRITE.values():
        batches = complete_batches_from_capture(
            path,
            prefix=0xAA,
            command=command,
            direction=0,
            endpoint=0x03,
        )
        if not batches:
            raise ValueError(f"no complete AA {command:02X} write found in {path}")
        packets, table = batches[-1]
        rebuilt = build_table_packets(0xAA, command, table)
        if rebuilt != packets:
            raise ValueError(
                f"rebuilt AA {command:02X} packets do not exactly match the capture"
            )
        result[command] = (packets, table)
    return result


def final_read_tables(path: Path) -> dict[int, bytes]:
    result = {}
    for command in READ_TO_WRITE:
        batches = complete_batches_from_capture(
            path,
            prefix=0x55,
            command=command,
            direction=1,
            endpoint=0x84,
        )
        if not batches:
            raise ValueError(f"no complete 55 {command:02X} read found in {path}")
        _, table = batches[-1]
        result[command] = table
    return result


def validate_target(
    writes: dict[int, tuple[list[bytes], bytes]],
    targets: dict[int, bytes],
) -> None:
    for read_command, write_command in READ_TO_WRITE.items():
        _, write_table = writes[write_command]
        target = targets[read_command]
        for index in nonzero_record_indices(write_table):
            if record(write_table, index) != record(target, index):
                raise ValueError(
                    f"AA {write_command:02X} record {index} does not match "
                    f"the AA {read_command:02X} verification capture"
                )


def load_defaults(path: Path) -> dict[int, bytes]:
    result = {}
    for command in READ_TO_WRITE:
        batches = complete_batches_from_capture(
            path,
            prefix=0x55,
            command=command,
            direction=1,
            endpoint=0x84,
        )
        if not batches:
            raise ValueError(f"no complete 55 {command:02X} read found in {path}")
        # The first post-reset cycle is the factory/default table.
        result[command] = batches[0][1]
    return result


def make_target_document(
    source_capture: Path,
    verification_capture: Path,
    writes: dict[int, tuple[list[bytes], bytes]],
    targets: dict[int, bytes],
) -> dict:
    return {
        "format": "th99-pro-keymap-target-v2",
        "packet_header_size": 8,
        "device": {"vid": "0x0C45", "pid": "0x800A", "interface": "MI_02"},
        "source_capture": {
            "path": str(source_capture.resolve()),
            "sha256": file_sha256(source_capture),
        },
        "verification_capture": {
            "path": str(verification_capture.resolve()),
            "sha256": file_sha256(verification_capture),
        },
        "layers": [
            {
                "name": TABLE_NAMES[read_command],
                "read_command": f"0x{read_command:02X}",
                "write_command": f"0x{write_command:02X}",
                "write_table_sha256": sha256(writes[write_command][1]),
                "target_table_sha256": sha256(targets[read_command]),
                "target_table_hex": targets[read_command].hex(),
                "write_packets_hex": [
                    packet.hex() for packet in writes[write_command][0]
                ],
            }
            for read_command, write_command in READ_TO_WRITE.items()
        ],
    }


def print_dry_run(
    source_capture: Path,
    verification_capture: Path,
    writes: dict[int, tuple[list[bytes], bytes]],
    targets: dict[int, bytes],
    defaults: dict[int, bytes] | None,
    candidates: list[str],
) -> None:
    print("TH99 Pro captured keymap restore v2 - DRY RUN")
    print("No HID handle was opened and no report was sent.\n")
    print(f"Restore capture:      {source_capture.resolve()}")
    print(f"  SHA-256: {file_sha256(source_capture)}")
    print(f"Verification capture: {verification_capture.resolve()}")
    print(f"  SHA-256: {file_sha256(verification_capture)}")
    print("Packet format: 8-byte header + up to 56 payload bytes")
    print("Target device: VID 0C45 / PID 800A / MI_02 only")
    if candidates:
        for index, path in enumerate(candidates):
            print(f"Detected candidate [{index}]: {path}")
    else:
        print("Detected candidate: none (offline validation still completed)")
    print()

    for read_command, write_command in READ_TO_WRITE.items():
        packets, write_table = writes[write_command]
        target = targets[read_command]
        changed = (
            differing_record_indices(defaults[read_command], target)
            if defaults is not None
            else []
        )
        print(
            f"{TABLE_NAMES[read_command]}: captured AA {write_command:02X}; "
            f"{len(packets)} reports; {len(nonzero_record_indices(write_table))} "
            "serialized records"
        )
        print(f"  write-table SHA-256:  {sha256(write_table)}")
        print(f"  target-read SHA-256:  {sha256(target)}")
        print("  regenerated reports: exact byte-for-byte capture match")
        if defaults is not None:
            print(f"  target differs from factory at {len(changed)} records:")
            for index in changed:
                print(
                    f"    {index:03d}: {record(defaults[read_command], index).hex()}"
                    f" -> {record(target, index).hex()}"
                )
        print()
    print("Live whitelist: AA 12 and AA 16 reads; AA 22 and AA 26 writes only.")
    print("Explicitly absent: reset (AA 0F), TFT (AA 50), firmware, macros, and lighting.")


def normalize_and_validate_response(raw: bytes, command: int) -> bytes:
    response = normalize_input_report(raw)
    validate_packet(response, prefix=0x55, command=command)
    return response


def read_live_table(kernel32, handle, command: int, timeout_ms: int) -> bytes:
    if command not in READ_TO_WRITE:
        raise ValueError(f"live read command AA {command:02X} is not whitelisted")
    requests = build_table_packets(0xAA, command, bytes(TABLE_SIZE))
    responses = []
    for request in requests:
        overlapped_io(kernel32, handle, b"\x00" + request, 0, timeout_ms)
        response = normalize_and_validate_response(
            overlapped_io(
                kernel32,
                handle,
                None,
                WINDOWS_REPORT_SIZE,
                timeout_ms,
            ),
            command,
        )
        if response[2:8] != request[2:8]:
            raise ValueError(
                f"AA {command:02X} response header does not match its request"
            )
        responses.append(response)

    # Import locally to make the permitted live-operation boundary obvious.
    from th99_keymap_protocol import rebuild_table

    return rebuild_table(responses, prefix=0x55, command=command)


def send_write_transaction(
    kernel32,
    handle,
    command: int,
    packets: list[bytes],
    timeout_ms: int,
) -> None:
    if command not in READ_TO_WRITE.values():
        raise ValueError(f"live write command AA {command:02X} is not whitelisted")
    for packet in packets:
        validate_packet(packet, prefix=0xAA, command=command)
        overlapped_io(kernel32, handle, b"\x00" + packet, 0, timeout_ms)
        response = normalize_input_report(
            overlapped_io(
                kernel32,
                handle,
                None,
                WINDOWS_REPORT_SIZE,
                timeout_ms,
            )
        )
        expected = b"\x55" + packet[1:]
        if response != expected:
            raise ValueError(
                f"unexpected AA {command:02X} acknowledgement: "
                f"{response[:16].hex(' ')}"
            )


def enforce_known_live_inputs(
    source_capture: Path,
    verification_capture: Path,
    writes: dict[int, tuple[list[bytes], bytes]],
    targets: dict[int, bytes],
) -> None:
    observed_capture_hashes = {
        "restore": file_sha256(source_capture),
        "verification": file_sha256(verification_capture),
    }
    if observed_capture_hashes != KNOWN_CAPTURE_HASHES:
        raise ValueError(
            "live mode is pinned to the two reviewed capture files; one or both "
            "capture hashes differ"
        )
    for command, expected in KNOWN_WRITE_TABLE_HASHES.items():
        actual = sha256(writes[command][1])
        if actual != expected:
            raise ValueError(f"AA {command:02X} write-table hash is not approved")
    for command, expected in KNOWN_TARGET_TABLE_HASHES.items():
        actual = sha256(targets[command])
        if actual != expected:
            raise ValueError(f"AA {command:02X} target-table hash is not approved")


CAPTURES = Path(__file__).resolve().parent.parent / "data" / "captures"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--restore-capture",
        type=Path,
        default=CAPTURES / "th99-key-remap.pcap",
    )
    parser.add_argument(
        "--verification-capture",
        type=Path,
        default=CAPTURES / "th99-startup-remapped.pcap",
    )
    parser.add_argument(
        "--defaults-capture",
        type=Path,
        default=CAPTURES / "th99-factory-reset.pcap",
        help="used only to display the intended record changes",
    )
    parser.add_argument(
        "--export-target",
        type=Path,
        help="write a corrected v2 JSON target (offline operation)",
    )
    parser.add_argument(
        "--execute-restore",
        action="store_true",
        help="perform the gated AA 22 / AA 26 restore and read-back verification",
    )
    parser.add_argument(
        "--acknowledge",
        default="",
        help=f"required with --execute-restore; exact value: {ACKNOWLEDGEMENT}",
    )
    parser.add_argument("--timeout-ms", type=int, default=5000)
    args = parser.parse_args()

    writes = final_write_transactions(args.restore_capture)
    targets = final_read_tables(args.verification_capture)
    validate_target(writes, targets)
    defaults = (
        load_defaults(args.defaults_capture)
        if args.defaults_capture.exists()
        else None
    )
    candidates = th99_config_paths(enumerate_hid_paths())

    if args.export_target is not None:
        if args.export_target.exists():
            parser.error(f"refusing to overwrite existing file: {args.export_target}")
        document = make_target_document(
            args.restore_capture,
            args.verification_capture,
            writes,
            targets,
        )
        args.export_target.write_text(
            json.dumps(document, indent=2), encoding="utf-8"
        )
        print(f"Corrected v2 target written to {args.export_target.resolve()}")

    if not args.execute_restore:
        print_dry_run(
            args.restore_capture,
            args.verification_capture,
            writes,
            targets,
            defaults,
            candidates,
        )
        return 0

    if args.acknowledge != ACKNOWLEDGEMENT:
        parser.error(
            f"live mode requires --acknowledge {ACKNOWLEDGEMENT}"
        )
    if not TIMEOUT_MIN <= args.timeout_ms <= TIMEOUT_MAX:
        parser.error(
            f"--timeout-ms must be between {TIMEOUT_MIN} and {TIMEOUT_MAX}"
        )
    if len(candidates) != 1:
        parser.error(
            f"expected exactly one TH99 Pro MI_02 interface, found {len(candidates)}"
        )
    enforce_known_live_inputs(
        args.restore_capture,
        args.verification_capture,
        writes,
        targets,
    )

    kernel32, handle = open_hid(candidates[0])
    try:
        before = {
            command: read_live_table(kernel32, handle, command, args.timeout_ms)
            for command in READ_TO_WRITE
        }
        needed = [
            read_command
            for read_command in READ_TO_WRITE
            if before[read_command] != targets[read_command]
        ]
        for read_command in needed:
            write_command = READ_TO_WRITE[read_command]
            packets, _ = writes[write_command]
            send_write_transaction(
                kernel32,
                handle,
                write_command,
                packets,
                args.timeout_ms,
            )
        after = {
            command: read_live_table(kernel32, handle, command, args.timeout_ms)
            for command in READ_TO_WRITE
        }
    finally:
        kernel32.CloseHandle(handle)

    mismatches = [
        command
        for command in READ_TO_WRITE
        if after[command] != targets[command]
    ]
    if mismatches:
        commands = ", ".join(f"AA {command:02X}" for command in mismatches)
        raise ValueError(f"post-restore verification failed for {commands}")

    if not needed:
        print("No write was needed; both keymap layers already matched the target.")
    else:
        restored = ", ".join(TABLE_NAMES[command] for command in needed)
        print(f"Restored and verified: {restored}.")
    for command in READ_TO_WRITE:
        print(
            f"AA {command:02X} verified SHA-256 {sha256(after[command])}"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        raise SystemExit(130)
