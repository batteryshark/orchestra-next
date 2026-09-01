"""Pure routing helpers from app.js, sliced by sentinel and run under node."""
import json
import shutil
import unittest

from tests.test_ui_js import run_node, slice_section

MODELS = [
    {"provider": "fake", "model": "model", "efforts": ["high", "low"]},
    {"provider": "openrouter", "model": "org/deep", "efforts": []},
]


class RoutingLogicTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("node") is None:
            self.skipTest("node is unavailable")
        self.prelude = slice_section("routing-logic") + f"\nconst MODELS = {json.dumps(MODELS)};"

    def evaluate(self, expression: str):
        return run_node(self.prelude + f"\nconsole.log(JSON.stringify({expression}));")

    def test_route_efforts_follow_the_chosen_model(self):
        self.assertEqual(self.evaluate("routeEfforts(MODELS, routeKey('fake', 'model'))"), ["high", "low"])
        self.assertEqual(self.evaluate("routeEfforts(MODELS, routeKey('openrouter', 'org/deep'))"), [])
        self.assertEqual(self.evaluate("routeEfforts(MODELS, routeKey('fake', 'other'))"), [])
        self.assertEqual(self.evaluate("routeEfforts(undefined, routeKey('fake', 'model'))"), [])

    def test_reroute_body_keeps_slashes_and_nulls_blank_fields(self):
        value = self.evaluate("rerouteBody(routeKey('openrouter', 'org/deep'), '', '  ')")
        self.assertEqual(value, {"provider": "openrouter", "model": "org/deep", "effort": None, "message": None})
        value = self.evaluate("rerouteBody(routeKey('fake', 'model'), 'high', ' go on ')")
        self.assertEqual(value, {"provider": "fake", "model": "model", "effort": "high", "message": "go on"})


if __name__ == "__main__":
    unittest.main()
