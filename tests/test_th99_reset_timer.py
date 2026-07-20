"""Offline regression tests for the reset-timer display mode."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from th99_four_bar_renderer import (
    DISPLAY_MODE_PROGRESS_BAR,
    DISPLAY_MODE_RESET_TIMER,
    HEIGHT,
    WIDTH,
    render_pixels,
)
from provider_usage_probe import ProviderUsage, UsageWindow
from th99_live_usage import Watcher, display_guard_tuple, format_reset_timer


class ResetTimerFormattingTests(unittest.TestCase):
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)

    def test_epoch_seconds_milliseconds_and_iso_are_normalized(self):
        reset_at = self.now + timedelta(days=1, hours=3, minutes=9)
        expected = "1D 03H 09M"
        self.assertEqual(format_reset_timer(int(reset_at.timestamp()), now=self.now), expected)
        self.assertEqual(
            format_reset_timer(int(reset_at.timestamp() * 1000), now=self.now), expected
        )
        self.assertEqual(format_reset_timer(reset_at.isoformat(), now=self.now), expected)

    def test_unknown_and_elapsed_resets_are_safe(self):
        self.assertIsNone(format_reset_timer(None, now=self.now))
        self.assertIsNone(format_reset_timer("not-a-date", now=self.now))
        self.assertEqual(
            format_reset_timer(int((self.now - timedelta(seconds=1)).timestamp()), now=self.now),
            "0D 00H 00M",
        )


class ResetTimerRendererTests(unittest.TestCase):
    def test_reset_layout_accepts_unavailable_windows(self):
        values = (38, 64, None, 51)
        timer_pixels = render_pixels(
            *values,
            display_mode=DISPLAY_MODE_RESET_TIMER,
            reset_timers=("0D 01H 47M", "4D 08H 09M", None, "1D 08H 09M"),
        )
        progress_pixels = render_pixels(*values, display_mode=DISPLAY_MODE_PROGRESS_BAR)
        self.assertEqual(len(timer_pixels), WIDTH * HEIGHT)
        self.assertNotEqual(timer_pixels, progress_pixels)


class DisplayGuardTests(unittest.TestCase):
    def test_reset_timer_guard_does_not_include_countdown_text(self):
        values = (38, 64, None, 51)
        self.assertEqual(
            display_guard_tuple(values, DISPLAY_MODE_RESET_TIMER),
            (DISPLAY_MODE_RESET_TIMER, 38, 64, None, 51),
        )
        self.assertNotEqual(
            display_guard_tuple(values, DISPLAY_MODE_RESET_TIMER),
            display_guard_tuple(values, DISPLAY_MODE_PROGRESS_BAR),
        )


class ResetTimerWatcherTests(unittest.TestCase):
    def test_unchanged_percentages_do_not_reformat_or_render_timer(self):
        window = {
            "claude": ProviderUsage(
                "test",
                UsageWindow(True, 38, 300, "2026-07-19T13:47:00Z"),
                UsageWindow(True, 64, 10080, "2026-07-23T20:09:00Z"),
            ),
            "codex": ProviderUsage(
                "test",
                UsageWindow(False, None, 300, None),
                UsageWindow(True, 51, 10080, "2026-07-20T20:09:00Z"),
            ),
        }
        watcher = Watcher(execute_upload=True, display_mode=DISPLAY_MODE_RESET_TIMER)
        with (
            patch("th99_live_usage.collect_usage", return_value=(window, {})),
            patch(
                "th99_live_usage.reset_timers_from_providers",
                return_value=("0D 01H 47M", "4D 08H 09M", None, "1D 08H 09M"),
            ) as reset_timers,
            patch("th99_live_usage.build_display_reports", return_value=(b"", b"", [])) as build,
            patch("th99_live_usage.write_preview") as preview,
            patch("th99_live_usage.upload_reports") as upload,
        ):
            watcher.run_cycle()
            watcher.run_cycle()

        reset_timers.assert_called_once_with(window)
        build.assert_called_once()
        preview.assert_called_once()
        upload.assert_called_once()

if __name__ == "__main__":
    unittest.main()