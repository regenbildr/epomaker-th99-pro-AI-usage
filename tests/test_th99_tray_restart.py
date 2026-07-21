"""Offline lifecycle tests for the tray watcher controller."""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from th99_tray_app import TrayController


class _FakeWatcher:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.finished = threading.Event()
        self.stop_event: threading.Event | None = None

    def validate(self) -> None:
        pass

    def run_forever(self, stop_event: threading.Event) -> int:
        self.stop_event = stop_event
        self.started.set()
        stop_event.wait(2)
        self.finished.set()
        return 0


class TrayRestartTests(unittest.TestCase):
    def _wait_until(self, condition, timeout: float = 2) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.01)
        self.fail("condition did not become true before timeout")

    def test_stop_then_restart_uses_a_new_worker_and_ignores_late_status(self):
        watchers: list[_FakeWatcher] = []
        callbacks = []

        def build_watcher(*, on_status, **_kwargs):
            callbacks.append(on_status)
            watcher = _FakeWatcher()
            watchers.append(watcher)
            return watcher

        controller = TrayController()
        with patch("th99_tray_app.app.build_watcher", side_effect=build_watcher):
            controller.start()
            self.assertTrue(watchers[0].started.wait(1))
            controller.stop()
            self.assertEqual(controller._toggle_label(), "Stopping...")

            # A second click while the first worker unwinds cannot start another run.
            controller.start()
            self.assertEqual(len(watchers), 1)

            # Late status from the stopped worker is ignored instead of turning red.
            callbacks[0]({"errors": {"test": "late failure"}})
            self.assertIsNone(controller._error)

            self.assertTrue(watchers[0].finished.wait(1))
            self._wait_until(lambda: not controller.is_running())

            controller.start()
            self.assertTrue(watchers[1].started.wait(1))
            self.assertIsNot(watchers[0].stop_event, watchers[1].stop_event)
            controller.stop()
            self.assertTrue(watchers[1].finished.wait(1))
            self._wait_until(lambda: not controller.is_running())

    def test_provider_error_keeps_the_safe_detail_and_logs_it_once(self):
        controller = TrayController()
        controller._running = True
        status = {"errors": {"claude": "RuntimeError: timed out"}}
        with patch("builtins.print") as output:
            controller._on_status(controller._run_token, status)
            controller._on_status(controller._run_token, status)

        self.assertEqual(controller._error, "claude: RuntimeError: timed out")
        output.assert_called_once()
        self.assertIn("claude: RuntimeError: timed out", output.call_args.args[0])

    def test_tray_values_show_remaining_capacity(self):
        controller = TrayController()
        controller._running = True
        controller._values = (38, 64, None, 51)

        self.assertEqual(
            controller._state_text(),
            "Claude 62%/36%, Codex N/A/49%",
        )

    def test_reconnect_status_is_not_a_tray_error(self):
        controller = TrayController()
        controller._running = True
        controller._on_status(
            controller._run_token,
            {
                "values": (1, 2, None, 4),
                "errors": {},
                "uploaded": False,
                "device_status": "disconnected",
            },
        )

        self.assertIsNone(controller._error)
        self.assertEqual(controller._device_status, "disconnected")
        self.assertIn("disconnected", controller._state_text())


if __name__ == "__main__":
    unittest.main()