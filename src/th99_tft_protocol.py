"""Offline protocol helpers for TH99 Pro MI_03 TFT uploads."""

from __future__ import annotations

import hashlib
from pathlib import Path

from th99_keymap_protocol import parse_th99_capture


COMMAND = 0x50
REPORT_HEADER_SIZE = 8
BLOCK_SIZE = 4096
REPORT_SIZE = REPORT_HEADER_SIZE + BLOCK_SIZE
TRANSFER_CONSTANT = 0x0650
FRAME_WIDTH = 160
FRAME_HEIGHT = 96
BYTES_PER_PIXEL = 2
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT * BYTES_PER_PIXEL
PREAMBLE_SIZE = BLOCK_SIZE
EXPECTED_ACK = b"\x55\x41\x00\x01" + bytes(60)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def capture_upload(path: Path) -> tuple[list[bytes], list[bytes], bytes]:
    records = parse_th99_capture(path)
    reports = [
        record.data
        for record in records
        if record.direction == 0
        and record.endpoint == 0x06
        and record.transfer == 1
        and len(record.data) == REPORT_SIZE
        and record.data[:2] == b"\xAA\x50"
    ]
    acknowledgements = [
        record.data
        for record in records
        if record.direction == 1
        and record.endpoint == 0x85
        and record.transfer == 1
        and record.data
    ]
    if not reports:
        raise ValueError(f"{path}: no TH99 MI_03 AA 50 upload reports found")
    validate_reports(reports)
    if len(acknowledgements) != len(reports):
        raise ValueError(
            f"capture has {len(reports)} reports but "
            f"{len(acknowledgements)} acknowledgements"
        )
    if any(ack != EXPECTED_ACK for ack in acknowledgements):
        raise ValueError("capture contains an unexpected TFT acknowledgement")
    payload = b"".join(report[REPORT_HEADER_SIZE:] for report in reports)
    return reports, acknowledgements, payload


def validate_reports(reports: list[bytes]) -> None:
    if not reports:
        raise ValueError("TFT report list is empty")
    count = len(reports)
    for sequence, report in enumerate(reports):
        if len(report) != REPORT_SIZE:
            raise ValueError(
                f"TFT report {sequence} is {len(report)} bytes, expected {REPORT_SIZE}"
            )
        if report[:2] != b"\xAA\x50":
            raise ValueError(f"TFT report {sequence} is not AA 50")
        if int.from_bytes(report[2:4], "little") != sequence:
            raise ValueError(f"TFT report sequence mismatch at {sequence}")
        if int.from_bytes(report[4:6], "little") != count:
            raise ValueError(f"TFT report count mismatch at {sequence}")
        if int.from_bytes(report[6:8], "little") != TRANSFER_CONSTANT:
            raise ValueError(f"TFT transfer constant mismatch at {sequence}")


def build_reports(payload: bytes) -> list[bytes]:
    if not payload or len(payload) % BLOCK_SIZE:
        raise ValueError("TFT payload must be a nonempty multiple of 4096 bytes")
    report_count = len(payload) // BLOCK_SIZE
    if report_count > 0xFFFF:
        raise ValueError("TFT report count exceeds 65535")
    reports = []
    for sequence in range(report_count):
        start = sequence * BLOCK_SIZE
        header = (
            b"\xAA\x50"
            + sequence.to_bytes(2, "little")
            + report_count.to_bytes(2, "little")
            + TRANSFER_CONSTANT.to_bytes(2, "little")
        )
        reports.append(header + payload[start : start + BLOCK_SIZE])
    validate_reports(reports)
    return reports


def pad_to_block(data: bytes) -> bytes:
    remainder = len(data) % BLOCK_SIZE
    return data if remainder == 0 else data + bytes(BLOCK_SIZE - remainder)


def solid_rgb565_frame(color: int) -> bytes:
    if not 0 <= color <= 0xFFFF:
        raise ValueError("RGB565 color must fit in 16 bits")
    return color.to_bytes(2, "little") * (FRAME_WIDTH * FRAME_HEIGHT)


def make_test_payload(captured_payload: bytes) -> bytes:
    """Build a two-frame black/white test using the captured preamble.

    Only the preamble's frame-count byte is changed.  The remaining preamble,
    including the observed timing-like value 0x0F, is preserved verbatim.
    """
    if len(captured_payload) < PREAMBLE_SIZE:
        raise ValueError("captured payload does not contain a full preamble")
    preamble = bytearray(captured_payload[:PREAMBLE_SIZE])
    if preamble[0] != 180:
        raise ValueError(
            f"reviewed preamble should declare 180 frames, got {preamble[0]}"
        )
    preamble[0] = 2
    frames = solid_rgb565_frame(0x0000) + solid_rgb565_frame(0xFFFF)
    return pad_to_block(bytes(preamble) + frames)


def inspect_payload(payload: bytes) -> dict:
    if len(payload) < PREAMBLE_SIZE or len(payload) % BLOCK_SIZE:
        raise ValueError("invalid TFT payload size")
    frame_count = payload[0]
    expected_unpadded = PREAMBLE_SIZE + frame_count * FRAME_BYTES
    if expected_unpadded > len(payload):
        raise ValueError("declared TFT frames exceed payload size")
    padding = payload[expected_unpadded:]
    if len(padding) >= BLOCK_SIZE or any(padding):
        raise ValueError("TFT payload has invalid trailing block padding")
    body = payload[PREAMBLE_SIZE:expected_unpadded]
    frames = [
        body[index * FRAME_BYTES : (index + 1) * FRAME_BYTES]
        for index in range(frame_count)
    ]
    return {
        "size": len(payload),
        "sha256": sha256(payload),
        "report_count": len(payload) // BLOCK_SIZE,
        "frame_count": frame_count,
        "frame_size": FRAME_BYTES,
        "unique_frames": len({sha256(frame) for frame in frames}),
        "padding_size": len(padding),
        "preamble_sha256": sha256(payload[:PREAMBLE_SIZE]),
        "frame_hashes": [sha256(frame) for frame in frames],
    }
