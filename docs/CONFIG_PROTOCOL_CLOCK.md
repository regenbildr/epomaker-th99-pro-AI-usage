# TH99 Pro config channel: set-clock command `0x34`

The `0x34` command sets the real-time clock shown on the keyboard's native
status screen (the same operation the official driver's "Time Correction"
performs). It is a **config-channel** command on `MI_02` — the same 64-byte
`AA <cmd> …` packet family as the keymap protocol, separate from the `MI_03`
TFT image path.

## Transport

- Interface: **`MI_02`** (config), not `MI_03` (TFT).
- Request: one **64-byte** output report on endpoint **`0x03`**.
- Acknowledgement: the **same 64 bytes echoed back** with the prefix changed
  `AA` → `55`, on IN endpoint **`0x84`**. (Same request/response convention as
  the keymap protocol.)
- A single report completes the operation — no multi-report sequence.
- **Wired USB only.** This channel exists on the wired composite device
  `VID_0C45&PID_800A`. Over the 2.4 GHz receiver the keyboard enumerates as a
  *different* device (`0C45:FEFE`) that does not expose the config/TFT
  interfaces, and the official driver offers time-sync only over direct USB —
  so `0x34` (and the `MI_03` TFT upload) work **only when wired**. (2.4 GHz PID
  and USB-only scope from the [goughlui.com teardown][teardown].)

## Packet layout (64 bytes)

Header is the 8-byte keymap-family header (`th99_keymap_protocol`):

| Offset | Bytes (request) | Meaning |
| --- | --- | --- |
| 0 | `AA` | request prefix (`55` in the echoed ACK) |
| 1 | `34` | **command: set clock** |
| 2 | `38` | declared payload length = 56 |
| 3–5 | `00 00 00` | 24-bit LE offset = 0 |
| 6 | `01` | final-packet flag |
| 7 | `00` | reserved |
| 8–17 | payload (below) | 10 meaningful bytes |
| 18–63 | `00…` | zero padding |

### Payload (bytes 8–17)

| Offset | Bytes | Field | Encoding |
| --- | --- | --- | --- |
| 8–10 | `5a 01 5a` | marker | constant |
| 11 | `YY` | year (last two digits, `+2000`) | plain binary (e.g. `0x1a` = 26 → 2026) |
| 12 | `MM` | month | plain binary (`0x07` = July) |
| 13 | `DD` | day | plain binary (`0x12` = 18) |
| 14 | `HH` | hour, 24-hour | plain binary (`0x15` = 21) |
| 15 | `MM` | minute | plain binary |
| 16 | `SS` | second | plain binary |
| 17 | `WD` | weekday | ISO weekday, Mon=1 … Sun=7 (`0x06` = Saturday) |

Values are **plain binary (hex == decimal), not BCD.** A third-party teardown
identifies the keyboard's RTC as a **CHMC D8563F** (a clone of the NXP
**PCF8563**), an I²C part whose hardware registers are *BCD* — so the firmware
converts this binary host payload to BCD before writing the RTC chip. The same
teardown measured that RTC drifting **~10 s/day**, which is precisely why the
driver exposes "Time Correction" as a manual resync at all. (RTC part ID and
drift figure: [goughlui.com teardown][teardown].)

Example payload — `5a 01 5a  1a 07 12 15 14 26  06` decodes to
2026-07-18 21:20:38, Saturday. The `5a 01 5a` marker and the field order are
fixed; only the datetime bytes change between calls.

## Known command set on this channel (`MI_02`, `AA <cmd>` header)

- `0x0F`–`0x1C` — read config tables (device info, keymaps, lighting, etc.)
- `0x22` / `0x26` — write basic / Fn keymap table
- **`0x34` — set real-time clock (this document)**
- `0xFC` — **event only, not a command** (see factory reset below)

Adjacent opcodes (`0x30`–`0x3F`) are plausible siblings for other native-screen
settings but are **unconfirmed** — probe only with captures, never by guessing
writes.

## Factory reset (`0xFC`): an event notification, not a HID command

The keyboard's factory reset (which resets keymaps and returns the native
screen in testing) **cannot be triggered by any HID command on this channel:**

- `AA FC` (a reset **request**) does not exist — the host never sends it.
- `55 FC` is emitted **asynchronously** by the keyboard as a reset event; its
  payload just echoes the current clock, the same fields as `0x34`. It is
  reset-specific (it does not appear on a normal connect).

The Epomaker web driver triggers the reset at the **USB / WebUSB layer** (a
descriptor-level / device re-open signal to the SONiX-class firmware), not
through the HID data channel. The [teardown][teardown] notes the driver
"requires WebUSB permissions" — and WebUSB (unlike WebHID) can issue raw control
transfers and reset/re-open the interface directly, which is what produces the
async `55 FC` event with no corresponding HID command. It is therefore **not
replayable** from a HID transport: there is **no software command — full or
targeted — to reset the keyboard or clear the TFT image.** The screen can only be
changed by uploading (overwriting) a new image via `MI_03`.

## Relevance to the usage display

`0x34` sets only the clock; it exposes no arbitrary text/number field, so it
does **not** replace the `MI_03` TFT image upload for showing usage bars. Its
value here is protocol ground truth: it proves a lightweight single-report
config channel and extends the confirmed command set. The tray application
offers it as **Sync keyboard clock now**. It is deliberately manual (the native
clock is hidden while the usage display is active), requires a wired keyboard
with the web driver closed, and does not upload a TFT image or use its
conservative flash-write budget.

## Capturing this yourself (Windows, USBPcap)

USBPcap must run elevated. Find the keyboard's root hub + device address with
`USBPcapCMD --extcap-interface \\.\USBPcapN --extcap-config` (look for the
composite device whose children match MI_00/01/02/03), then, from an
Administrator shell:

```powershell
& "C:\Program Files\USBPcap\USBPcapCMD.exe" -d \\.\USBPcap2 --devices <addr> `
  --inject-descriptors -o data\captures\th99-time-correction.pcap
```

`--inject-descriptors` is required so an already-connected device's descriptor
appears in the trace — `parse_th99_capture()` uses it to identify the keyboard.
Trigger "Time Correction" in the web driver, then Ctrl+C. Captures record all
keystrokes typed during the window, so keep them local (`data/captures/` is
gitignored).

[teardown]: https://goughlui.com/2026/05/05/review-teardown-epomaker-th99-pro-usb-2-4g-bt5-0-hot-swap-96-keyboard-w-lcd-knob-rgb-leds/
```
