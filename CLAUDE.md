# CLAUDE.md — TH99 Pro live usage display

Guidance for Claude Code (and any coding agent) working in this repo. Read this
first. Codex users: `AGENTS.md` points here.

## What this project does

Shows **live Claude and Codex subscription usage** (5-hour and 7-day windows) on
the TFT screen of an **Epomaker TH99 Pro** keyboard, using the keyboard's
existing USB-HID protocol — **no firmware modification or reflashing**.

Pipeline, once per poll:

```
Read Claude + Codex usage percentages and reset timestamps
      -> if layout or whole usage values changed and interval allows: render at 160x96
      -> Reset Timer values are remaining time until the reported reset
      -> convert to RGB565, build two identical frames in the confirmed TFT container, upload 16 reports
```

The renderer is deterministic: identical inputs always produce identical pixels
and the same SHA-256. In Reset Timer mode, the displayed time is the remaining
time until reset and is formatted only after the layout/percentage guard permits a
new image; only the selected layout and four whole usage percentages can request
a screen write.
Provider tokens are only read from local credentialstores, sent only to their provider, and never printed, logged, or embedded in
images.

## Platform

- **Windows only.** Python 3.12, PowerShell. Hard Windows dependencies:
  USB-HID via `kernel32`/ctypes, the registry via `winreg` (tray "run at
  startup"), and `os.startfile` (tray "open preview"). There is no macOS/Linux
  support — a port would need to replace those. Don't assume cross-platform when
  editing.
- Run commands **from the repo root**; entrypoints live in `src/`.

## Repository layout

| Path | Contents |
| --- | --- |
| `src/` | The live pipeline. All modules import each other with flat names; run them as `python src/<file>.py` from repo root (Python puts `src/` on the path). |
| `tests/` | Offline regression tests (`unittest`). `conftest.py` adds `src/` to the path. |
| `data/captures/` | `*.pcap` USB captures used to confirm/verify the protocol. **Local-only, gitignored** (personal dev captures — not published). Tests that need them skip when absent. |
| `data/keymaps/` | Keymap JSON backups/targets for the (separate) keymap-restore utility. **Local-only, gitignored** (personal). |
| `assets/` | Generated preview images / RGB565 blobs (reference outputs). |
| `schemas/` | Codex `app-server` JSON schemas (reference). |
| `docs/` | Protocol notes and per-utility READMEs (how the current pipeline works). |
| `archive/` | Superseded script versions, kept **locally only** (gitignored — not published). Not on the supported path. |

### Naming convention

Active modules in `src/` carry **no version suffix** — each concern is a single
canonical file (e.g. `th99_live_usage.py`, `th99_four_bar_renderer.py`). Earlier
iterations were consolidated into these and moved to `archive/` (gitignored,
local-only). Keep it that way: evolve a module in place rather than adding a
`_vN` sibling.

### Commit convention

Commits produced with the help of a coding agent should credit it with a
`Co-Authored-By` trailer as the **last** line of the commit message, e.g.:

```
Co-Authored-By: Claude <noreply@anthropic.com>
```

This is a co-author trailer (a human-readable credit), **not** a cryptographic
commit signature — do not GPG/SSH-sign on the agent's behalf or touch signing
keys. Because `noreply@anthropic.com` isn't linked to a GitHub account, the
trailer labels the commit but does not add the agent to the Contributors graph.

## Key entrypoints (`src/`)

- `th99_tray_app.py` — **system-tray on/off switch** (`pystray`). Runs the
  watcher in a background thread; menu = Start/Stop tracking, **Display layout**
  (Progress Bar/Reset Timer), **Usage-check frequency** (1/2 min; 2 min by
  default) and **Screen update limit** (5/10/15/30/60 min; 15 min by default)
  radio submenus,
  **Run at startup**, **Sync keyboard clock now**, latest values, open preview,
  Quit. Clock sync is the captured one-report `MI_02` `0x34` command and does
  not touch the TFT. Stop is asynchronous while an in-flight provider request
  exits; wait for **Start tracking** before restarting. Each run has its own stop
  event, preventing a stale run from affecting the new one. Turning tracking on
  performs live TFT uploads. Uses
  `th99_live_usage.build_watcher(...)`.
  - **Settings** persist to `%APPDATA%\th99-usage\config.json` (outside the
    repo, so never published); values are clamped to the `Watcher.validate()`
    floors on load. Presets are the guardrail — the menu can't set an unsafe
    value. Changing an interval while running applies on the next cycle.
  - **Run at startup** is the per-user registry key
    `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` value `TH99UsageTray`
    (no admin), launching via `pythonw` so there's no console window. The menu
    checkbox reflects the live registry state.
- `th99_live_usage.py` — **production utility/CLI** and the shared `Watcher`
  loop (used by both the CLI and the tray). Preview by default; live TFT upload
  is guarded by an acknowledgement phrase; `--watch` polls periodically. Renders
  via `th99_four_bar_renderer`, writes the preview atomically, and exposes
  `build_watcher()`.
- `provider_usage_probe.py` — read-only usage probe (no HID). Codex via the local
  `codex app-server`, Claude via the OAuth usage endpoint; represents missing
  windows explicitly as `N/A` rather than `0`.
- `claude_oauth_refresh.py` — direct Claude OAuth token refresh (see below).
- `th99_keymap_restore.py` — **separate** keymap-recovery tool. Not called by
  the usage utility; documented in `docs/KEYMAP_RESTORE.md`.

Transport/protocol: `th99_tft_container`, `th99_tft_protocol`,
`th99_tft_hid_transport`, `th99_tft_upload`, `th99_hid_transport`,
`th99_keymap_protocol`. Rendering: `th99_four_bar_renderer`, `th99_codex_bar`,
`create_th99_four_bar_mockup`.

## Running

```powershell
# System-tray on/off switch (recommended everyday control)
python src/th99_tray_app.py

# Read-only usage (no keyboard access)
python src/provider_usage_probe.py

# Preview only — renders assets/th99-live-usage-current.bmp, opens no HID handle
python src/th99_live_usage.py

# Guarded one-time live upload (keyboard wired; Epomaker web driver closed)
python src/th99_live_usage.py --execute-upload --acknowledge UPLOAD_LIVE_USAGE

# Continuous watcher (uploads only when values change; min interval enforced)
python src/th99_live_usage.py --execute-upload --acknowledge UPLOAD_LIVE_USAGE --watch
```

Tests (pytest is optional; plain unittest works):

```powershell
python -m pytest tests/ -q            # if pytest is installed
$env:PYTHONPATH="src"; python -m unittest discover -s tests -p "test_*.py"
```

The regression tests read `.pcap` fixtures from `data/captures/`, which is
**not published** (gitignored). With the fixtures present they run; on a fresh
clone without them they **skip** (never fail). Live upload's capture hash-pin
(`th99-upload.pcap`) likewise needs a local capture — supply your own to use
`--execute-upload`.

## Claude usage + OAuth refresh (the former blocker, now solved)

- **Usage:** `GET https://api.anthropic.com/api/oauth/usage` with the Claude Code
  OAuth access token from `~/.claude/.credentials.json` (header
  `anthropic-beta: oauth-2025-04-20`).
- **Refresh:** On Windows the credential is a **plaintext file** we can read and
  write, so `claude_oauth_refresh.py` performs the *same* refresh Claude Code does
  internally (extracted from the installed `cli.js`):
  - `POST https://platform.claude.com/v1/oauth/token`, `Content-Type: application/json`
  - body: `{grant_type: "refresh_token", refresh_token, client_id: "9d1c250a-e61b-44d9-88ed-5944d1962f5e", scope: "<space-joined scopes>"}`
  - response: `{access_token, refresh_token?, expires_in, scope?}`;
    `expiresAt = now_ms + expires_in*1000`.
- **Safety/coexistence:** refresh tokens **rotate**. The refresher reads the file
  fresh, refreshes only when within 5 min of expiry (or on a 401), writes the
  complete rotated credential back **atomically**, and holds a lock file to avoid
  racing Claude Code. **Run only one refresher.** No token value is ever printed.
- The macOS delegated-`/status` PTY approach used by CodexBar does **not** apply
  here — it only exists to work around the macOS Keychain, which Windows lacks.

Codex usage comes from the local `codex app-server` (`account/rateLimits/read`),
launched read-only/untrusted.

**Provenance of the polling design.** Both usage sources were modeled on
**CodexBar** (<https://github.com/steipete/codexbar>), an open-source macOS
menu-bar app that reads the same provider limits. Its `CodexBarCore` sources
established the two approaches we reuse on Windows: Codex via the local
`codex app-server` `account/rateLimits/read` call, and Claude via the Claude
Code OAuth credential + usage endpoint. The one thing that does **not** carry
over is CodexBar's macOS-Keychain refresh workaround (a delegated `/status` PTY
session) — Windows stores the Claude credential as a plaintext file, so we
refresh it directly instead (see above).

## Confirmed TH99 Pro protocol facts (summary)

TFT container details in `docs/TFT_PROTOCOL.md`; the `MI_02`
config channel (incl. the set-clock command) in `docs/CONFIG_PROTOCOL_CLOCK.md`.

- Wired USB `VID:PID = 0C45:800A`. `MI_02` = 64-byte keymap/config; `MI_03` = TFT.
- TFT HID reports are 4,104 bytes: `AA 50`, seq (LE), report count (LE), `50 06`,
  then a 4,096-byte payload block. ACK is 64 bytes starting `55 41 00 01`.
- Display 160x96, RGB565 little-endian (30,720 bytes/frame). Container = 256-byte
  metadata (`02 32 00` prefix) + two identical frames + zero padding = 16 reports.
- No HID reset/clear command is confirmed. The web-driver factory reset is not a
  HID command (the keyboard only emits an async `55 FC` event). **Observed:** a
  cold boot returns to the native status screen instead of restoring the custom
  usage display. This does not reveal whether bytes remain in flash; it means
  custom-display mode is not selected at boot. See `docs/TFT_PROTOCOL.md`.
- **Wired-only.** The `MI_02`/`MI_03` interfaces exist on the wired composite
  `0C45:800A`; over 2.4 GHz the board enumerates as `0C45:FEFE` (no config/TFT
  interfaces), so uploads and clock-sync require a wired connection.
- **Storage hardware** (per a third-party teardown, used to size the update
  limit): the keyboard contains a **Puya PY25Q128HA** 16 MB SPI flash (100k P/E
  cycles / 4 KB sector, 20-yr retention). We model each changed full upload as a
  flash cycle conservatively; host protocol cannot prove its storage commit or
  read it back. The RTC is a **CHMC D8563F** (PCF8563-class, BCD registers —
  hence `0x34`'s binary→BCD conversion). The flash-wear budget behind the 15-min
  default setting (with a five-minute hard minimum) is in `docs/TFT_PROTOCOL.md`.
  Teardown + datasheet cited there.
- `MI_02` config commands share a 64-byte `AA <cmd> <len> <off24> <final> <rsvd>`
  header (ACK = same bytes echoed with `AA`→`55` on IN `0x84`; requests OUT on
  `0x03`). Confirmed opcodes: `0x12/0x16` keymap read, `0x22/0x26` keymap write,
  `0x34` set real-time clock (`5a 01 5a` + `YY MM DD HH MM SS` binary + ISO
  weekday). This channel sets the native status screen's clock; it does **not**
  provide an arbitrary text field, so usage still needs the `MI_03` TFT image.

## Safety boundaries (do not cross)

- No firmware is compiled, modified, extracted, generated, or flashed — existing
  protocol only.
- TFT writes target only the confirmed `MI_03` interface. Keymap writes are not
  part of the usage utility.
- Live upload requires the explicit acknowledgement phrase and is pinned to the
  reviewed capture hash.
- Provider tokens are never logged or embedded in images. Do not commit
  `~/.claude/.credentials.json` (it lives outside the repo; `.gitignore` also
  blocks `*.credentials.json`).
- Keep the Epomaker web driver closed during live upload/watch; keyboard wired.
```
