"""Offline regression tests for the corrected TH99 keymap restore path."""

from __future__ import annotations

import unittest
from pathlib import Path

from th99_keymap_protocol import (
    READ_TO_WRITE,
    build_table_packets,
    nonzero_record_indices,
    record,
    sha256,
    validate_packet,
)
from th99_keymap_restore import (
    KNOWN_TARGET_TABLE_HASHES,
    KNOWN_WRITE_TABLE_HASHES,
    final_read_tables,
    final_write_transactions,
    validate_target,
)


CAPTURES = Path(__file__).resolve().parent.parent / "data" / "captures"
RESTORE_CAPTURE = CAPTURES / "th99-key-remap.pcap"
VERIFICATION_CAPTURE = CAPTURES / "th99-startup-remapped.pcap"


@unittest.skipUnless(
    RESTORE_CAPTURE.exists() and VERIFICATION_CAPTURE.exists(),
    "keymap capture fixtures not included in the repo",
)
class RestoreCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.writes = final_write_transactions(RESTORE_CAPTURE)
        cls.targets = final_read_tables(VERIFICATION_CAPTURE)

    def test_final_transactions_rebuild_exactly(self):
        for command, (packets, table) in self.writes.items():
            self.assertEqual(packets, build_table_packets(0xAA, command, table))

    def test_reviewed_table_hashes_are_unchanged(self):
        for command, expected in KNOWN_WRITE_TABLE_HASHES.items():
            self.assertEqual(sha256(self.writes[command][1]), expected)
        for command, expected in KNOWN_TARGET_TABLE_HASHES.items():
            self.assertEqual(sha256(self.targets[command]), expected)

    def test_serialized_records_match_verified_target(self):
        validate_target(self.writes, self.targets)
        for read_command, write_command in READ_TO_WRITE.items():
            write_table = self.writes[write_command][1]
            for index in nonzero_record_indices(write_table):
                self.assertEqual(
                    record(write_table, index),
                    record(self.targets[read_command], index),
                )

    def test_header_is_eight_bytes(self):
        packet = self.writes[0x22][0][0]
        length, offset, final = validate_packet(
            packet, prefix=0xAA, command=0x22
        )
        self.assertEqual((length, offset, final), (56, 0, 0))
        self.assertEqual(packet[7], 0)
        self.assertEqual(packet[8:64], self.writes[0x22][1][:56])

    def test_reserved_header_byte_is_rejected(self):
        packet = bytearray(self.writes[0x22][0][0])
        packet[7] = 1
        with self.assertRaisesRegex(ValueError, "reserved header byte"):
            validate_packet(bytes(packet), prefix=0xAA, command=0x22)


if __name__ == "__main__":
    unittest.main()
