"""Pure evidence helpers from app.js, sliced by sentinel and run under node."""
import json
import shutil
import unittest

from tests.test_ui_js import run_node, slice_section


class EvidenceLogicTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("node") is None:
            self.skipTest("node is unavailable")
        self.prelude = "".join(slice_section(name) for name in ("constants", "fmt", "logic", "evidence-logic"))

    def evaluate(self, expression: str) -> dict:
        return run_node(self.prelude + f"\nconsole.log(JSON.stringify({expression}));")

    def test_absorb_history_seeds_cursor_then_prepends_pages(self):
        value = self.evaluate("""(() => {
          const detail = { events: [], eventsAfter: 0, historyFloor: null, historyDone: false };
          const page = (from, to) => Array.from({ length: from - to + 1 }, (_, i) => ({ id: from - i, type: "t" }));
          absorbHistory(detail, page(900, 401));
          const first = { after: detail.eventsAfter, floor: detail.historyFloor, done: detail.historyDone, head: detail.events[0].id };
          absorbHistory(detail, page(400, 1));
          const second = { after: detail.eventsAfter, floor: detail.historyFloor, done: detail.historyDone, ids: detail.events.map((e) => e.id) };
          const empty = { events: [], eventsAfter: 0, historyFloor: null, historyDone: false };
          absorbHistory(empty, []);
          return { first, second, empty };
        })()""")
        self.assertEqual(value["first"], {"after": 900, "floor": 401, "done": False, "head": 401})
        self.assertEqual((value["second"]["after"], value["second"]["floor"], value["second"]["done"]), (900, 1, True))
        self.assertEqual(value["second"]["ids"], list(range(1, 901)))
        self.assertEqual(value["empty"], {"events": [], "eventsAfter": 0, "historyFloor": 0, "historyDone": True})

    def test_raw_event_rows_filter_and_cap_newest_first(self):
        events = [{"id": i, "type": "acp.tool_call" if i % 2 else "dsh.message"} for i in range(1, 1202)]
        value = self.evaluate(f"""(() => {{
          const events = {json.dumps(events)};
          const all = rawEventRows(events, "");
          const tools = rawEventRows(events, " TOOL ");
          return {{ all: [all.rows.length, all.matched, all.rows[0].id, all.rows.at(-1).id], tools: [tools.rows.length, tools.matched, tools.rows[0].id] }};
        }})()""")
        self.assertEqual(value["all"], [500, 1201, 1201, 702])
        self.assertEqual(value["tools"], [500, 601, 1201])

    def test_preview_plan_by_media_type_and_size(self):
        value = self.evaluate("""({
          text: previewPlan({ media_type: "text/plain", byte_size: 10 }),
          json: previewPlan({ media_type: "application/json", byte_size: 262144 }),
          image: previewPlan({ media_type: "image/png", byte_size: 2 * 1024 * 1024 }),
          big_text: previewPlan({ media_type: "text/plain", byte_size: 262145 }).note,
          big_image: previewPlan({ media_type: "image/png", byte_size: 3 * 1024 * 1024 }).note,
          binary: previewPlan({ media_type: "application/octet-stream", byte_size: 1 }),
          pruned: previewPlan({ media_type: "text/plain", byte_size: 1, available: false }),
        })""")
        self.assertEqual(value["text"], {"kind": "text"})
        self.assertEqual(value["json"], {"kind": "text"})
        self.assertEqual(value["image"], {"kind": "image"})
        self.assertIn("256.0 KB", value["big_text"])
        self.assertIn("2.0 MB", value["big_image"])
        self.assertIsNone(value["binary"])
        self.assertIsNone(value["pruned"])


if __name__ == "__main__":
    unittest.main()
