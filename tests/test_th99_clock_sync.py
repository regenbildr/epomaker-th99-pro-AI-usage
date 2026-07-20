"""Offline checks for the narrowly-scoped TH99 ``AA 34`` clock command."""

from __future__ import annotations

from datetime import datetime
import unittest

from th99_clock_sync import build_set_clock_packet, validate_set_clock_packet


class ClockPacketTests(unittest.TestCase):
    def test_builds_observed_layout_with_plain_binary_fields(self):
        when = datetime(2026, 7, 18, 21, 20, 38)  # Saturday / ISO weekday 6
        packet = build_set_clock_packet(when)

        self.assertEqual(len(packet), 64)
        self.assertEqual(packet[:8], bytes((0xAA, 0x34, 56, 0, 0, 0, 1, 0)))
        self.assertEqual(
            packet[8:18],
            bytes((0x5A, 0x01, 0x5A, 26, 7, 18, 21, 20, 38, 6)),
        )
        self.assertEqual(packet[18:], b"\x00" * 46)

    def test_response_must_be_an_exact_valid_ack_shape(self):
        request = build_set_clock_packet(datetime(2026, 7, 18, 21, 20, 38))
        response = b"\x55" + request[1:]
        validate_set_clock_packet(response, prefix=0x55)

    def test_rejects_wrong_weekday(self):
        packet = bytearray(build_set_clock_packet(datetime(2026, 7, 18, 21, 20, 38)))
        packet[17] = 7
        with self.assertRaisesRegex(ValueError, "weekday"):
            validate_set_clock_packet(bytes(packet), prefix=0xAA)

    def test_rejects_year_outside_the_captured_range(self):
        with self.assertRaisesRegex(ValueError, "2000"):
            build_set_clock_packet(datetime(2100, 1, 1))


if __name__ == "__main__":
    unittest.main()