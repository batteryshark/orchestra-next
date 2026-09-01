"""Pure config helpers from app.js, sliced by sentinel and run under node."""
import json
import shutil
import unittest

from tests.test_ui_js import run_node, slice_section

MODELS = [{"provider": "fake", "model": "model", "efforts": ["high", "low"]},
          {"provider": "other", "model": "big", "efforts": []}]
ORIGINAL = {"id": "p1", "slug": "worker", "name": "Worker", "provider": "fake", "model": "model", "effort": "low",
            "tier": 2, "max_concurrency": None, "note": None, "revision": 3}


class ConfigLogicTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("node") is None:
            self.skipTest("node is unavailable")
        self.prelude = slice_section("constants") + slice_section("config-logic")

    def evaluate(self, expression: str):
        return run_node(self.prelude + f"\nconsole.log(JSON.stringify({expression}));")

    def test_profile_body_normalizes_blank_fields(self):
        raw = {"name": " Worker ", "slug": "", "provider": "fake", "model": "model", "effort": "",
               "tier": "2", "max_concurrency": "", "note": ""}
        value = self.evaluate(f"profileBody({json.dumps(raw)})")
        self.assertEqual(value, {"name": "Worker", "provider": "fake", "model": "model", "effort": None,
                                 "tier": 2, "max_concurrency": None, "note": None})
        with_slug = self.evaluate(f"profileBody({json.dumps(dict(raw, slug=' w1 ', max_concurrency='4'))})")
        self.assertEqual((with_slug["slug"], with_slug["max_concurrency"]), ("w1", 4))

    def test_profile_patch_sends_only_changed_fields(self):
        same = {key: ORIGINAL[key] for key in ("name", "provider", "model", "effort", "tier", "max_concurrency", "note")}
        self.assertEqual(self.evaluate(f"profilePatch({json.dumps(ORIGINAL)}, {json.dumps(same)})"), {"expected_revision": 3})
        changed = dict(same, tier=1, effort=None, note="fast")
        self.assertEqual(self.evaluate(f"profilePatch({json.dumps(ORIGINAL)}, {json.dumps(changed)})"),
                         {"expected_revision": 3, "tier": 1, "effort": None, "note": "fast"})

    def test_group_patch_carries_one_field(self):
        self.assertEqual(self.evaluate("groupPatch('cwd', null, 5)"), {"expected_revision": 5, "cwd": None})
        self.assertEqual(self.evaluate("groupPatch('archived', true, 1)"), {"expected_revision": 1, "archived": True})

    def test_model_efforts_filters_to_the_chosen_route(self):
        models = json.dumps(MODELS)
        self.assertEqual(self.evaluate(f"modelEfforts({models}, 'fake', 'model')"), ["high", "low"])
        self.assertEqual(self.evaluate(f"modelEfforts({models}, 'fake', 'missing')"), [])
        self.assertEqual(self.evaluate("modelEfforts([], 'fake', 'model')"), [])


if __name__ == "__main__":
    unittest.main()
