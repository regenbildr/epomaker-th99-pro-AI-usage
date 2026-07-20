"""Offline regression tests for the TH99 Pro AA50 TFT uploader."""

from __future__ import annotations

from pathlib import Path
import unittest

from th99_tft_protocol import (
    EXPECTED_ACK,
    FRAME_BYTES,
    REPORT_SIZE,
    build_reports,
    capture_upload,
    inspect_payload,
    make_test_payload,
    sha256,
)
from th99_tft_upload import (
    KNOWN_CAPTURE_PAYLOAD_HASH,
    KNOWN_CAPTURE_REPORT_STREAM_HASH,
    KNOWN_TEST_PAYLOAD_HASH,
    KNOWN_TEST_REPORT_STREAM_HASH,
)


CAPTURES = Path(__file__).resolve().parent.parent / "data" / "captures"
CAPTURE = CAPTURES / "th99-upload.pcap"


@unittest.skipUnless(CAPTURE.exists(), "capture fixture not included in the repo")
class TFTUploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.captured_reports, cls.acks, cls.captured_payload = capture_upload(CAPTURE)
        cls.test_payload = make_test_payload(cls.captured_payload)
        cls.test_reports = build_reports(cls.test_payload)

    def test_captured_upload_rebuilds_exactly(self):
        self.assertEqual(build_reports(self.captured_payload), self.captured_reports)
        self.assertEqual(sha256(self.captured_payload), KNOWN_CAPTURE_PAYLOAD_HASH)
        self.assertEqual(
            sha256(b"".join(self.captured_reports)),
            KNOWN_CAPTURE_REPORT_STREAM_HASH,
        )

    def test_all_captured_acknowledgements_are_exact(self):
        self.assertEqual(len(self.acks), len(self.captured_reports))
        self.assertTrue(all(ack == EXPECTED_ACK for ack in self.acks))

    def test_two_frame_payload_is_small_and_deterministic(self):
        metadata = inspect_payload(self.test_payload)
        self.assertEqual(metadata["frame_count"], 2)
        self.assertEqual(metadata["unique_frames"], 2)
        self.assertEqual(metadata["padding_size"], 0)
        self.assertEqual(len(self.test_payload), 65536)
        self.assertEqual(len(self.test_reports), 16)
        self.assertEqual(sha256(self.test_payload), KNOWN_TEST_PAYLOAD_HASH)
        self.assertEqual(
            sha256(b"".join(self.test_reports)),
            KNOWN_TEST_REPORT_STREAM_HASH,
        )

    def test_test_frames_are_black_then_white(self):
        body = self.test_payload[4096:]
        self.assertEqual(body[:FRAME_BYTES], bytes(FRAME_BYTES))
        self.assertEqual(body[FRAME_BYTES : 2 * FRAME_BYTES], b"\xFF" * FRAME_BYTES)

    def test_report_headers_are_continuous(self):
        for sequence, report in enumerate(self.test_reports):
            self.assertEqual(len(report), REPORT_SIZE)
            self.assertEqual(report[:2], b"\xAA\x50")
            self.assertEqual(int.from_bytes(report[2:4], "little"), sequence)
            self.assertEqual(int.from_bytes(report[4:6], "little"), 16)
            self.assertEqual(report[6:8], b"\x50\x06")


if __name__ == "__main__":
    unittest.main()
