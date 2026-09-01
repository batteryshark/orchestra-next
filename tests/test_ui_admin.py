"""Pure admin-console helpers, sliced from app.js by sentinel and run under node."""
import shutil
import unittest

from tests.test_ui_js import run_node, slice_section


class UiAdminTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("node") is None:
            self.skipTest("node is unavailable")
        # The admin block only extends ACTIONS/FORMS at the top level; stubs stand in for the DOM-bound modules.
        self.prelude = (slice_section("constants") + slice_section("fmt")
                        + "\nconst ACTIONS = {}, FORMS = {}, store = {};\n"
                        + slice_section("slice3-admin"))

    def evaluate(self, expression: str):
        return run_node(self.prelude + f"\nconsole.log(JSON.stringify({expression}));")

    def test_plan_totals_counts_items_and_sums_sizes(self):
        value = self.evaluate("planTotals([{size_bytes: 1024}, {size_bytes: 512}, {kind: 'artifact'}])")
        self.assertEqual(value, {"count": 3, "bytes": 1536})
        self.assertEqual(self.evaluate("planTotals([])"), {"count": 0, "bytes": 0})

    def test_admin_block_registers_the_handlers_its_dom_uses(self):
        value = self.evaluate("({actions: Object.keys(ACTIONS).sort(), forms: Object.keys(FORMS)})")
        self.assertEqual(value, {"actions": ["audit-more", "pair-create", "storage-apply"], "forms": ["storage-plan"]})

    def test_pair_route_carries_the_scanned_code(self):
        script = (slice_section("constants") + "\nconst location = {hash: '#/pair/ABCD-EFGH-JKMN'};\n"
                  + slice_section("router") + "\nconsole.log(JSON.stringify(parseHash()));")
        value = run_node(script)
        self.assertEqual(value, {"view": "pair", "runId": None, "section": None, "code": "ABCD-EFGH-JKMN"})


if __name__ == "__main__":
    unittest.main()
