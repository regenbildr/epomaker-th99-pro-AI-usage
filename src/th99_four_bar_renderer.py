"""Deterministic TH99 TFT layouts for usage bars and reset timers."""

from __future__ import annotations

import binascii
from pathlib import Path
import struct
import zlib

from th99_codex_bar import FONT, Canvas, text_width, write_bmp
from th99_tft_protocol import FRAME_BYTES


WIDTH = 160
HEIGHT = 96
BACKGROUND = (7, 11, 17)
TEXT = (226, 233, 241)
MUTED = (112, 122, 134)
TRACK = (25, 34, 44)
TRACK_BORDER = (73, 86, 101)
CLAUDE = (218, 119, 86)
CODEX = (24, 205, 155)
DIVIDER = (43, 53, 65)
ROW_TEXT_SCALE = 2

DISPLAY_MODE_PROGRESS_BAR = "progress_bar"
DISPLAY_MODE_RESET_TIMER = "reset_timer"
DISPLAY_MODES = (DISPLAY_MODE_PROGRESS_BAR, DISPLAY_MODE_RESET_TIMER)

FONT.update(
    {
        "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
        "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
        "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
        "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
        "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
        "/": ("00001", "00010", "00100", "01000", "10000", "00000", "00000"),
    }
)


# Progress Bar layout: provider -> 5H/7D + bar -> percentage.
SECTION_GAP = 6
ROW_LABEL_X = 15
BAR_X = 40
VALUE_X = WIDTH - text_width("99%", ROW_TEXT_SCALE)
BAR_RIGHT = VALUE_X - SECTION_GAP
BAR_WIDTH = BAR_RIGHT - BAR_X

# Reset Timer layout: provider -> reset counter -> percentage.
RESET_TIMER_X = 15
RESET_VALUE_X = VALUE_X


def _validate_percent(percent: int | None) -> None:
    if percent is not None and not 0 <= percent <= 100:
        raise ValueError("percentage must be between 0 and 100 or None")


def _validate_values(values: tuple[int | None, ...]) -> None:
    if len(values) != 4:
        raise ValueError("renderer needs exactly four usage values")
    for percent in values:
        _validate_percent(percent)


def draw_text_rotated_minus_90(
    canvas: Canvas,
    *,
    x: int,
    y: int,
    value: str,
    color: tuple[int, int, int],
) -> None:
    normal_width = text_width(value, 1)
    cursor = 0
    for character in value:
        glyph = FONT[character]
        for row, bits in enumerate(glyph):
            for column, bit in enumerate(bits):
                if bit == "1":
                    canvas.rectangle(
                        x + row,
                        y + normal_width - 1 - (cursor + column),
                        1,
                        1,
                        color,
                    )
        cursor += 6


def draw_compact_text(
    canvas: Canvas,
    *,
    x: int,
    y: int,
    value: str,
    scale: int,
    advance: int,
    color: tuple[int, int, int],
) -> None:
    """Draw scale-2 text with tighter tracking for the four-character 100%."""
    cursor = x
    for character in value:
        glyph = FONT[character]
        for row, bits in enumerate(glyph):
            for column, bit in enumerate(bits):
                if bit == "1":
                    canvas.rectangle(
                        cursor + column * scale,
                        y + row * scale,
                        scale,
                        scale,
                        color,
                    )
        cursor += advance


def draw_percentage(
    canvas: Canvas,
    *,
    x: int,
    y: int,
    percent: int | None,
    color: tuple[int, int, int],
) -> None:
    _validate_percent(percent)
    if percent is None:
        canvas.text(x, y, "N/A", ROW_TEXT_SCALE, MUTED)
    elif percent == 100:
        draw_compact_text(
            canvas,
            x=x,
            y=y,
            value="100%",
            scale=ROW_TEXT_SCALE,
            advance=8,
            color=color,
        )
    else:
        canvas.text(x, y, f"{percent:02d}%", ROW_TEXT_SCALE, color)


