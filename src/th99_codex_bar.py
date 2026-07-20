"""Render and optionally upload a static Codex usage bar to the TH99 Pro TFT.

The display payload always contains two identical 160x96 RGB565 little-endian
frames.  This retains the accepted 16-report AA50 container shape while
eliminating animation flicker.  Default mode is offline-only.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import struct
import sys

from th99_tft_hid_transport import (
    enumerate_hid_paths,
    open_hid,
    th99_display_paths,
)
from th99_tft_protocol import (
    FRAME_BYTES,
    FRAME_HEIGHT,
    FRAME_WIDTH,
    PREAMBLE_SIZE,
    build_reports,
    capture_upload,
    inspect_payload,
    pad_to_block,
    sha256,
)
from th99_tft_upload import (
    KNOWN_CAPTURE_FILE_HASH,
    file_sha256,
    send_upload,
)


FONT = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    "%": ("11001", "11010", "00100", "01000", "10110", "00110", "00000"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
}

BACKGROUND = (7, 11, 17)
TEXT = (232, 238, 245)
ACCENT = (24, 205, 155)
TRACK = (29, 38, 49)
BORDER = (98, 112, 128)


def text_width(text: str, scale: int) -> int:
    return max(0, len(text) * 6 * scale - scale)


class Canvas:
    def __init__(self, width: int, height: int, background: tuple[int, int, int]):
        self.width = width
        self.height = height
        self.pixels = [background] * (width * height)

    def rectangle(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        color: tuple[int, int, int],
    ) -> None:
        for py in range(max(0, y), min(self.height, y + height)):
            start = py * self.width
            for px in range(max(0, x), min(self.width, x + width)):
                self.pixels[start + px] = color

    def text(
        self,
        x: int,
        y: int,
        value: str,
        scale: int,
        color: tuple[int, int, int],
    ) -> None:
        cursor = x
        for character in value:
            glyph = FONT[character]
            for row, bits in enumerate(glyph):
                for column, bit in enumerate(bits):
                    if bit == "1":
                        self.rectangle(
                            cursor + column * scale,
                            y + row * scale,
                            scale,
                            scale,
                            color,
                        )
            cursor += 6 * scale


def render_frame(percent: int) -> tuple[bytes, list[tuple[int, int, int]]]:
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    canvas = Canvas(FRAME_WIDTH, FRAME_HEIGHT, BACKGROUND)

    canvas.text(8, 11, "CODEX", 2, TEXT)
    percentage = f"{percent}%"
    percentage_scale = 2 if percent == 100 else 3
    percentage_x = 152 - text_width(percentage, percentage_scale)
    canvas.text(percentage_x, 7, percentage, percentage_scale, ACCENT)

    x, y, width, height = 8, 42, 144, 28
    canvas.rectangle(x, y, width, height, BORDER)
    canvas.rectangle(x + 2, y + 2, width - 4, height - 4, TRACK)
    inner_width = width - 8
    fill_width = round(inner_width * percent / 100)
    if fill_width:
        canvas.rectangle(x + 4, y + 4, fill_width, height - 8, ACCENT)

    canvas.text(8, 80, "USAGE", 2, BORDER)

    rgb565 = bytearray()
    for red, green, blue in canvas.pixels:
        value = ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3)
        rgb565.extend(value.to_bytes(2, "little"))
    if len(rgb565) != FRAME_BYTES:
        raise AssertionError("renderer produced an incorrect frame size")
    return bytes(rgb565), canvas.pixels


def make_payload(captured_payload: bytes, frame: bytes) -> bytes:
    if len(captured_payload) < PREAMBLE_SIZE:
        raise ValueError("captured payload lacks the reviewed preamble")
    if len(frame) != FRAME_BYTES:
        raise ValueError("Codex frame has an incorrect size")
    preamble = bytearray(captured_payload[:PREAMBLE_SIZE])
    if preamble[0] != 180:
        raise ValueError("reviewed preamble no longer declares 180 frames")
    preamble[0] = 2
    return pad_to_block(bytes(preamble) + frame + frame)


def write_bmp(path: Path, pixels: list[tuple[int, int, int]]) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing file: {path}")
    row_size = (FRAME_WIDTH * 3 + 3) & ~3
    pixel_bytes = bytearray()
    for y in range(FRAME_HEIGHT - 1, -1, -1):
        row = pixels[y * FRAME_WIDTH : (y + 1) * FRAME_WIDTH]
        for red, green, blue in row:
            pixel_bytes.extend((blue, green, red))
        pixel_bytes.extend(bytes(row_size - FRAME_WIDTH * 3))
    pixel_offset = 14 + 40
    file_size = pixel_offset + len(pixel_bytes)
    header = b"BM" + struct.pack("<IHHI", file_size, 0, 0, pixel_offset)
    dib = struct.pack(
        "<IIIHHIIIIII",
        40,
        FRAME_WIDTH,
        FRAME_HEIGHT,
        1,
        24,
        0,
        len(pixel_bytes),
        2835,
        2835,
        0,
        0,
    )
    path.write_bytes(header + dib + pixel_bytes)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--percent", type=int, default=80)
    parser.add_argument(
        "--capture",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "captures" / "th99-upload.pcap",
    )
    parser.add_argument("--preview", type=Path)
    parser.add_argument("--export-payload", type=Path)
    parser.add_argument("--execute-upload", action="store_true")
    parser.add_argument(
        "--acknowledge",
        default="",
        help="live mode requires UPLOAD_CODEX_BAR_NNN_V2, with NNN as zero-padded percent",
    )
    parser.add_argument("--timeout-ms", type=int, default=5000)
    args = parser.parse_args()

    if not 0 <= args.percent <= 100:
        parser.error("--percent must be between 0 and 100")
    captured_reports, _, captured_payload = capture_upload(args.capture)
    if build_reports(captured_payload) != captured_reports:
        raise ValueError("reviewed upload does not regenerate exactly")
    frame, pixels = render_frame(args.percent)
    payload = make_payload(captured_payload, frame)
    reports = build_reports(payload)
    metadata = inspect_payload(payload)
    if metadata["frame_count"] != 2 or metadata["unique_frames"] != 1:
        raise ValueError("Codex payload must contain exactly two identical frames")
    if len(reports) != 16:
        raise ValueError("Codex payload must produce exactly 16 reports")

    candidates = th99_display_paths(enumerate_hid_paths())
    if args.preview is not None:
        write_bmp(args.preview, pixels)
        print(f"Preview written to {args.preview.resolve()}")
    if args.export_payload is not None:
        if args.export_payload.exists():
            parser.error(f"refusing to overwrite existing file: {args.export_payload}")
        args.export_payload.write_bytes(payload)
        print(f"Payload written to {args.export_payload.resolve()}")

    print("TH99 Pro Codex bar v2")
    print(f"Usage: {args.percent}%")
    print(f"Frame SHA-256: {sha256(frame)}")
    print(f"Payload SHA-256: {sha256(payload)}")
    print(f"Reports: {len(reports)} x 4104 bytes")
    print("Frames: 2 identical (static/no animation flicker)")
    print(f"MI_03 candidates: {len(candidates)}")

    if not args.execute_upload:
        print("DRY RUN: no HID handle was opened and no report was sent.")
        return 0

    required = f"UPLOAD_CODEX_BAR_{args.percent:03d}_V2"
    if args.acknowledge != required:
        parser.error(f"live mode requires --acknowledge {required}")
    if file_sha256(args.capture) != KNOWN_CAPTURE_FILE_HASH:
        parser.error("live mode is pinned to the reviewed TFT capture")
    if not 500 <= args.timeout_ms <= 30000:
        parser.error("--timeout-ms must be between 500 and 30000")
    if len(candidates) != 1:
        parser.error(
            f"expected exactly one TH99 Pro MI_03 interface, found {len(candidates)}"
        )

    kernel32, handle = open_hid(candidates[0])
    try:
        send_upload(kernel32, handle, reports, args.timeout_ms)
    finally:
        kernel32.CloseHandle(handle)
    print("Codex bar upload completed; visually confirm the keyboard screen.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        raise SystemExit(130)
