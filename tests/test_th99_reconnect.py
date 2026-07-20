"""Offline reconnection behavior for the live TH99 watcher."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from provider_usage_probe import ProviderUsage, UsageWindow
from th99_live_usage import TftDeviceUnavailable, Watcher


def _providers() -> dict[str, ProviderUsage]:
    return {
        "claude": ProviderUsage(
            "test",
            UsageWindow(True, 21, 300, None),
            UsageWindow(True, 42, 10080, None),
        ),
        "codex": ProviderUsage(
            "test",
            UsageWindow(False, None, 300, None),
            UsageWindow(True, 63, 10080, None),
        ),
    }


class ReconnectWatcherTests(unittest.TestCase):
    def test_disconnect_then_reconnect_forces_one_upload_with_same_values(self):
        statuses: list[dict] = []
        watcher = Watcher(
            execute_upload=True,
            min_upload_seconds=900,
            on_status=statuses.append,
        )
        with (
            patch("th99_live_usage.collect_usage", return_value=(_providers(), {})),
            patch(
                "th99_live_usage.find_display_path",
                side_effect=["mi_03", TftDeviceUnavailable("absent"), "mi_03"],
            ),
            patch("th99_live_usage.build_display_reports", return_value=(b"", b"", [])),
            patch("th99_live_usage.write_preview"),
            patch("th99_live_usage.upload_reports") as upload,
        ):
            watcher.run_cycle()
            watcher.run_cycle()
            watcher.run_cycle()

        self.assertEqual(upload.call_count, 2)
        self.assertEqual(statuses[1]["device_status"], "disconnected")
        self.assertEqual(statuses[2]["device_status"], "connected")

    def test_transient_upload_error_retries_without_ending_the_watcher(self):
        statuses: list[dict] = []
        watcher = Watcher(
            execute_upload=True,
            min_upload_seconds=900,
            on_status=statuses.append,
        )
        with (
            patch("th99_live_usage.collect_usage", return_value=(_providers(), {})),
            patch("th99_live_usage.find_display_path", return_value="mi_03"),
            patch("th99_live_usage.build_display_reports", return_value=(b"", b"", [])),
            patch("th99_live_usage.write_preview"),
            patch(
                "th99_live_usage.upload_reports", side_effect=[OSError("gone"), None]
            ) as upload,
        ):
            watcher.run_cycle()
            watcher.run_cycle()
            self.assertEqual(upload.call_count, 1)
            self.assertEqual(statuses[1]["device_status"], "reconnecting")

            # Simulate the next eligible recovery interval without sleeping.
            watcher._last_recovery_attempt_time -= watcher.min_upload_seconds
            watcher.run_cycle()

        self.assertEqual(upload.call_count, 2)
        self.assertEqual(statuses[0]["device_status"], "reconnecting")
        self.assertEqual(statuses[2]["device_status"], "connected")

    def test_unexpected_protocol_error_is_not_treated_as_a_disconnect(self):
        watcher = Watcher(execute_upload=True)
        with (
            patch("th99_live_usage.collect_usage", return_value=(_providers(), {})),
            patch("th99_live_usage.find_display_path", return_value="mi_03"),
            patch("th99_live_usage.build_display_reports", return_value=(b"", b"", [])),
            patch("th99_live_usage.write_preview"),
            patch("th99_live_usage.upload_reports", side_effect=ValueError("bad ACK")),
        ):
            with self.assertRaisesRegex(ValueError, "bad ACK"):
                watcher.run_cycle()


if __name__ == "__main__":
    unittest.main()