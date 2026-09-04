"""Dispatch-form helpers from app.js (new-run-logic) under node, plus the picker dialog's document conformance."""
import json
import re
import shutil
import unittest
from pathlib import Path

from tests.test_ui_js import run_node, slice_section

UI = Path(__file__).resolve().parent.parent / "orchestra" / "ui"


class NewRunLogicTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("node") is None:
            self.skipTest("node is unavailable")
        self.prelude = slice_section("constants") + slice_section("new-run-logic")

    def evaluate(self, expression: str):
        return run_node(self.prelude + f"\nconsole.log(JSON.stringify({expression}));")

    def test_profile_option_label(self):
        full = {"slug": "worker", "provider": "fake", "model": "model", "effort": "low", "enabled": True}
        self.assertEqual(self.evaluate(f"profileOptionLabel({json.dumps(full)})"), "worker · fake/model · low")
        off = dict(full, effort=None, enabled=False)
        self.assertEqual(self.evaluate(f"profileOptionLabel({json.dumps(off)})"), "worker · fake/model (disabled)")

    def test_dispatch_summary(self):
        cases = {
            '{profile:"w",cwd:"/r",branch:"main"}': {"text": "Will run w in /r (git: main)", "bad": False},
            '{profile:"w",cwd:"/r",branch:null}': {"text": "Will run w in /r", "bad": False},
            '{profile:"w",cwd:""}': {"text": "Will run w in the daemon's working directory", "bad": False},
            '{profile:"",cwd:"/r"}': {"text": "Choose a profile.", "bad": False},
            '{profile:"w",cwd:"/r",branch:"main",error:"nope"}': {"text": "nope", "bad": True},
        }
        for expression, expected in cases.items():
            self.assertEqual(self.evaluate(f"dispatchSummary({expression})"), expected, expression)

    def test_path_crumbs(self):
        self.assertEqual(self.evaluate('pathCrumbs("/a/b")'),
                         [{"name": "/", "path": "/"}, {"name": "a", "path": "/a"}, {"name": "b", "path": "/a/b"}])
        self.assertEqual(self.evaluate('pathCrumbs("/")'), [{"name": "/", "path": "/"}])


class NewRunDocumentTests(unittest.TestCase):
    def setUp(self):
        self.html = (UI / "index.html").read_text(encoding="utf-8")
        self.js = (UI / "app.js").read_text(encoding="utf-8")

    def test_dispatch_form_layout(self):
        form = self.html.split('data-form="dispatch"', 1)[1].split("</form>", 1)[0]
        self.assertLess(form.index('name="objective"'), form.index('name="profile"'), "objective comes first")
        self.assertIn('data-action="browse-cwd"', form)
        self.assertIn('class="dispatch-summary', form)
        self.assertNotIn("<details class=\"advanced\" open", form)

    def test_picker_dialog_and_actions(self):
        self.assertIn('<dialog id="dir-picker"', self.html)
        for node in ("dir-picker-crumbs", "dir-picker-list", "dir-picker-state"):
            self.assertIn(f'id="{node}"', self.html)
        for action in ("browse-cwd", "picker-up", "picker-use", "picker-enter", "picker-go"):
            self.assertIn(f'"{action}"', self.js, f"ACTIONS is missing {action}")
        self.assertIn("/api/host-directories?path=", self.js)
        self.assertEqual(len(re.findall(r"^// --- new-run-form ---$", self.js, flags=re.M)), 1)
        self.assertIn("<!-- new-run-form dialogs -->", self.html)
        self.assertIn("/* new-run-form */", (UI / "app.css").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
