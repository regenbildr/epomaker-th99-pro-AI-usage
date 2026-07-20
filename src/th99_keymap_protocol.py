"""Corrected TH99 Pro keymap protocol helpers (8-byte packet header).

This module performs only offline parsing and packet construction.  It never
opens a HID device.  Keymap packets are 64 bytes and use this layout:

    0       0xAA request / 0x55 response
    1       command
    2       payload length (maximum 56)
    3..5    24-bit little-endian table offset
    6       final-packet flag
    7       reserved (zero)
    8..63   payload and zero padding
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import struct


VID = 0x0C45
PID = 0x800A
REPORT_SIZE = 64
HEADER_SIZE = 8
PAYLOAD_SIZE = REPORT_SIZE - HEADER_SIZE
TABLE_SIZE = 512
RECORD_SIZE = 4
RECORD_COUNT = TABLE_SIZE // RECORD_SIZE
READ_TO_WRITE = {0x12: 0x22, 0x16: 0x26}
TABLE_NAMES = {0x12: "basic_keymap", 0x16: "fn_keymap"}

PCAP_RECORD_HEADER = struct.Struct("<IIII")
USBPCAP_HEADER = struct.Struct("<HQIHBHHBBI")
TH99_DESCRIPTOR = bytes(
    (0x12, 0x01, 0x00, 0x02, 0, 0, 0, 0x40, 0x45, 0x0C, 0x0A, 0x80)
)


@dataclass(frozen=True)
class USBRecord:
    direction: int
    bus: int
    device: int
    endpoint: int
    transfer: int
    data: bytes


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_th99_capture(path: Path) -> list[USBRecord]:
    """Return only records belonging to the captured TH99 Pro device."""
    with path.open("rb") as capture:
        if capture.read(4) != b"\xd4\xc3\xb2\xa1":
            raise ValueError(f"{path}: expected a little-endian classic PCAP")
        capture.seek(24)
        records: list[USBRecord] = []
        while header := capture.read(PCAP_RECORD_HEADER.size):
            if len(header) != PCAP_RECORD_HEADER.size:
                raise ValueError(f"{path}: truncated PCAP record header")
            _, _, captured_size, _ = PCAP_RECORD_HEADER.unpack(header)
            raw = capture.read(captured_size)
            if len(raw) != captured_size:
                raise ValueError(f"{path}: truncated PCAP record")
            if len(raw) < USBPCAP_HEADER.size:
                continue
            (
                header_size,
                _,
                _,
                _,
                direction,
                bus,
                device,
                endpoint,
                transfer,
                data_size,
            ) = USBPCAP_HEADER.unpack_from(raw)
            if header_size + data_size > len(raw):
                continue
            records.append(
                USBRecord(
                    direction,
                    bus,
                    device,
                    endpoint,
                    transfer,
                    raw[header_size : header_size + data_size],
                )
            )

    address = next(
        (
            (record.bus, record.device)
            for record in records
            if record.transfer == 2
            and record.data.startswith(TH99_DESCRIPTOR)
        ),
        None,
    )
    if address is None:
        raise ValueError(f"{path}: TH99 Pro VID 0C45 / PID 800A not found")
    return [
        record
        for record in records
        if (record.bus, record.device) == address
    ]


def validate_packet(
    packet: bytes,
    *,
    prefix: int,
    command: int,
) -> tuple[int, int, int]:
    if len(packet) != REPORT_SIZE:
        raise ValueError(f"expected a 64-byte packet, got {len(packet)}")
    if packet[0] != prefix or packet[1] != command:
        raise ValueError(
            f"expected {prefix:02X} {command:02X}, got {packet[:2].hex(' ')}"
        )
    length = packet[2]
    offset = int.from_bytes(packet[3:6], "little")
    final = packet[6]
    if length > PAYLOAD_SIZE:
        raise ValueError(f"payload length {length} exceeds {PAYLOAD_SIZE}")
    if offset + length > TABLE_SIZE:
        raise ValueError("packet payload exceeds the 512-byte table")
    if final not in (0, 1):
        raise ValueError(f"invalid final flag {final}")
    if packet[7] != 0:
        raise ValueError(f"reserved header byte is {packet[7]:02X}, expected 00")
    if any(packet[HEADER_SIZE + length :]):
        raise ValueError("nonzero bytes found after the declared payload")
    return length, offset, final


def split_batches(packets: list[bytes]) -> list[list[bytes]]:
    batches: list[list[bytes]] = []
    current: list[bytes] = []
    for packet in packets:
        offset = int.from_bytes(packet[3:6], "little")
        if offset == 0 and current:
            batches.append(current)
            current = []
        current.append(packet)
    if current:
        batches.append(current)
    return batches


def packets_from_capture(
    path: Path,
    *,
    prefix: int,
    command: int,
    direction: int,
    endpoint: int,
) -> list[bytes]:
    packets = [
        record.data
        for record in parse_th99_capture(path)
        if record.direction == direction
        and record.endpoint == endpoint
        and len(record.data) >= 2
        and record.data[0] == prefix
        and record.data[1] == command
    ]
    for packet in packets:
        validate_packet(packet, prefix=prefix, command=command)
    return packets


def rebuild_table(
    packets: list[bytes],
    *,
    prefix: int,
    command: int,
) -> bytes:
    expected_offsets = list(range(0, TABLE_SIZE, PAYLOAD_SIZE))
    offsets: list[int] = []
    table = bytearray(TABLE_SIZE)
    covered = bytearray(TABLE_SIZE)
    for packet in packets:
        length, offset, final = validate_packet(
            packet, prefix=prefix, command=command
        )
        offsets.append(offset)
        should_be_final = offset + length == TABLE_SIZE
        if final != int(should_be_final):
            raise ValueError(
                f"incorrect final flag at table offset {offset}: {final}"
            )
        table[offset : offset + length] = packet[
            HEADER_SIZE : HEADER_SIZE + length
        ]
        covered[offset : offset + length] = b"\x01" * length
    if offsets != expected_offsets:
        raise ValueError(
            f"expected table offsets {expected_offsets}, observed {offsets}"
        )
    if not all(covered):
        raise ValueError("packet batch does not cover the complete table")
    return bytes(table)


def complete_batches_from_capture(
    path: Path,
    *,
    prefix: int,
    command: int,
    direction: int,
    endpoint: int,
) -> list[tuple[list[bytes], bytes]]:
    packets = packets_from_capture(
        path,
        prefix=prefix,
        command=command,
        direction=direction,
        endpoint=endpoint,
    )
    result = []
    for batch in split_batches(packets):
        table = rebuild_table(batch, prefix=prefix, command=command)
        result.append((batch, table))
    return result


def build_table_packets(prefix: int, command: int, table: bytes) -> list[bytes]:
    if len(table) != TABLE_SIZE:
        raise ValueError("keymap table must be exactly 512 bytes")
    packets = []
    for offset in range(0, TABLE_SIZE, PAYLOAD_SIZE):
        length = min(PAYLOAD_SIZE, TABLE_SIZE - offset)
        packet = bytearray(REPORT_SIZE)
        packet[0] = prefix
        packet[1] = command
        packet[2] = length
        packet[3:6] = offset.to_bytes(3, "little")
        packet[6] = int(offset + length == TABLE_SIZE)
        packet[7] = 0
        packet[HEADER_SIZE : HEADER_SIZE + length] = table[
            offset : offset + length
        ]
        packets.append(bytes(packet))
    return packets


def record(table: bytes, index: int) -> bytes:
    if len(table) != TABLE_SIZE:
        raise ValueError("keymap table must be exactly 512 bytes")
    if not 0 <= index < RECORD_COUNT:
        raise IndexError(index)
    start = index * RECORD_SIZE
    return table[start : start + RECORD_SIZE]


def nonzero_record_indices(table: bytes) -> list[int]:
    return [
        index
        for index in range(RECORD_COUNT)
        if record(table, index) != b"\x00" * RECORD_SIZE
    ]


def differing_record_indices(left: bytes, right: bytes) -> list[int]:
    return [
        index
        for index in range(RECORD_COUNT)
        if record(left, index) != record(right, index)
    ]
