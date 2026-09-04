"""Static conformance of the console document: CSP-safe, sentinel-sliced, wired."""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

UI_DIR = Path(__file__).resolve().parent.parent / "orchestra" / "ui"


class DocumentConformanceTests(unittest.TestCase):
    def setUp(self):
        self.html = (UI_DIR / "index.html").read_text(encoding="utf-8")
        self.js = (UI_DIR / "app.js").read_text(encoding="utf-8")
        self.css = (UI_DIR / "app.css").read_text(encoding="utf-8")

    def test_files_exist_and_are_nonempty(self):
        for name in ("index.html", "app.css", "app.js"):
            self.assertTrue((UI_DIR / name).stat().st_size > 100, name)

    def test_app_js_parses_as_one_module(self):
        if shutil.which("node") is None:
            self.skipTest("node is unavailable")
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "app.mjs"
            target.write_text(self.js, encoding="utf-8")
            result = subprocess.run(["node", "--check", str(target)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_app_js_never_uses_html_injection(self):
        for marker in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            self.assertNotIn(marker, self.js, marker)

    def test_index_has_no_inline_script_style_or_handlers(self):
        self.assertNotRegex(self.html, r"<script(?![^>]*\bsrc=)")
        self.assertNotIn("<style", self.html)
        self.assertNotRegex(self.html, r"\son[a-z]+\s*=", "inline event handler")
        self.assertNotIn("style=", self.html)

    def test_sentinels_are_paired(self):
        starts = re.findall(r"^// --- (?!end )(.+?) ---$", self.js, flags=re.M)
        ends = re.findall(r"^// --- end (.+?) ---$", self.js, flags=re.M)
        self.assertEqual(starts, ends)
        for name in ("constants", "fmt", "logic", "qr"):
            self.assertIn(name, starts)

    def test_every_data_action_in_html_has_a_handler(self):
        actions = set(re.findall(r'data-action="([a-z-]+)"', self.html))
        for action in actions:
            self.assertIn(f'"{action}"', self.js, f"ACTIONS is missing {action}")
        forms = set(re.findall(r'data-form="([a-z-]+)"', self.html))
        for form in forms:
            self.assertIn(f"{form}:", self.js, f"FORMS is missing {form}")

    def test_local_storage_access_is_guarded(self):
        """Every `localStorage` use sits inside a `try`, so a blocked store cannot break the page.

        Limits: this is a line-level check. It accepts a `try` on the same line or on the two
        lines above, and does not parse block structure. It also requires the single key name."""
        lines = self.js.splitlines()
        hits = [i for i, line in enumerate(lines) if "localStorage" in line]
        self.assertTrue(hits, "expected localStorage use")
        for i in hits:
            window = "\n".join(lines[max(0, i - 2):i + 1])
            self.assertRegex(window, r"\btry\b", f"unguarded localStorage at line {i + 1}")
        self.assertIn('"orchestra-next.ui"', self.js)
        self.assertNotRegex(self.js, r'"orchestra\.[a-z]', "V2 key name")

    def test_config_tabs_exist_and_route(self):
        tabs = ["profiles", "groups", "identities", "storage", "audit", "settings", "logs", "diagnostics"]
        for tab in tabs:
            self.assertIn(f'<section id="config-{tab}" data-ctab="{tab}" role="tabpanel" hidden>', self.html, tab)
            self.assertIn(f"<!-- config-tab:{tab} -->", self.html)
            self.assertIn(f"<!-- /config-tab:{tab} -->", self.html)
            self.assertIn(f"// config-tab:{tab}", self.js)
            self.assertIn(f"// /config-tab:{tab}", self.js)
        for name in ("renderConfigSettings", "renderConfigLogs"):
            self.assertIn(f"function {name}()", self.js)
        if shutil.which("node") is None:
            self.skipTest("node is unavailable")
        from tests.test_ui_js import run_node, slice_section
        script = slice_section("constants") + slice_section("router") + """
          globalThis.store = { ui: { section: "activity", configTab: "audit" } };
          const at = (hash) => { globalThis.location = { hash }; return parseHash(); };
          console.log(JSON.stringify({ logs: at("#/config/logs"), last: at("#/config"), bad: at("#/config/nope"), tabs: CONFIG_TABS }));"""
        value = run_node(script)
        self.assertEqual(value["logs"], {"view": "config", "runId": None, "section": "logs"})
        self.assertEqual(value["last"]["section"], "audit")
        self.assertEqual(value["bad"]["section"], "audit")
        self.assertEqual(value["tabs"], tabs)

    def test_dark_scheme_and_reduced_motion_are_declared(self):
        self.assertIn("prefers-color-scheme: dark", self.css)
        self.assertIn("prefers-reduced-motion: reduce", self.css)
        self.assertIn('name="color-scheme"', self.html)
        self.assertIn("scrollbar-gutter: stable", self.css)


if __name__ == "__main__":
    unittest.main()
