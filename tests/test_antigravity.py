"""Antigravity delegation bridge: authorization gate, catalog, delta accounting."""
import contextlib
import io
import json
import os
import threading
import unittest
from pathlib import Path

from orchestra import antigravity, auth, child_runs, cli, dsh, http as transport, runs
from orchestra.contracts import ContractError, RunRequest
from tests.common import StateCase

FAKE = str(Path(__file__).with_name("fake_agy.py"))
EXTRA_ENV = ("ORCHESTRA_NEXT_URL", "ORCHESTRA_NEXT_RUN_ID", "ORCHESTRA_NEXT_AGY", "FAKE_AGY_STATE", "FAKE_AGY_MODE", "GEMINI_API_KEY", "GOOGLE_API_KEY")


class AntigravityCase(StateCase):
    def setUp(self):
        super().setUp()
        self.old_extra = {name: os.environ.get(name) for name in EXTRA_ENV}
        self.create_profile()
        self.server = transport.make_server(addr="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        os.environ["ORCHESTRA_NEXT_URL"] = f"http://127.0.0.1:{self.server.server_address[1]}"
        os.environ["ORCHESTRA_NEXT_AGY"] = FAKE
        os.environ["FAKE_AGY_STATE"] = str(self.root / "agy-state.json")
        os.environ.pop("FAKE_AGY_MODE", None)
        os.environ["GEMINI_API_KEY"] = "must-not-leak"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        for name, value in self.old_extra.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        super().tearDown()

    def run_with(self, request_id, *, allow, workdir=True):
        body = {"request_id": request_id, "profile": "fake", "objective": request_id, "cwd": str(self.root), "allow_antigravity": allow}
        run, _ = runs.submit(self.con, RunRequest.from_mapping(body))
        if workdir:
            (self.root / "wt").mkdir(exist_ok=True)
            with self.con:
                self.con.execute("UPDATE runs SET workdir=? WHERE id=?", (str(self.root / "wt"), run["id"]))
        token = auth.mint_run(self.con, run["id"])
        auth_file = self.root / f"auth-{run['id']}.json"
        auth_file.write_text(json.dumps({"token": token}), encoding="utf-8")
        os.environ["ORCHESTRA_NEXT_RUN_AUTH_FILE"] = str(auth_file)
        os.environ["ORCHESTRA_NEXT_RUN_ID"] = str(run["id"])
        return run["id"]

    def delegate(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["delegate", *argv])
        return code, out.getvalue(), err.getvalue()

    def rows(self, run_id):
        events = [json.loads(row[0]) for row in self.con.execute("SELECT payload_json FROM events WHERE run_id=? AND type='delegation.antigravity' ORDER BY id", (run_id,))]
        usage = [dict(row) for row in self.con.execute("SELECT * FROM usage_events WHERE run_id=? ORDER BY id", (run_id,))]
        return events, usage


class DelegateBridgeTests(AntigravityCase):
    def test_refused_without_allow_antigravity(self):
        run_id = self.run_with("plain", allow=False)
        code, out, err = self.delegate("review it", "--model", "gemini-3.8-flash-low")
        self.assertEqual(code, 1)
        self.assertIn("dispatch with --allow-antigravity", err)
        self.assertEqual(self.rows(run_id), ([], []))

    def test_unknown_model_lists_catalog(self):
        self.run_with("catalog", allow=True)
        code, _, err = self.delegate("review it", "--model", "nope")
        self.assertEqual(code, 1)
        self.assertIn("gemini-3.8-flash-low", err)
        self.assertIn("claude-opus-4-6-thinking", err)
        self.assertEqual(self.delegate("x", "--model", "gemini-3.8-flash-low", "--mode", "write")[0], 1)

    def test_refused_without_worktree(self):
        self.run_with("nowt", allow=True, workdir=False)
        code, _, err = self.delegate("review it", "--model", "gemini-3.8-flash-low")
        self.assertEqual(code, 1)
        self.assertIn("worktree", err)

    def test_success_records_event_and_usage_then_reuses_conversation(self):
        run_id = self.run_with("ok", allow=True)
        before = runs.find(self.con, run_id)["tokens_total"]
        code, out, err = self.delegate("first look", "--model", "gemini-3.8-flash-low")
        self.assertEqual(code, 0, err)
        self.assertIn("turn 1 review of " + str((self.root / "wt").resolve()), out)
        summary = json.loads(err.strip().splitlines()[-1])["delegation"]
        self.assertEqual((summary["status"], summary["tokens"], summary["model"]), ("SUCCESS", 1010, "gemini-3.8-flash-low"))
        events, usage = self.rows(run_id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["delta"], events[0]["usage"])
        self.assertEqual(events[0]["usage"]["total"], 1010)
        self.assertTrue(events[0]["objective"].startswith("first look"))
        self.assertEqual(len(usage), 1)
        self.assertEqual((usage[0]["provider"], usage[0]["event_type"], usage[0]["total_tokens"], usage[0]["source_seq"]), ("antigravity", "antigravity.delegate", 1010, 1))
        self.assertEqual(usage[0]["session_id"], summary["conversation_id"])
        # Second turn: same conversation, cumulative usage from the CLI, delta stored.
        code, out, err = self.delegate("second look", "--model", "gemini-3.8-flash-low")
        self.assertEqual(code, 0, err)
        self.assertIn("turn 2", out)
        events, usage = self.rows(run_id)
        self.assertEqual(events[1]["conversation_id"], events[0]["conversation_id"])
        self.assertEqual(events[1]["usage"]["total"], 2020)
        self.assertEqual(events[1]["delta"], {"input": 1000, "output": 10, "thinking": 0, "cache_read": 0, "total": 1010})
        self.assertEqual([row["total_tokens"] for row in usage], [1010, 1010])
        self.assertEqual(json.loads(err.strip().splitlines()[-1])["delegation"]["tokens"], 1010)
        self.assertEqual(runs.find(self.con, run_id)["tokens_total"], before)
        listing = [row["source_seq"] for row in usage]
        self.assertEqual(listing, [1, 2])

    def test_error_mode_records_error_and_exits_1(self):
        run_id = self.run_with("bad", allow=True)
        os.environ["FAKE_AGY_MODE"] = "error"
        code, _, err = self.delegate("review it", "--model", "gemini-3.1-pro-high")
        self.assertEqual(code, 1)
        events, usage = self.rows(run_id)
        self.assertEqual(events[0]["status"], "ERROR")
        self.assertIn("exited 1", events[0]["error"])
        self.assertEqual(json.loads(err.strip().splitlines()[-1])["delegation"]["status"], "ERROR")
        control = self.con.execute("SELECT outcome FROM control_events WHERE action='run.delegate'").fetchone()
        self.assertEqual(control["outcome"], "ERROR")

    def test_timeout_records_timeout(self):
        run_id = self.run_with("slow", allow=True)
        os.environ["FAKE_AGY_MODE"] = "hang"
        old = antigravity.PROCESS_TIMEOUT
        antigravity.PROCESS_TIMEOUT = 1
        try:
            code, _, _ = self.delegate("review it", "--model", "gemini-3.8-flash-low")
        finally:
            antigravity.PROCESS_TIMEOUT = old
        self.assertEqual(code, 1)
        self.assertEqual(self.rows(run_id)[0][0]["status"], "TIMEOUT")

    def test_child_clamp(self):
        parent = self.run_with("parent-off", allow=False)
        child = child_runs.create(self.con, parent, {"request_id": "c1", "profile": "fake", "objective": "child", "allow_antigravity": True}, actor="run:1")
        self.assertFalse(json.loads(child["request_snapshot"])["allow_antigravity"])
        parent = self.run_with("parent-on", allow=True)
        child = child_runs.create(self.con, parent, {"request_id": "c2", "profile": "fake", "objective": "child", "allow_antigravity": True}, actor="run:1")
        self.assertTrue(json.loads(child["request_snapshot"])["allow_antigravity"])
        self.assertFalse(json.loads(child_runs.create(self.con, parent, {"request_id": "c3", "profile": "fake", "objective": "child"}, actor="run:1")["request_snapshot"])["allow_antigravity"])


class DriverTests(unittest.TestCase):
    def test_pure_helpers(self):
        self.assertEqual(antigravity.parse_catalog("Fetching available models...\na\tA\n\nb\tB\n"), ["a", "b"])
        argv = antigravity.build_argv("obj", "m", "conv")
        self.assertEqual(argv[1:3], ["--print", antigravity.PROMPT_PREFIX + "obj"])
        self.assertEqual(argv[-2:], ["--conversation", "conv"])
        self.assertIn("--sandbox", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("--conversation", antigravity.build_argv("obj", "m"))
        env = antigravity.build_env({"GEMINI_API_KEY": "x", "GOOGLE_API_KEY": "y", "HOME": "/h", "PATH": "/bin"})
        self.assertEqual(env, {"HOME": "/h", "PATH": "/bin"})
        self.assertEqual(antigravity.parse_output('noise\n{"a": 1}\n[1]\n'), {"a": 1})
        self.assertIsNone(antigravity.parse_output("nothing"))
        long = antigravity.record("o", "m", {"response": "x" * 40_000, "status": "SUCCESS", "usage": {"input_tokens": 1, "total_tokens": -5}})
        self.assertEqual((len(long["response"]), long["truncated"], long["usage"]["total"], long["status"]), (32_000, True, 0, "SUCCESS"))
        hint = 'jetski: no output produced — a tool required the "command" permission that headless mode cannot prompt for, so it was auto-denied.'
        denied = antigravity.record("o", "m", {"response": "", "status": "SUCCESS", "usage": {}}, stderr=hint + "\n")
        self.assertEqual(denied["status"], "DENIED")
        self.assertIn("auto-denied", denied["error"])
        fine = antigravity.record("o", "m", {"response": "findings", "status": "SUCCESS", "usage": {}}, stderr=hint)
        self.assertEqual(fine["status"], "SUCCESS")

    def test_contract_rejects_non_boolean(self):
        with self.assertRaises(ContractError):
            RunRequest.from_mapping({"request_id": "r", "profile": "p", "objective": "o", "allow_antigravity": "yes"})
        self.assertFalse(RunRequest.from_mapping({"request_id": "r", "profile": "p", "objective": "o"}).as_dict()["allow_antigravity"])


if __name__ == "__main__":
    unittest.main()
