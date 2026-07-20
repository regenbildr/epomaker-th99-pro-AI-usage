"""Regression tests for the confirmed 256-byte TH99 TFT metadata layout."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import unittest

from th99_tft_container import build_container, parse_container
from th99_tft_protocol import capture_upload


CAPTURES = Path(__file__).resolve().parent.parent / "data" / "captures"
_FIXTURES = ["th99-upload.pcap", "th99-official-2frame-upload.pcap"]


@unittest.skipUnless(
    all((CAPTURES / name).exists() for name in _FIXTURES),
    "capture fixtures not included in the repo",
)
class ContainerV4Tests(unittest.TestCase):
    def captured_payload(self, name: str) -> bytes:
        return capture_upload(CAPTURES / name)[2]

    def test_official_two_frame_capture_rebuilds_exactly(self):
        payload = self.captured_payload("th99-official-2frame-upload.pcap")
        parsed = parse_container(payload)
        self.assertEqual(len(parsed.frames), 2)
        self.assertEqual(parsed.timings, b"\x32")
        self.assertEqual(len(parsed.padding), 3840)
        self.assertEqual(build_container(list(parsed.frames), parsed.timings), payload)

    def test_official_frames_are_solid_black_and_white(self):
        parsed = parse_container(
            self.captured_payload("th99-official-2frame-upload.pcap")
        )
        self.assertEqual(Counter(parsed.frames[0]), Counter({0: len(parsed.frames[0])}))
        self.assertEqual(Counter(parsed.frames[1]), Counter({255: len(parsed.frames[1])}))

    def test_original_180_frame_capture_rebuilds_exactly(self):
        payload = self.captured_payload("th99-upload.pcap")
        parsed = parse_container(payload)
        self.assertEqual(len(parsed.frames), 180)
        self.assertEqual(parsed.timings, b"\x0F" * 179)
        self.assertEqual(len(parsed.padding), 3840)
        self.assertEqual(build_container(list(parsed.frames), parsed.timings), payload)

    def test_metadata_is_256_bytes_not_4096(self):
        payload = self.captured_payload("th99-official-2frame-upload.pcap")
        parsed = parse_container(payload)
        self.assertEqual(len(parsed.metadata), 256)
        self.assertEqual(parsed.metadata[:3], b"\x02\x32\x00")
        self.assertEqual(set(parsed.metadata[3:]), {255})


if __name__ == "__main__":
    unittest.main()