def draw_progress_bar_row(
    canvas: Canvas,
    *,
    y: int,
    label: str,
    percent: int | None,
    color: tuple[int, int, int],
) -> None:
    _validate_percent(percent)
    height = 21
    text_y = y + 3
    canvas.text(ROW_LABEL_X, text_y, label, ROW_TEXT_SCALE, TEXT)
    canvas.rectangle(BAR_X, y, BAR_WIDTH, height, TRACK_BORDER)
    canvas.rectangle(BAR_X + 1, y + 1, BAR_WIDTH - 2, height - 2, TRACK)

    if percent is not None:
        fill = round((BAR_WIDTH - 4) * percent / 100)
        if fill:
            canvas.rectangle(BAR_X + 2, y + 2, fill, height - 4, color)
    draw_percentage(canvas, x=VALUE_X, y=text_y, percent=percent, color=color)


def draw_timer(
    canvas: Canvas,
    *,
    x: int,
    y: int,
    value: str,
    show_days: bool,
    color: tuple[int, int, int],
) -> None:
    """Draw ``0D 03H 09M`` using the same 5x7 scale-2 font as ``5H``/``7D``."""
    cursor = x
    for index, character in enumerate(value):
        if not show_days and index in (0, 1):
            cursor += 6 * ROW_TEXT_SCALE
            continue
        if character == " ":
            cursor += 4
            continue
        glyph = FONT[character]
        for row, bits in enumerate(glyph):
            for column, bit in enumerate(bits):
                if bit == "1":
                    canvas.rectangle(
                        cursor + column * ROW_TEXT_SCALE,
                        y + row * ROW_TEXT_SCALE,
                        ROW_TEXT_SCALE,
                        ROW_TEXT_SCALE,
                        color,
                    )
        cursor += 6 * ROW_TEXT_SCALE


def draw_reset_timer_row(
    canvas: Canvas,
    *,
    y: int,
    percent: int | None,
    reset_timer: str | None,
    show_days: bool,
    color: tuple[int, int, int],
) -> None:
    text_y = y + 3
    if reset_timer is None:
        canvas.text(RESET_TIMER_X, text_y, "N/A", ROW_TEXT_SCALE, MUTED)
    else:
        draw_timer(
            canvas,
            x=RESET_TIMER_X,
            y=text_y,
            value=reset_timer,
            show_days=show_days,
            color=TEXT,
        )
    draw_percentage(canvas, x=RESET_VALUE_X, y=text_y, percent=percent, color=color)


def render_progress_pixels(values: tuple[int | None, ...]) -> list[tuple[int, int, int]]:
    _validate_values(values)
    claude_5h, claude_7d, codex_5h, codex_7d = values
    canvas = Canvas(WIDTH, HEIGHT, BACKGROUND)
    draw_text_rotated_minus_90(canvas, x=2, y=6, value="CLAUDE", color=CLAUDE)
    draw_progress_bar_row(canvas, y=1, label="5H", percent=claude_5h, color=CLAUDE)
    draw_progress_bar_row(canvas, y=25, label="7D", percent=claude_7d, color=CLAUDE)
    canvas.rectangle(1, 47, 158, 1, DIVIDER)
    draw_text_rotated_minus_90(canvas, x=2, y=57, value="CODEX", color=CODEX)
    draw_progress_bar_row(canvas, y=50, label="5H", percent=codex_5h, color=CODEX)
    draw_progress_bar_row(canvas, y=74, label="7D", percent=codex_7d, color=CODEX)
    return canvas.pixels


