"""Delegation row strings, sliced from app.js and run under node."""
import json
import shutil
import unittest

from tests.test_ui_js import run_node, slice_section


class DelegationSummaryTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("node") is None:
            self.skipTest("node is unavailable")
        self.prelude = slice_section("constants") + slice_section("fmt") + slice_section("logic")

    def evaluate(self, expression):
        return run_node(self.prelude + f"\nconsole.log(JSON.stringify({expression}));")

    def test_success_row_shows_turn_delta_and_conversation(self):
        payload = {"model": "gemini-3.8-flash-low", "status": "SUCCESS", "num_turns": 2, "duration_seconds": 1.62,
                   "usage": {"total": 29312}, "delta": {"total": 14718}}
        value = self.evaluate(f"delegationSummary({json.dumps(payload)})")
        self.assertTrue(value["ok"])
        self.assertEqual(value["title"], "🛰 delegation · gemini-3.8-flash-low · SUCCESS")
        self.assertEqual(value["stats"], "turn 2 · 14.7k tokens this turn · 29.3k conversation · 1.6s")

    def test_error_row_is_not_ok(self):
        value = self.evaluate('delegationSummary({model: "claude-opus-4-6-thinking", status: "ERROR", num_turns: 1, usage: {total: 14532}, delta: {total: 14532}})')
        self.assertFalse(value["ok"])
        self.assertIn("ERROR", value["title"])

    def test_missing_delta_falls_back_to_usage(self):
        value = self.evaluate('delegationSummary({model: "m", status: "SUCCESS", num_turns: 1, usage: {total: 500}})')
        self.assertIn("500 tokens this turn", value["stats"])
        empty = self.evaluate("delegationSummary(null)")
        self.assertFalse(empty["ok"])
        self.assertEqual(empty["title"], "🛰 delegation · ? · ?")


if __name__ == "__main__":
    unittest.main()
