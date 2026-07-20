"""Offline checks for usage-percent normalization before TFT change detection."""

from __future__ import annotations

import unittest

from provider_usage_probe import bounded_percent


class BoundedPercentTests(unittest.TestCase):
    def test_fractional_utilization_is_floored_to_the_displayed_percent(self):
        self.assertEqual(bounded_percent(0.1, "test"), 0)
        self.assertEqual(bounded_percent(0.9, "test"), 0)
        self.assertEqual(bounded_percent(1.0, "test"), 1)
        self.assertEqual(bounded_percent(1.99, "test"), 1)
        self.assertEqual(bounded_percent(99.99, "test"), 99)
        self.assertEqual(bounded_percent(100.0, "test"), 100)

    def test_out_of_range_and_non_numeric_values_are_rejected(self):
        for value in (-0.01, 100.01, float("nan"), float("inf"), True, "1"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    bounded_percent(value, "test")


if __name__ == "__main__":
    unittest.main()