def render_reset_timer_pixels(
    values: tuple[int | None, ...],
    reset_timers: tuple[str | None, ...],
) -> list[tuple[int, int, int]]:
    _validate_values(values)
    if len(reset_timers) != 4:
        raise ValueError("reset-timer layout needs exactly four reset values")
    claude_5h, claude_7d, codex_5h, codex_7d = values
    claude_5h_timer, claude_7d_timer, codex_5h_timer, codex_7d_timer = reset_timers
    canvas = Canvas(WIDTH, HEIGHT, BACKGROUND)
    draw_text_rotated_minus_90(canvas, x=2, y=6, value="CLAUDE", color=CLAUDE)
    draw_reset_timer_row(
        canvas,
        y=1,
        percent=claude_5h,
        reset_timer=claude_5h_timer,
        show_days=False,
        color=CLAUDE,
    )
    draw_reset_timer_row(
        canvas,
        y=25,
        percent=claude_7d,
        reset_timer=claude_7d_timer,
        show_days=True,
        color=CLAUDE,
    )
    canvas.rectangle(1, 47, 158, 1, DIVIDER)
    draw_text_rotated_minus_90(canvas, x=2, y=57, value="CODEX", color=CODEX)
    draw_reset_timer_row(
        canvas,
        y=50,
        percent=codex_5h,
        reset_timer=codex_5h_timer,
        show_days=False,
        color=CODEX,
    )
    draw_reset_timer_row(
        canvas,
        y=74,
        percent=codex_7d,
        reset_timer=codex_7d_timer,
        show_days=True,
        color=CODEX,
    )
    return canvas.pixels


def render_pixels(
    claude_5h: int | None,
    claude_7d: int | None,
    codex_5h: int | None,
    codex_7d: int | None,
    *,
    display_mode: str = DISPLAY_MODE_PROGRESS_BAR,
    reset_timers: tuple[str | None, ...] | None = None,
) -> list[tuple[int, int, int]]:
    values = (claude_5h, claude_7d, codex_5h, codex_7d)
    if display_mode == DISPLAY_MODE_PROGRESS_BAR:
        return render_progress_pixels(values)
    if display_mode == DISPLAY_MODE_RESET_TIMER:
        return render_reset_timer_pixels(values, reset_timers or (None, None, None, None))
    raise ValueError(f"unknown display mode: {display_mode}")


def pixels_to_rgb565(pixels: list[tuple[int, int, int]]) -> bytes:
    if len(pixels) != WIDTH * HEIGHT:
        raise ValueError("renderer returned an incorrect pixel count")
    frame = bytearray()
    for red, green, blue in pixels:
        value = ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3)
        frame.extend(value.to_bytes(2, "little"))
    if len(frame) != FRAME_BYTES:
        raise AssertionError("RGB565 frame has an incorrect size")
    return bytes(frame)


def write_preview(
    path: Path,
    values: tuple[int | None, ...],
    *,
    display_mode: str = DISPLAY_MODE_PROGRESS_BAR,
    reset_timers: tuple[str | None, ...] | None = None,
) -> None:
    write_bmp(path, render_pixels(*values, display_mode=display_mode, reset_timers=reset_timers))


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", binascii.crc32(body))


def write_png(
    path: Path,
    values: tuple[int | None, ...],
    scale: int = 4,
    *,
    display_mode: str = DISPLAY_MODE_PROGRESS_BAR,
    reset_timers: tuple[str | None, ...] | None = None,
) -> None:
    if scale < 1:
        raise ValueError("PNG scale must be at least one")
    pixels = render_pixels(*values, display_mode=display_mode, reset_timers=reset_timers)
    raw = bytearray()
    for y in range(HEIGHT):
        row = pixels[y * WIDTH : (y + 1) * WIDTH]
        expanded = b"".join(bytes(pixel) * scale for pixel in row)
        for _ in range(scale):
            raw.append(0)
            raw.extend(expanded)
    signature = b"\x89PNG\r\n\x1a\n"
    header = struct.pack(">IIBBBBB", WIDTH * scale, HEIGHT * scale, 8, 2, 0, 0, 0)
    path.write_bytes(
        signature
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )