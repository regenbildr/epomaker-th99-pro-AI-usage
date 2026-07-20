# AGENTS.md

**Read [`CLAUDE.md`](CLAUDE.md) first — it is the authoritative guide for this
repo.** This file is a short pointer for Codex and other coding agents; the full
architecture, run commands, OAuth-refresh design, protocol facts, and safety
boundaries live in `CLAUDE.md`.

## Quick facts

- **Goal:** display live Claude/Codex usage on an Epomaker TH99 Pro TFT via its
  existing USB-HID protocol — no firmware changes.
- **Platform: Windows only** / PowerShell / Python 3.12 (hard deps: `kernel32`
  USB-HID, `winreg`, `os.startfile`; no macOS/Linux support). Run everything
  **from the repo root**; entrypoints are in `src/`.
- **Layout:** `src/` (live pipeline, flat imports), `tests/`, `data/captures/`,
  `data/keymaps/`, `assets/`, `schemas/`, `docs/`. (`data/*` and `archive/` are
  gitignored, local-only.)
- **Naming:** active modules carry no version suffix — one canonical file per
  concern (evolve in place, don't add a `_vN` sibling). Superseded iterations
  live in `archive/` (gitignored). See CLAUDE.md "Naming convention".

## Run

```powershell
python src/provider_usage_probe.py          # read-only usage
python src/th99_live_usage.py               # preview only, no HID
# live upload is guarded:
python src/th99_live_usage.py --execute-upload --acknowledge UPLOAD_LIVE_USAGE
$env:PYTHONPATH="src"; python -m unittest discover -s tests -p "test_*.py"
```

## Git commits

- When creating a commit in this repository, add the trailer
  `Co-authored-by: Codex <codex@openai.com>` unless the user directs otherwise.

## Guardrails

- No firmware compile/modify/extract/flash. Existing protocol only; TFT writes
  only to the confirmed `MI_03` interface.
- Never print, log, or commit provider tokens. Claude OAuth refresh is direct on
  Windows (`src/claude_oauth_refresh.py`); run only one refresher (refresh tokens
  rotate).
- Do not run a live TFT upload without the acknowledgement phrase, a wired
  keyboard, and the Epomaker web driver closed.
```
