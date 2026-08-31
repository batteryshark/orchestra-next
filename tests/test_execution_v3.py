import json
import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from orchestra import attention, messaging, paths, runs, supervise
from orchestra.contracts import RunRequest
from tests.common import StateCase


class ExecutionTests(StateCase):
    def setUp(self):
        super().setUp()
        self.install_profile()
        self.create_profile()
        self.repo = self.git_repo()

    def submit(self, request_id, **values):
        body = {"request_id": request_id, "profile": "fake", "objective": "finish the fixture", "cwd": str(self.repo)}
        body.update(values)
        return runs.submit(self.con, RunRequest.from_mapping(body))[0]

    def test_multi_round_goal_completes_in_one_session_with_usage_and_git_evidence(self):
        run = self.submit("goal")
        self.assertEqual(supervise.supervise(run["id"]), 0)
        done = runs.find(self.con, run["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["goal_state"], "complete")
        self.assertEqual(done["rounds_started"], 1)
        self.assertEqual((done["tokens_input"], done["tokens_cache_read"], done["tokens_cache_write"]), (20, 10, 2))
        self.assertTrue(done["dsh_session_id"])
        self.assertTrue(done["end_ref"])
        self.assertFalse(paths.run_auth_path(run["id"]).exists())
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM usage_events WHERE run_id=?", (run["id"],)).fetchone()[0], 1)

    def test_crash_resumes_same_run_and_session_once(self):
        os.environ["FAKE_DSH_MODE"] = "crash-once"
        run = self.submit("resume")
        self.assertEqual(supervise.supervise(run["id"]), 1)
        queued = runs.find(self.con, run["id"])
        self.assertEqual((queued["status"], queued["resume_count"]), ("queued", 1))
        session_id = queued["dsh_session_id"]
        self.assertEqual(supervise.supervise(run["id"]), 0)
        done = runs.find(self.con, run["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["dsh_session_id"], session_id)

    def test_permission_parks_exact_process_and_answer_continues(self):
        os.environ["FAKE_DSH_MODE"] = "permission"
        run = self.submit("permission")
        thread = threading.Thread(target=supervise.supervise, args=(run["id"],))
        thread.start()
        deadline = time.time() + 5
        item = None
        while time.time() < deadline:
            rows = attention.inbox(self.con)
            if rows:
                item = rows[0]; break
            time.sleep(0.05)
        self.assertIsNotNone(item)
        waiting = runs.find(self.con, run["id"])
        self.assertEqual((waiting["status"], waiting["waiting_kind"]), ("waiting", "permission"))
        pid = waiting["dsh_pid"]
        attention.answer(self.con, item["attention_id"], answer="allow", actor="device:test", human=True)
        thread.join(8)
        self.assertFalse(thread.is_alive())
        done = runs.find(self.con, run["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["resume_count"], 0)
        self.assertGreater(pid, 0)
        self.assertEqual(self.con.execute("SELECT outcome FROM control_events WHERE action='permission.answer'").fetchone()[0], "allowed")

    def test_permission_timeout_rejects_and_releases_process(self):
        os.environ["FAKE_DSH_MODE"] = "permission"
        run = self.submit("permission-timeout")
        with mock.patch.object(supervise, "PERMISSION_WAIT_SECONDS", 0.05):
            self.assertEqual(supervise.supervise(run["id"]), 0)
        waiting = runs.find(self.con, run["id"])
        self.assertEqual((waiting["status"], waiting["waiting_kind"], waiting["dsh_pid"]),
                         ("waiting", "attention", None))
        expired = self.con.execute(
            "SELECT status FROM attention_requests WHERE run_id=? AND kind='permission'",
            (run["id"],)).fetchone()
        self.assertEqual(expired["status"], "expired")
        timeout = self.con.execute(
            "SELECT outcome FROM control_events WHERE action='permission.timeout'").fetchone()
        self.assertEqual(timeout["outcome"], "rejected")

    def test_ralph_uses_explicit_overlay_and_completes(self):
        os.environ["FAKE_DSH_MODE"] = "ralph"
        run = self.submit("ralph", strategy="ralph")
        self.assertEqual(supervise.supervise(run["id"]), 0)
        self.assertEqual(runs.find(self.con, run["id"])["status"], "completed")

    def test_explicit_ralph_resume_uses_remaining_aggregate_rounds(self):
        os.environ["FAKE_DSH_MODE"] = "ralph"
        run = self.submit("ralph-resume", strategy="ralph")
        with self.con:
            self.con.execute(
                "UPDATE runs SET dsh_session_id='existing-ralph',rounds_started=3,"
                "waiting_detail='Resolved answer' WHERE id=?", (run["id"],))
        self.assertEqual(supervise.supervise(run["id"]), 0)
        done = runs.find(self.con, run["id"])
        self.assertEqual((done["status"], done["rounds_started"]), ("completed", 5))

    def test_ralph_budget_exhaustion_opens_attention_without_replay(self):
        os.environ["FAKE_DSH_MODE"] = "ralph-budget"
        run = self.submit("ralph-budget", strategy="ralph")
        self.assertEqual(supervise.supervise(run["id"]), 1)
        waiting = runs.find(self.con, run["id"])
        self.assertEqual((waiting["status"], waiting["waiting_kind"], waiting["resume_count"]), ("waiting", "attention", 0))
        self.assertEqual(waiting["rounds_started"], 8)

    def test_interrupted_ralph_is_not_replayed(self):
        os.environ["FAKE_DSH_MODE"] = "crash-once"
        run = self.submit("ralph-crash", strategy="ralph")
        self.assertEqual(supervise.supervise(run["id"]), 1)
        waiting = runs.find(self.con, run["id"])
        self.assertEqual((waiting["status"], waiting["resume_count"]), ("waiting", 0))

    def test_two_verification_repairs_then_attention(self):
        verifier = str(Path(__file__).with_name("fail_verifier.py"))
        run = self.submit("verify", verify={"argv": [verifier], "timeout_seconds": 5})
        self.assertEqual(supervise.supervise(run["id"]), 1)
        waiting = runs.find(self.con, run["id"])
        self.assertEqual((waiting["status"], waiting["waiting_kind"], waiting["verification_repairs"]), ("waiting", "verification", 2))
        self.assertEqual(attention.inbox(self.con)[0]["kind"], "verification")

    def test_ralph_verification_repairs_keep_session_and_aggregate_budget(self):
        os.environ["FAKE_DSH_MODE"] = "ralph"
        verifier = str(Path(__file__).with_name("fail_verifier.py"))
        run = self.submit("ralph-verify", strategy="ralph",
                          verify={"argv": [verifier], "timeout_seconds": 5})
        self.assertEqual(supervise.supervise(run["id"]), 1)
        waiting = runs.find(self.con, run["id"])
        self.assertEqual((waiting["status"], waiting["verification_repairs"],
                          waiting["rounds_started"]), ("waiting", 2, 6))
        journals = list(paths.run_session_dir(run["id"]).rglob("session.jsonl"))
        self.assertEqual(len(journals), 1)

    def test_reroute_records_cache_epoch(self):
        run = self.submit("route")
        messaging.queue(self.con, run["id"], kind="reroute", body=json.dumps({"provider": "fake", "model": "other", "effort": "high", "body": "continue"}), sender="device:test")
        supervise.supervise(run["id"])
        done = runs.find(self.con, run["id"])
        self.assertEqual((done["route_model"], done["route_effort"], done["cache_epoch"]), ("other", "high", 1))


if __name__ == "__main__":
    unittest.main()
