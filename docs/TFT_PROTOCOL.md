# TH99 Pro TFT screen protocol

How the Epomaker TH99 Pro's TFT screen is driven over USB HID, and why the
screen-update interval is rate-limited. All values below are the ones the live
pipeline uses.

## Transport & container

- **Transport:** USB HID interface `MI_03` on the wired composite device
  `VID_0C45&PID_800A`. (The screen is not reachable over 2.4 GHz/Bluetooth.)
- **Output reports:** 4,104 bytes each, sent as a fixed sequence of **16
  reports** per image.
- **Report header:** `AA 50`, 16-bit sequence, 16-bit report count, `50 06`,
  followed by a 4,096-byte payload block.
- **Acknowledgement:** 64 bytes beginning `55 41 00 01`, remainder zero — one per
  report (16/16 on a successful upload).

## Payload layout

A full image is a **256-byte metadata block** followed by **two identical
160×96 RGB565 little-endian frames** (30,720 bytes each), zero-padded to a
4,096-byte boundary — 16 reports total.

Metadata block for `N` frames:

- byte 0: `N` (frame count)
- bytes 1 … `N-1`: per-frame timing values
- byte `N`: `00` terminator
- remaining bytes through 255: `FF`

The deterministic two-frame image therefore begins `02 32 00 FF…`, followed by
the two frames and the trailing zero padding. `th99_tft_container.py` builds
this payload; `th99_tft_protocol.py` frames and sends the 16 reports.

## Clearing the screen and cold boot

There is **no confirmed software command to clear the screen or delete an
uploaded image**. A factory reset would clear it, but factory reset is **not a
HID command**: the web driver triggers it at the USB/WebUSB layer and the
keyboard only emits an asynchronous `55 FC` event (see
[CONFIG_PROTOCOL_CLOCK.md](CONFIG_PROTOCOL_CLOCK.md)).

**Observed behavior:** after a cold power-off/power-on cycle, the keyboard
returns to its native clock/status screen rather than automatically displaying
the uploaded usage image. This gives the user a safe way to leave the custom
display without a factory reset. USB captures do not expose a display read-back
or storage commit, so this observation does **not** prove whether the uploaded
bytes remain in SPI flash; it only proves that the firmware does not restore the
custom display mode at boot. Starting the watcher again uploads the current
image for the new powered session.

## Storage hardware and the flash-wear budget (why the update interval is limited)

The five-minute **screen-update interval floor** (the tray app's 5/10/15/30/60-
minute presets, plus the "upload only when the value changed" rule) is a
deliberate flash-endurance guardrail, sized from the keyboard's storage hardware.
The default screen-update setting is 15 minutes.

- **Storage chip:** the keyboard contains a **Puya PY25Q128HA** (128 Mbit / 16
  MB) SPI flash, per an independent [teardown][teardown]. The host protocol
  does not expose a read-back or storage-commit operation, and cold boot returns
  to the native screen; therefore treating an upload as a flash write is a
  **conservative safety assumption**, not a directly observed storage trace.
- **Endurance (PY25Q128HA datasheet):** **100,000 program/erase cycles per erase
  block**, 20-year retention, with 4 KB sector / 32 KB / 64 KB / chip erase
  granularities. Flash bits only clear `1→0` on program, so changing any byte
  back requires erasing the whole block first.
- **Our image size:** the 16-report container is 65,536 bytes — exactly one
  64 KB block / sixteen 4 KB sectors. If each upload erases and rewrites one
  such region, all sixteen sectors take **one P/E cycle per upload, in
  parallel** and reach the 100k limit together. That yields the conservative
  **~100,000-upload** budget below. Firmware storage layout and any
  wear-leveling are not observable over HID.

Worst case (the value changes on every gated interval):

| Update-interval setting | Uploads/day (worst case) | Lifetime to 100k P/E |
| --- | --- | --- |
| 5 min | 288 | ~1.0 year |
| 10 min | 144 | ~1.9 years |
| 15 min (default) | 96 | ~2.9 years |
| 30 min | 48 | ~5.7 years |
| 60 min | 24 | ~11.4 years |

The *real* figure is far better than the worst-case column, because the watcher
**skips uploads when the rendered image is byte-identical** to the last one (so
plateaus cost nothing), and any firmware wear-leveling across the mostly-empty
16 MB flash would extend it further. The default setting is **15 minutes**;
5 minutes is the supported hard minimum for users who accept the shorter
projected endurance. Do not remove the change-skip behavior.

[teardown]: https://goughlui.com/2026/05/05/review-teardown-epomaker-th99-pro-usb-2-4g-bt5-0-hot-swap-96-keyboard-w-lcd-knob-rgb-leds/
