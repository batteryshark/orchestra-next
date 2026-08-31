"""The dashboard's own QR encoder, run from the file the browser is served.

Pairing shows a one-time code as a QR so an operator does not retype it. The
encoder is hand-written because Orchestra ships no dependencies, so it is
pinned here: the expected matrices were produced once by an encoder whose
bitmaps macOS Vision decoded back byte for byte, across every version it
supports and multi-byte UTF-8.
"""
import hashlib
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

DASHBOARD = Path(__file__).parent.parent / "orchestra" / "dashboard.html"
START = "// --- QR encoder"
END = "// --- end QR encoder ---"


def encoder_source() -> str:
    text = DASHBOARD.read_text(encoding="utf-8")
    body = text[text.index(START):text.index(END)]
    return body


class DashboardQREncoderTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")

    def encode(self, values: list[str]) -> list[list[str]]:
        script = encoder_source() + (
            "\nconst out=JSON.parse(process.argv[1]).map("
            "text=>qrMatrix(text).map(row=>row.join('')));"
            "\nconsole.log(JSON.stringify(out));")
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script, json.dumps(values)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_every_supported_version_matches_the_decoded_recording(self):
        cases = json.loads(
            (Path(__file__).parent / "fixtures_qr.json").read_text())
        matrices = self.encode([case["text"] for case in cases])
        for case, matrix in zip(cases, matrices):
            with self.subTest(text=case["text"][:16]):
                self.assertEqual(len(matrix), case["size"])
                digest = hashlib.sha256(
                    "/".join(matrix).encode()).hexdigest()
                self.assertEqual(digest, case["sha256"])

    def test_a_payload_beyond_version_ten_is_refused_not_truncated(self):
        script = encoder_source() + (
            "\ntry{qrMatrix('A'.repeat(400));console.log('NO ERROR')}"
            "catch(e){console.log('REFUSED')}")
        result = subprocess.run(["node", "--input-type=module", "-e", script],
                                capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "REFUSED", result.stderr)


if __name__ == "__main__":
    unittest.main()
