"""System-tray on/off switch for the TH99 Pro live usage watcher.

A tray icon (green = running, grey = stopped, red = error) with a menu to:
- start/stop live tracking,
- sync the keyboard's native-screen clock to Windows,
- see the latest Claude/Codex values,
- choose Progress Bar or Reset Timer layout, the 1- or 2-minute usage-check
  frequency, and the minimum screen-update interval,
- run at startup (per-user, no admin), and
- open the last preview / quit.

The watcher runs in a background thread in this process, so the menu controls
it directly. Settings persist to ``%APPDATA%\\th99-usage\\config.json`` (outside
the repo), validated/clamped on load.

Run from the repo root:

    python src/th99_tray_app.py

Turning tracking ON performs real TFT uploads (keyboard wired, Epomaker web
driver closed). Only one instance should run at a time.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import threading
import winreg

import pystray
from PIL import Image, ImageDraw

import th99_live_usage as app
from th99_clock_sync import sync_keyboard_clock


RUNNING_COLOR = (46, 204, 113)  # green
STOPPED_COLOR = (127, 140, 141)  # grey
ERROR_COLOR = (231, 76, 60)  # red

# Preset choices (seconds, label). Presets are the guardrail — a user can only
# pick a sane value from the menu; load_config() clamps anything hand-edited.
# Polling only reads provider usage; TFT flash writes remain governed by the
# separate screen-update limit below.
DEFAULT_POLL_SECONDS = 120
DEFAULT_MIN_UPLOAD_SECONDS = 900
DEFAULT_DISPLAY_MODE = app.DISPLAY_MODE_PROGRESS_BAR

POLL_CHOICES = [(60, "1 minute"), (120, "2 minutes (default)")]
WRITE_CHOICES = [
    (300, "5 minutes"),
    (600, "10 minutes"),
    (900, "15 minutes (default)"),
    (1800, "30 minutes"),
    (3600, "60 minutes"),
]

DISPLAY_CHOICES = [
    (app.DISPLAY_MODE_PROGRESS_BAR, "Progress Bar"),
    (app.DISPLAY_MODE_RESET_TIMER, "Reset Timer"),
]

# Hard floors, matching th99_live_usage.Watcher.validate().
MIN_POLL_SECONDS = 60
MIN_WRITE_SECONDS = 300

CONFIG_DIR = Path(os.getenv("APPDATA") or Path.home()) / "th99-usage"
CONFIG_PATH = CONFIG_DIR / "config.json"

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "TH99UsageTray"


# --- config ------------------------------------------------------------------
@dataclass
class Config:
    poll_seconds: int = DEFAULT_POLL_SECONDS
    min_upload_seconds: int = DEFAULT_MIN_UPLOAD_SECONDS
    display_mode: str = DEFAULT_DISPLAY_MODE


def load_config() -> Config:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        data = {}
    poll = data.get("poll_seconds", DEFAULT_POLL_SECONDS)
    write = data.get("min_upload_seconds", DEFAULT_MIN_UPLOAD_SECONDS)
    display_mode = data.get("display_mode", DEFAULT_DISPLAY_MODE)
    valid_polls = {seconds for seconds, _ in POLL_CHOICES}
    valid_writes = {seconds for seconds, _ in WRITE_CHOICES}
    valid_display_modes = {mode for mode, _ in DISPLAY_CHOICES}
    if not isinstance(poll, int) or poll not in valid_polls:
        poll = DEFAULT_POLL_SECONDS
    if not isinstance(write, int) or write not in valid_writes:
        write = DEFAULT_MIN_UPLOAD_SECONDS
    if not isinstance(display_mode, str) or display_mode not in valid_display_modes:
        display_mode = DEFAULT_DISPLAY_MODE
    return Config(poll, write, display_mode)


def save_config(config: Config) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "poll_seconds": config.poll_seconds,
                    "min_upload_seconds": config.min_upload_seconds,
                    "display_mode": config.display_mode,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(CONFIG_PATH)
    except OSError:
        pass  # a lost preference is not worth crashing the tray


# --- run at startup (per-user registry Run key) ------------------------------
def _startup_command() -> str:
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    launcher = pythonw if pythonw.exists() else exe
    script = Path(__file__).resolve()
    return f'"{launcher}" "{script}"'


def startup_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_startup(enabled: bool) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, _startup_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


# --- icon --------------------------------------------------------------------
def _make_image(color: tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGB", (64, 64), (24, 26, 32))
    draw = ImageDraw.Draw(image)
    draw.ellipse((10, 10, 54, 54), fill=color)
    return image


def _fmt(value: int | None) -> str:
    return f"{value}%" if value is not None else "N/A"


class TrayController:
    def __init__(self) -> None:
        self.config = load_config()
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._watcher: app.Watcher | None = None
        self._lock = threading.Lock()
        self._icon_lock = threading.Lock()  # serialize all pystray icon writes
        self._current_color: tuple[int, int, int] | None = None
        self._running = False
        self._stopping = False
        self._run_token = 0
        self._values: tuple[int | None, ...] | None = None
        self._error: str | None = None
        self._clock_syncing = False
        self._clock_synced_at: str | None = None
        self.icon: pystray.Icon | None = None

    # --- state ----------------------------------------------------------
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def _state_text(self, _item=None) -> str:
        """Reflects the actual state: error / paused / values / starting."""
        with self._lock:
            error = self._error
            values = self._values
            running = self._running
            stopping = self._stopping
            clock_syncing = self._clock_syncing
            clock_synced_at = self._clock_synced_at
        if error:
            return f"error ({error})"
        if stopping:
            return "stopping..."
        if clock_syncing:
            return "syncing keyboard clock..."
        if not running:
            return f"paused; clock synced {clock_synced_at}" if clock_synced_at else "paused"
        if values:
            c5, c7, x5, x7 = values
            status = f"Claude {_fmt(c5)}/{_fmt(c7)}, Codex {_fmt(x5)}/{_fmt(x7)}"
            return f"{status}; clock synced {clock_synced_at}" if clock_synced_at else status
        return "starting..."

    def _title(self, _item=None) -> str:
        return f"TH99 Pro: {self._state_text()}"

    def _toggle_label(self, _item=None) -> str:
        with self._lock:
            if self._stopping:
                return "Stopping..."
            return "Stop tracking" if self._running else "Start tracking"

    def _refresh(self) -> None:
        if self.icon is None:
            return
        with self._lock:
            color = (
                ERROR_COLOR if self._error
                else (RUNNING_COLOR if self._running else STOPPED_COLOR)
            )
        title = self._title()
        # Serialize icon writes: this runs from both the watcher thread and the
        # GUI thread, and concurrent icon rebuilds crash Win32 (DestroyIcon,
        # WinError 1402). Only rebuild the HICON when the color actually changed.
        with self._icon_lock:
            try:
                if color != self._current_color:
                    self.icon.icon = _make_image(color)
                    self._current_color = color
                self.icon.title = title
                self.icon.update_menu()
            except OSError:
                pass  # transient tray repaint race; the next event repaints

    def _on_status(self, run_token: int, status: dict) -> None:
        """Apply status only from the current watcher run."""
        with self._lock:
            if run_token != self._run_token:
                return
            if status.get("values") is not None:
                self._values = status["values"]
                self._error = None
            elif status.get("errors"):
                self._error = "; ".join(status["errors"])
        self._refresh()

    # --- settings -------------------------------------------------------
    def _set_poll(self, seconds: int) -> None:
        self.config.poll_seconds = seconds
        save_config(self.config)
        if self._watcher is not None:
            self._watcher.poll_seconds = seconds  # applies on the next cycle
        self._refresh()

    def _set_write(self, seconds: int) -> None:
        self.config.min_upload_seconds = seconds
        save_config(self.config)
        if self._watcher is not None:
            self._watcher.min_upload_seconds = seconds
        self._refresh()

    def _set_display_mode(self, display_mode: str) -> None:
        self.config.display_mode = display_mode
        save_config(self.config)
        if self._watcher is not None:
            self._watcher.set_display_mode(display_mode)
        self._refresh()

    def toggle_startup(self, _icon=None, _item=None) -> None:
        try:
            set_startup(not startup_enabled())
        except OSError as error:
            with self._lock:
                self._error = f"startup: {error}"
        self._refresh()

    # --- actions --------------------------------------------------------
    def start(self, _icon=None, _item=None) -> None:
        # A stopped worker may still be unwinding a provider request. Do not start
        # another one until it has exited; otherwise two runs can race for HID.
        with self._lock:
            if self._running or (self._thread is not None and self._thread.is_alive()):
                return
            self._error = None
            self._stopping = False
            self._run_token += 1
            run_token = self._run_token
            stop_event = threading.Event()

        watcher = app.build_watcher(
            on_status=lambda status: self._on_status(run_token, status),
            poll_seconds=self.config.poll_seconds,
            min_upload_seconds=self.config.min_upload_seconds,
            display_mode=self.config.display_mode,
        )
        try:
            watcher.validate()
        except ValueError as error:
            with self._lock:
                if run_token == self._run_token:
                    self._error = str(error)
            self._refresh()
            return

        def run() -> None:
            try:
                watcher.run_forever(stop_event)
            except Exception as error:  # keep the tray alive; surface the reason
                with self._lock:
                    if run_token == self._run_token:
                        self._error = f"{type(error).__name__}: {error}"
            finally:
                with self._lock:
                    if self._thread is threading.current_thread():
                        self._running = False
                        self._stopping = False
                        self._thread = None
                        self._watcher = None
                        self._stop = None
                self._refresh()

        thread = threading.Thread(target=run, name="th99-watcher", daemon=True)
        with self._lock:
            self._watcher = watcher
            self._stop = stop_event
            self._thread = thread
            self._running = True
        thread.start()
        self._refresh()

    def stop(self, _icon=None, _item=None) -> None:
        # Stopping is asynchronous so the tray stays responsive while a current
        # provider request finishes. A fresh Event per run prevents restart races.
        with self._lock:
            if not self._running:
                return
            self._error = None
            self._stopping = True
            self._run_token += 1  # Ignore any late status from the stopped run.
            stop_event = self._stop
        if stop_event is not None:
            stop_event.set()
        self._refresh()

    def toggle(self, _icon=None, _item=None) -> None:
        with self._lock:
            if self._stopping:
                return
            running = self._running
        self.stop() if running else self.start()
    def open_preview(self, _icon=None, _item=None) -> None:
        path = app.PREVIEW_PATH
        if path.exists():
            os.startfile(str(path))  # noqa: S606 (Windows, opens the image viewer)

    def sync_clock(self, _icon=None, _item=None) -> None:
        """Sync the native status-screen RTC without touching the TFT image."""
        with self._lock:
            if self._clock_syncing:
                return
            self._clock_syncing = True
            self._error = None
        self._refresh()

        def run() -> None:
            try:
                when = sync_keyboard_clock()
            except Exception as error:  # keep the tray alive; surface the reason
                with self._lock:
                    self._error = f"clock sync: {type(error).__name__}: {error}"
            else:
                with self._lock:
                    self._clock_synced_at = when.strftime("%H:%M")
            finally:
                with self._lock:
                    self._clock_syncing = False
                self._refresh()

        threading.Thread(target=run, name="th99-clock-sync", daemon=True).start()

    def quit(self, icon, _item=None) -> None:
        with self._lock:
            stop_event = self._stop
        if stop_event is not None:
            stop_event.set()
        icon.stop()

    # --- menu -----------------------------------------------------------
    def _choice_menu(self, choices, current_getter, setter) -> pystray.Menu:
        items = []
        for seconds, label in choices:
            items.append(
                pystray.MenuItem(
                    label,
                    (lambda s: (lambda icon, item: setter(s)))(seconds),
                    checked=(lambda s: (lambda item: current_getter() == s))(seconds),
                    radio=True,
                )
            )
        return pystray.Menu(*items)

    def run(self) -> None:
        menu = pystray.Menu(
            pystray.MenuItem(self._state_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._toggle_label, self.toggle, default=True),
            pystray.MenuItem(
                "Display layout",
                self._choice_menu(
                    DISPLAY_CHOICES,
                    lambda: self.config.display_mode,
                    self._set_display_mode,
                ),
            ),
            pystray.MenuItem(
                "Usage-check frequency",
                self._choice_menu(
                    POLL_CHOICES, lambda: self.config.poll_seconds, self._set_poll
                ),
            ),
            pystray.MenuItem(
                "Screen update limit",
                self._choice_menu(
                    WRITE_CHOICES, lambda: self.config.min_upload_seconds, self._set_write
                ),
            ),
            pystray.MenuItem(
                "Run at startup", self.toggle_startup, checked=lambda item: startup_enabled()
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Sync keyboard clock now", self.sync_clock),
            pystray.MenuItem("Open preview", self.open_preview),
            pystray.MenuItem("Quit", self.quit),
        )
        self.icon = pystray.Icon(
            "th99_usage",
            icon=_make_image(STOPPED_COLOR),
            title="TH99 Pro: paused",
            menu=menu,
        )
        self.icon.run()


def main() -> int:
    TrayController().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
