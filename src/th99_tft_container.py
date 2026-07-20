"""Confirmed TH99 Pro TFT container format derived from official captures.

Container layout:

* bytes 0..255: metadata
* metadata[0]: frame count N
* metadata[1:N]: N-1 timing bytes
* metadata[N]: zero terminator
* metadata[N+1:256]: 0xFF fill
* N frames of 160x96 RGB565 little-endian pixels
* zero padding to a 4096-byte boundary

This module performs no HID I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from th99_tft_protocol import BLOCK_SIZE, FRAME_BYTES, pad_to_block


METADATA_SIZE = 256
WIDTH = 160
HEIGHT = 96


@dataclass(frozen=True)
class TFTContainer:
    metadata: bytes
    timings: bytes
    frames: tuple[bytes, ...]
    padding: bytes


def parse_container(payload: bytes) -> TFTContainer:
    if len(payload) < METADATA_SIZE or len(payload) % BLOCK_SIZE:
        raise ValueError("TFT payload must be a nonempty multiple of 4096 bytes")
    metadata = payload[:METADATA_SIZE]
    count = metadata[0]
    if count == 0:
        raise ValueError("TFT metadata declares zero frames")
    if metadata[count] != 0:
        raise ValueError("TFT metadata lacks the zero terminator")
    if any(value != 0xFF for value in metadata[count + 1 :]):
        raise ValueError("TFT metadata fill after the terminator is not 0xFF")
    frame_end = METADATA_SIZE + count * FRAME_BYTES
    if frame_end > len(payload):
        raise ValueError("declared frames exceed the TFT payload")
    frames = tuple(
        payload[
            METADATA_SIZE + index * FRAME_BYTES :
            METADATA_SIZE + (index + 1) * FRAME_BYTES
        ]
        for index in range(count)
    )
    padding = payload[frame_end:]
    if len(padding) >= BLOCK_SIZE or any(padding):
        raise ValueError("TFT alignment padding is not fewer than 4096 zero bytes")
    return TFTContainer(metadata, metadata[1:count], frames, padding)


def build_container(frames: list[bytes], timings: bytes) -> bytes:
    count = len(frames)
    if not 1 <= count <= 254:
        raise ValueError("TFT frame count must be between 1 and 254")
    if len(timings) != count - 1:
        raise ValueError("TFT container requires exactly N-1 timing bytes")
    if any(len(frame) != FRAME_BYTES for frame in frames):
        raise ValueError("every TFT frame must be 160x96 RGB565")
    metadata = bytearray(b"\xFF" * METADATA_SIZE)
    metadata[0] = count
    metadata[1:count] = timings
    metadata[count] = 0
    payload = pad_to_block(bytes(metadata) + b"".join(frames))
    parsed = parse_container(payload)
    if parsed.frames != tuple(frames) or parsed.timings != timings:
        raise AssertionError("TFT container failed its reconstruction check")
    return payload
