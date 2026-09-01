"""The console's QR encoder, sliced from app.js and pinned to decoded recordings.

The pairing panel shows a one-time code as a QR so an operator does not retype
it on the other device. The encoder ships inside app.js because the console has
no dependencies, so it is pinned here: fixtures_qr.json holds matrices that an
encoder whose bitmaps macOS Vision decoded byte for byte produced once, across
every supported version and multi-byte UTF-8.
"""
import hashlib
import json
import shutil
import unittest
from pathlib import Path

from tests.test_ui_js import run_node, slice_section

FIXTURES = Path(__file__).resolve().parent / "fixtures_qr.json"


class UiQrEncoderTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("node") is None:
            self.skipTest("node is unavailable")
        self.prelude = slice_section("qr")

    def test_every_supported_version_matches_the_decoded_recording(self):
        cases = json.loads(FIXTURES.read_text(encoding="utf-8"))
        texts = json.dumps([case["text"] for case in cases])
        matrices = run_node(self.prelude + f"\nconsole.log(JSON.stringify({texts}.map((text) => qrMatrix(text).map((row) => row.join('')))));")
        self.assertEqual(len(matrices), len(cases))
        for case, matrix in zip(cases, matrices):
            with self.subTest(text=case["text"][:16]):
                self.assertEqual(len(matrix), case["size"])
                self.assertEqual(hashlib.sha256("/".join(matrix).encode()).hexdigest(), case["sha256"])

    def test_a_payload_beyond_version_ten_is_refused_not_truncated(self):
        value = run_node(self.prelude + "\nlet out = 'NO ERROR'; try { qrMatrix('A'.repeat(400)); } catch { out = 'REFUSED'; }\nconsole.log(JSON.stringify(out));")
        self.assertEqual(value, "REFUSED")


if __name__ == "__main__":
    unittest.main()
