"""Create a native-resolution TH99 four-limit UI reset-timer mockup."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from th99_codex_bar import Canvas, FONT


WIDTH = 160
HEIGHT = 96
BACKGROUND = (7, 11, 17)
TEXT = (226, 233, 241)
MUTED = (112, 126, 143)
CLAUDE = (218, 119, 86)
CODEX = (24, 205, 155)
DIVIDER = (43, 53, 65)


FONT.update(
    {
        " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
        "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
        "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
        "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
        "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
        "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
        "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
        "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
        "/": ("00001", "00010", "00100", "01000", "10000", "00000", "00000"),
        "d": ("00001", "00001", "01111", "10001", "10001", "10001", "01111"),
        "h": ("10000", "10000", "10110", "11001", "10001", "10001", "10001"),
        "m": ("00000", "00000", "11010", "10101", "10101", "10101", "10101"),
    }
)


ROW_TEXT_SCALE = 2
TIMER_X = 15
PERCENT_X = 126


def draw_text_rotated_minus_90(
    canvas: Canvas,
    *,
    x: int,
    y: int,
    value: str,
    color: tuple[int, int, int],
) -> None:
    cursor = 0
    normal_width = len(value) * 6 - 1
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


def draw_compact_percent(
    canvas: Canvas,
    *,
    x: int,
    y: int,
    percent: int,
    color: tuple[int, int, int],
) -> None:
    if percent != 100:
        canvas.text(x, y, f"{percent:02d}%", ROW_TEXT_SCALE, color)
        return

    cursor = x
    for character in "100%":
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
        cursor += 8


def draw_timer(
    canvas: Canvas,
    *,
    x: int,
    y: int,
    value: str,
    show_days: bool,
    color: tuple[int, int, int],
) -> None:
    """Draw the timer with the same 5x7, scale-2 glyphs as the row labels."""
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

def draw_row(
    canvas: Canvas,
    *,
    y: int,
    percent: int | None,
    reset_in: str,
    show_days: bool,
    color: tuple[int, int, int],
) -> None:
    text_y = y + 3
    draw_timer(
        canvas,
        x=TIMER_X,
        y=text_y,
        value=reset_in,
        show_days=show_days,
        color=TEXT,
    )
    if percent is None:
        canvas.text(PERCENT_X, text_y, "N/A", ROW_TEXT_SCALE, MUTED)
    else:
        draw_compact_percent(canvas, x=PERCENT_X, y=text_y, percent=percent, color=color)

def render() -> list[tuple[int, int, int]]:
    canvas = Canvas(WIDTH, HEIGHT, BACKGROUND)

    draw_text_rotated_minus_90(canvas, x=2, y=6, value="CLAUDE", color=CLAUDE)
    draw_row(canvas, y=1, percent=38, reset_in="0D 01H 47M", show_days=False, color=CLAUDE)
    draw_row(canvas, y=25, percent=64, reset_in="4D 08H 09M", show_days=True, color=CLAUDE)

    canvas.rectangle(1, 47, 158, 1, DIVIDER)

    draw_text_rotated_minus_90(canvas, x=2, y=57, value="CODEX", color=CODEX)
    draw_row(canvas, y=50, percent=80, reset_in="0D 02H 31M", show_days=False, color=CODEX)
    draw_row(canvas, y=74, percent=51, reset_in="1D 08H 09M", show_days=True, color=CODEX)
    return canvas.pixels


def main() -> int:
    output = Path("assets/th99-reset-timer-mockup.png")
    image = Image.new("RGB", (WIDTH, HEIGHT))
    image.putdata(render())
    image.save(output, format="PNG")
    print(f"Created {output.resolve()} at {WIDTH}x{HEIGHT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())