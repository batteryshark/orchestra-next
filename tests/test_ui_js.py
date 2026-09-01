"""Pure app.js logic, sliced by sentinel and run under node."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "orchestra" / "ui" / "app.js"

DIFF_FIXTURE = """diff --git a/alpha.py b/alpha.py
index 111..222 100644
--- a/alpha.py
+++ b/alpha.py
@@ -1,2 +1,3 @@
 keep
-old
+new
+extra
diff --git a/beta.py b/beta.py
new file mode 100644
--- /dev/null
+++ b/beta.py
@@ -0,0 +1 @@
+created
"""


def slice_section(name: str) -> str:
    source = APP_JS.read_text(encoding="utf-8")
    start, end = f"// --- {name} ---", f"// --- end {name} ---"
    return source.split(start, 1)[1].split(end, 1)[0]


def run_node(script: str) -> dict:
    result = subprocess.run(["node", "--input-type=module", "-e", script],
                            capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise AssertionError(f"node failed: {result.stderr}")
    return json.loads(result.stdout)


class UiLogicTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("node") is None:
            self.skipTest("node is unavailable")
        self.prelude = slice_section("constants") + slice_section("fmt") + slice_section("logic")

    def evaluate(self, expression: str) -> dict:
        script = self.prelude + f"\nconsole.log(JSON.stringify({expression}));"
        return run_node(script)

    def test_formatters(self):
        value = self.evaluate("""({
          tokens_small: fmt.tokens(9999),
          tokens_compact: fmt.tokens(41200),
          tokens_millions: fmt.tokens(3100000),
          duration: fmt.duration(3725),
          bytes: fmt.bytes(1536),
          excerpt: fmt.excerpt("first line\\nsecond", 100),
          countdown_expired: fmt.countdown("2000-01-01T00:00:00+00:00"),
        })""")
        self.assertEqual(value["tokens_small"], "9,999")
        self.assertEqual(value["tokens_compact"], "41.2k")
        self.assertEqual(value["tokens_millions"], "3.1M")
        self.assertEqual(value["duration"], "1h 02m")
        self.assertEqual(value["bytes"], "1.5 KB")
        self.assertEqual(value["excerpt"], "first line")
        self.assertEqual(value["countdown_expired"], "expired")

    def test_runs_query_builds_server_filters(self):
        value = self.evaluate("""({
          none: runsQuery({status: new Set(), group: "", profile: "", text: "x"}),
          failed: runsQuery({status: new Set(["failed"]), group: "", profile: ""}),
          ids: runsQuery({status: new Set(["running", "queued"]), group: "g-7", profile: "p-3", strategy: "ralph"}),
          before: runsQuery({status: new Set(), group: "", profile: ""}, {before: 41, limit: 50}),
          strategy_client_side: filterRuns([{strategy: "goal", status: "running"}, {strategy: "ralph", status: "running"}],
            {status: new Set(), group: "", profile: "", text: "", strategy: "ralph"}).length,
        })""")
        self.assertEqual(value["none"], "order=desc&limit=200")
        self.assertEqual(value["failed"], "order=desc&limit=200&status=failed,timed_out")
        self.assertEqual(value["ids"], "order=desc&limit=200&status=starting,running,queued&group=g-7&profile=p-3")
        self.assertEqual(value["before"], "order=desc&limit=50&before=41")
        self.assertEqual(value["strategy_client_side"], 1)

    def test_merge_thread_orders_and_keys(self):
        value = self.evaluate("""mergeThread(
          [{id: 2, created_at: "2026-01-01T00:00:02+00:00", type: "b"},
           {id: 1, created_at: "2026-01-01T00:00:02+00:00", type: "a"}],
          [{message_id: "msg-1", created_at: "2026-01-01T00:00:01+00:00", kind: "tell"}]
        ).map(entry => entry.key)""")
        self.assertEqual(value, ["mmsg-1", "e1", "e2"])

    def test_parse_diff_splits_files_and_counts(self):
        value = self.evaluate(f"parseDiff({json.dumps(DIFF_FIXTURE)})")
        self.assertEqual([f["path"] for f in value["files"]], ["alpha.py", "beta.py"])
        self.assertEqual((value["files"][0]["add"], value["files"][0]["del"]), (2, 1))
        self.assertEqual((value["files"][1]["add"], value["files"][1]["del"]), (1, 0))
        classes = {line["text"][:3]: line["cls"] for line in value["files"][0]["lines"]}
        self.assertEqual(classes.get("+++"), "d-meta")
        self.assertEqual(classes.get("@@ "), "d-hunk")
        self.assertEqual(classes.get("+ne"), "d-add")
        self.assertEqual(classes.get("-ol"), "d-del")

    def test_parse_diff_without_git_header(self):
        value = self.evaluate("parseDiff('plain patch text')")
        self.assertEqual(value["files"][0]["path"], "patch")
        self.assertEqual(value["files"], value["files"])

    def test_aggregate_usage_groups_by_route_epoch_descendant(self):
        rows = [
            {"provider": "p", "model": "m", "cache_epoch": 0, "descendant_session_id": None,
             "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 1, "cache_write_tokens": 2, "total_tokens": 18},
            {"provider": "p", "model": "m", "cache_epoch": 0, "descendant_session_id": None,
             "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0, "cache_write_tokens": 0, "total_tokens": 15},
            {"provider": "p", "model": "m", "cache_epoch": 1, "descendant_session_id": "d1",
             "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0, "cache_write_tokens": 0, "total_tokens": 2},
        ]
        value = self.evaluate(f"aggregateUsage({json.dumps(rows)})")
        self.assertEqual(len(value["groups"]), 2)
        self.assertEqual(value["groups"][0]["input"], 20)
        self.assertEqual(value["groups"][0]["events"], 2)
        self.assertEqual(value["totals"]["total"], 35)

    def test_filter_runs_by_status_group_and_text(self):
        runs = [
            {"id": 1, "status": "running", "group_id": "g1", "profile_id": "p1", "slug": "one", "title": "Fix auth", "objective": ""},
            {"id": 2, "status": "failed", "group_id": "g2", "profile_id": "p1", "slug": "two", "title": "", "objective": "Broken build"},
            {"id": 3, "status": "timed_out", "group_id": "g1", "profile_id": "p2", "slug": "three", "title": "", "objective": ""},
        ]
        base = json.dumps(runs)
        failed = self.evaluate(f"filterRuns({base}, {{status: new Set(['failed']), group: '', profile: '', text: ''}}).map(r => r.id)")
        self.assertEqual(failed, [2, 3])
        grouped = self.evaluate(f"filterRuns({base}, {{status: new Set(), group: 'g1', profile: '', text: ''}}).map(r => r.id)")
        self.assertEqual(grouped, [1, 3])
        text = self.evaluate(f"filterRuns({base}, {{status: new Set(), group: '', profile: '', text: 'broken'}}).map(r => r.id)")
        self.assertEqual(text, [2])

    def test_run_signature_tracks_meaningful_change(self):
        base = {"revision": 1, "updated_at": "t", "status": "running", "waiting_kind": None,
                "tokens_total": 5, "active_seconds": 9, "rounds_started": 1}
        changed = dict(base, tokens_total=6)
        value = self.evaluate(f"runSignature({json.dumps(base)}) === runSignature({json.dumps(changed)})")
        self.assertFalse(value)

    def test_message_parts_split_reasoning_from_text(self):
        payload = {"message": {"role": "assistant", "content": [
            {"type": "reasoning", "text": "think"}, {"type": "tool-call", "id": "c1", "name": "read"},
            {"type": "text", "text": "answer"}]}}
        value = self.evaluate(f"messageParts({json.dumps(payload)})")
        self.assertEqual(value, {"reasoning": ["think"], "text": ["answer"]})
        only_tools = self.evaluate("messageParts({message: {content: [{type: 'tool-call'}]}})")
        self.assertEqual(only_tools, {"reasoning": [], "text": []})

    def test_prompt_source_separates_operator_prompts_from_plugin_snapshots(self):
        user = self.evaluate("promptSource({content: [], source: {kind: 'user'}})")
        self.assertEqual(user, {"kind": "user"})
        legacy = self.evaluate("promptSource({content: []})")
        self.assertEqual(legacy["kind"], "user")
        plugin = self.evaluate("promptSource({source: {kind: 'plugin', plugin: '@deepseek-ai/dsh-system-prompt', form: 'snapshot', sections: [{name: 'sandbox:policy', text: 'x'}]}})")
        self.assertEqual((plugin["kind"], plugin["plugin"], plugin["sections"][0]["name"]), ("context", "@deepseek-ai/dsh-system-prompt", "sandbox:policy"))

    def test_extract_text_handles_content_shapes(self):
        value = self.evaluate("""({
          plain: extractText("hello"),
          text: extractText({text: "a"}),
          array: extractText({content: [{text: "a"}, "b"]}),
          nothing: extractText({foo: 1}),
        })""")
        self.assertEqual(value, {"plain": "hello", "text": "a", "array": "a\nb", "nothing": ""})

    def test_ui_state_round_trips_and_tolerates_malformed_input(self):
        value = self.evaluate("""(() => {
          const state = { filters: { status: new Set(["failed", "running"]), group: "g1", profile: "7", text: "auth", strategy: "swarm" },
                          ui: { follow: false, machine: true, section: "usage" } };
          const back = uiStateDecode(uiStateEncode(state));
          const bad = [uiStateDecode(null), uiStateDecode("{not json"), uiStateDecode('{"filters":{"status":"x","group":3},"section":"nope","machine":"yes"}')];
          return {
            key: UI_STATE_KEY,
            raw: JSON.parse(uiStateEncode(state)),
            back: { ...back, filters: { ...back.filters, status: [...back.filters.status] } },
            no_strategy: "strategy" in uiStateDecode(uiStateEncode({ filters: { status: new Set(), group: "", profile: "", text: "" }, ui: {} })).filters,
            bad: bad.map((b) => ({ ...b, filters: { ...b.filters, status: [...b.filters.status] } })),
          };
        })()""")
        self.assertEqual(value["key"], "orchestra-next.ui")
        self.assertNotIn("follow", value["raw"])
        self.assertEqual(value["raw"]["filters"]["strategy"], "swarm")
        self.assertEqual(value["back"], {"filters": {"status": ["failed", "running"], "group": "g1", "profile": "7", "text": "auth", "strategy": "swarm"},
                                         "machine": True, "section": "usage"})
        self.assertFalse(value["no_strategy"])
        defaults = {"filters": {"status": [], "group": "", "profile": "", "text": ""}, "machine": False, "section": "activity"}
        self.assertEqual(value["bad"], [defaults] * 3)


if __name__ == "__main__":
    unittest.main()
