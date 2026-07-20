# How this was built (methodology & sources)

This project displays live Claude/Codex usage on an Epomaker TH99 Pro's TFT
screen **without touching firmware** — it only speaks the keyboard's existing
USB-HID protocol. Nothing about that protocol is publicly documented by Epomaker,
so it was reverse-engineered from observed USB traffic and cross-checked against
independent hardware and software sources. This document explains the method so
anyone wanting to build on it can see exactly how each fact was obtained and
what is confirmed vs. inferred.

## The overall approach

```
1. Identify the device + interfaces      (USB descriptors, teardown)
2. Capture the official web driver        (USBPcap + Wireshark)
      doing the exact operation we want    (upload image / set clock / etc.)
3. Decode the captured packets            (custom Python parsers in src/)
4. Replay the captured bytes verbatim     (pinned to the capture's SHA-256)
5. Only then substitute generated content (our deterministic renderer)
6. Model the usage-polling side on prior art (CodexBar)
```

The guiding rule throughout: **never guess writes to the keyboard.** Every byte
we send to a config/TFT channel was first observed coming from the official
driver. Exploratory writes to an undocumented vendor channel risk bricking or
corrupting device state, so discovery is strictly capture-driven.

## Packet-discovery tooling

- **[USBPcap](https://desowin.org/usbpcap/)** — a Windows USB packet-capture
  filter driver. Run elevated; it records raw USB transfers per root hub. Key
  invocation details we rely on:
  - `-d \\.\USBPcapN` selects a root hub (the "channel"); `--devices <addr>`
    narrows to one device address on that hub.
  - `--inject-descriptors` re-emits an already-connected device's descriptors
    into the trace so the parser can identify the keyboard mid-stream.
  - `-b 134217728` (max buffer) avoids dropped packets during bursty uploads.
  - Output is a classic little-endian `.pcap`.
- **[Wireshark](https://www.wireshark.org/)** — for interactive inspection and
  filtering (e.g. `usb.idVendor == 0x0c45 && usb.idProduct == 0x800a`).
- **Custom Python parsers** (`src/th99_keymap_protocol.py`,
  `src/th99_tft_protocol.py`) — decode the USBPcap records directly
  (`USBPCAP_HEADER`/`PCAP_RECORD_HEADER` structs), classify transfer types, and
  extract the `AA …`/`55 …` command frames. These are what turned raw traces
  into the documented packet layouts.
- **USB Device Tree Viewer** / Windows device manager — to find the composite
  device's interfaces and endpoints before capturing.

Captures live in `data/captures/` and are **local-only / gitignored**: USB
traces record every keystroke typed during the capture window, so they are
personal and never published. The regression tests read them as fixtures when
present and skip cleanly when absent.

## What each capture established

| Operation captured (in the web driver) | What it decoded to | Doc |
| --- | --- | --- |
| Custom image upload | `MI_03` TFT container: 4,104-byte `AA 50` reports, 256-byte metadata + two 160×96 RGB565 frames + padding = 16 reports; `55 41` ACK | `TFT_PROTOCOL.md` |
| "Time Correction" (clock sync) | `MI_02` `0x34` set-clock: `5a 01 5a` marker + binary `YY MM DD HH MM SS` + ISO weekday | `CONFIG_PROTOCOL_CLOCK.md` |
| Factory reset | *Not a HID command* — no `AA FC` request in any capture; only an async `55 FC` event. Reset is triggered at the USB/WebUSB layer and is not replayable | `CONFIG_PROTOCOL_CLOCK.md` |
| Key remap | `MI_02` `0x22`/`0x26` keymap writes, `0x12`/`0x16` reads (basis of the keymap-restore tool) | `KEYMAP_RESTORE.md` |
| Image "delete" | No keyboard-bound HID data — browser-local only; no device-side delete request is confirmed | `TFT_PROTOCOL.md` |

## Independent sources used to confirm / design

Reverse-engineered facts are stronger when they line up with independent
evidence. Two external sources were load-bearing:

- **TH99 Pro teardown — Gough Lui**
  ([goughlui.com](https://goughlui.com/2026/05/05/review-teardown-epomaker-th99-pro-usb-2-4g-bt5-0-hot-swap-96-keyboard-w-lcd-knob-rgb-leds/)).
  Provided the silicon-level ground truth we can't see over USB: the SPI flash
  part (**Puya PY25Q128HA**, 16 MB), main MCU (**HFD80CP100**, SONiX-class), RTC
  (**CHMC D8563F**, PCF8563-class), wireless MCU, and dual Li-Po battery; the
  wired `0C45:800A` vs. 2.4 GHz `0C45:FEFE` identities and USB-only scope; and
  the 250-frame GIF limit. **This teardown + the PY25Q128HA datasheet's
  100k-cycle/4 KB-sector endurance rating are the basis for the screen-update
  interval floor** — the flash-wear budget is worked out in
  `TFT_PROTOCOL.md`. It also explains the RTC's binary→BCD quirk and
  why the driver even has a "Time Correction" function (~10 s/day drift).
- **CodexBar — Peter Steinberger**
  ([github.com/steipete/codexbar](https://github.com/steipete/codexbar)).
  An open-source macOS menu-bar usage tracker. Its `CodexBarCore` sources are the
  prior art for **how we poll provider usage**: Codex via the local
  `codex app-server` `account/rateLimits/read` JSON-RPC call, and Claude via the
  Claude Code OAuth credential + usage endpoint. We diverge only on token
  refresh — CodexBar's delegated-PTY `/status` trick is a macOS-Keychain
  workaround; on Windows the credential is a plaintext file we refresh directly
  (`claude_oauth_refresh.py`).

Other references consulted: the [Epomaker Upgear](https://epomaker.com/blogs/software/epomaker-upgear)
web driver itself (the capture subject), and the public
[Epomaker GitHub org](https://github.com/Epomaker?tab=repositories) — QMK-derived
firmware for *other* boards, **not** the TH99 Pro's proprietary web-driver path.

## Confirmed vs. inferred

Everything in `TFT_PROTOCOL.md` and `CONFIG_PROTOCOL_CLOCK.md` under
"confirmed" was observed in captures and, where replayed, pinned to the capture's
SHA-256. Component part numbers come from the third-party teardown (cited, not
independently verified by us). The flash-wear lifetimes are arithmetic on the
datasheet rating and are worst-case bounds, not measurements. Anything not yet
observed (e.g. adjacent `0x30`–`0x3F` opcodes) is explicitly flagged as
unconfirmed and must be capture-verified, never guessed.

## Safety boundaries (unchanged)

No firmware is compiled, modified, extracted, or flashed. TFT writes target only
the confirmed `MI_03` interface; live upload requires an explicit acknowledgement
phrase and is pinned to a reviewed capture hash. Provider tokens are read from
local stores, sent only to their provider, and never logged or embedded in
images. See `CLAUDE.md` for the full list.
