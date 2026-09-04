import json
import os
import sys
import time
import unittest

from orchestra import artifacts, attention, auth, child_runs, messaging, runs, scheduler, settings
from orchestra.contracts import RunRequest
from tests.common import StateCase


class DomainTests(StateCase):
    def setUp(self):
        super().setUp()
        self.create_profile()
        self.repo = self.git_repo()

    def request(self, request_id, **values):
        body = {"request_id": request_id, "profile": "fake", "objective": request_id, "cwd": str(self.repo)}
        body.update(values)
        return RunRequest.from_mapping(body)

    def test_run_request_is_durable_and_idempotent(self):
        first, created = runs.submit(self.con, self.request("one"))
        second, duplicate = runs.submit(self.con, self.request("one"))
        self.assertTrue(created); self.assertFalse(duplicate)
        self.assertEqual(first["id"], second["id"])
        self.assertNotIn("runtime", json.loads(first["profile_snapshot"]))
        with self.assertRaises(runs.AdmissionError):
            runs.submit(self.con, self.request("one", objective="different"))

    def test_dependencies_and_concurrency(self):
        first, _ = runs.submit(self.con, self.request("first"))
        second, _ = runs.submit(self.con, self.request("second", after=[{"run_id": first["id"], "condition": "success"}]))
        admitted = scheduler.admit(self.con)
        self.assertIn(first["id"], admitted["admitted"])
        self.assertNotIn(second["id"], admitted["admitted"])
        self.con.execute("UPDATE runs SET status='completed' WHERE id=?", (first["id"],)); self.con.commit()
        self.assertIn(second["id"], scheduler.admit(self.con)["admitted"])

    def test_paused_scheduler_admits_nothing_and_resume_admits_again(self):
        run, _ = runs.submit(self.con, self.request("held"))
        settings.set_paused(self.con, True, actor="test")
        self.assertEqual(scheduler.admit(self.con), {"admitted": [], "skipped": []})
        settings.set_paused(self.con, False, actor="test")
        self.assertEqual(scheduler.admit(self.con)["admitted"], [run["id"]])

    def test_max_active_runs_setting_bounds_admission(self):
        ids = [runs.submit(self.con, self.request(f"r{index}"))[0]["id"] for index in range(3)]
        settings.update(self.con, {"max_active_runs": 2}, expected_revision=settings.revision(self.con), actor="test")
        self.assertEqual(scheduler.admit(self.con)["admitted"], ids[:2])
        self.con.execute("UPDATE runs SET status='running' WHERE id IN (?,?)", ids[:2]); self.con.commit()
        self.assertEqual(scheduler.admit(self.con)["admitted"], [])
        settings.update(self.con, {"max_active_runs": 3}, expected_revision=settings.revision(self.con), actor="test")
        self.assertEqual(scheduler.admit(self.con)["admitted"], [ids[2]])

    def test_raw_auth_scopes_and_terminal_revocation(self):
        run, _ = runs.submit(self.con, self.request("auth"))
        token = auth.mint_run(self.con, run["id"])
        identity = auth.identify(self.con, token)
        auth.authorize(identity, "artifact", target_run_id=run["id"])
        with self.assertRaises(auth.AuthError):
            auth.authorize(identity, "artifact", target_run_id=run["id"] + 1)
        self.con.execute("UPDATE runs SET status='completed' WHERE id=?", (run["id"],)); self.con.commit()
        self.assertIsNone(auth.identify(self.con, token))
        service, _ = auth.create_service_token(self.con, "delegate", ["read", "reroute"])
        self.assertEqual(service["authorities"], ["read", "reroute"])
        with self.assertRaises(auth.AuthError):
            auth.create_service_token(self.con, "legacy", ["control"])

    def test_attention_leases_prevent_automated_races_but_human_can_override(self):
        run, _ = runs.submit(self.con, self.request("attention"))
        item = attention.open_request(self.con, run["id"], kind="question", prompt="Choose")
        lease = attention.lease(self.con, item["attention_id"], holder="service:a")
        with self.assertRaises(attention.AttentionError):
            attention.answer(self.con, item["attention_id"], answer="B", actor="service:b", lease_id="wrong")
        result = attention.answer(self.con, item["attention_id"], answer="human answer", actor="device:h", human=True)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(runs.find(self.con, run["id"])["status"], "queued")
        actions = [row[0] for row in self.con.execute("SELECT action FROM control_events")]
        self.assertIn("attention.lease", actions); self.assertIn("attention.answer", actions)

    def test_child_tier_and_count_are_bounded(self):
        parent, _ = runs.submit(self.con, self.request("parent", max_children=1, max_child_tier=2))
        child = child_runs.create(self.con, parent["id"], {"request_id": "child", "profile": "fake", "objective": "child"})
        self.assertEqual(child["parent_run_id"], parent["id"])
        with self.assertRaises(child_runs.DelegationError):
            child_runs.create(self.con, parent["id"], {"request_id": "child2", "profile": "fake", "objective": "child"})

    def test_attention_opened_fires_the_callback_once_per_request(self):
        from orchestra import callbacks, config
        sink = self.root / "callback.jsonl"
        script = self.root / "callback.py"
        script.write_text("import sys\nopen(sys.argv[1], 'a').write(sys.stdin.read() + '\\n')\n", encoding="utf-8")
        config.write({"callback_command": [sys.executable, str(script), str(sink)]})
        run, _ = runs.submit(self.con, self.request("callback"))
        item = attention.open_request(self.con, run["id"], kind="permission", prompt="allow?", context={"tool": "bash"})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not sink.is_file():
            time.sleep(0.05)
        time.sleep(0.2)
        lines = [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["event"], "attention.opened")
        self.assertEqual(lines[0]["data"], {"attention_id": item["attention_id"], "run_id": run["id"], "kind": "permission", "blocking": True})
        self.assertIn("attention.opened", callbacks.EVENTS)

    def test_artifact_publication_is_immutable_and_cannot_escape(self):
        run, _ = runs.submit(self.con, self.request("artifact"))
        work = self.root / "work"; work.mkdir(); (work / "report.md").write_text("original", encoding="utf-8")
        self.con.execute("UPDATE runs SET workdir=? WHERE id=?", (str(work), run["id"])); self.con.commit()
        item = artifacts.publish(self.con, run["id"], "report.md")
        (work / "report.md").write_text("changed", encoding="utf-8")
        stored = artifacts.stored_file(self.con, item["artifact_id"])[0]
        self.assertEqual(stored.read_text(), "original")
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.stage(run["id"], work, "../outside")

    def test_tell_waits_for_a_safe_boundary_but_interrupt_does_not(self):
        run, _ = runs.submit(self.con, self.request("messages"))
        messaging.queue(self.con, run["id"], kind="tell", body="later", sender="device")
        messaging.queue(self.con, run["id"], kind="interrupt", body="now", sender="device")
        claimed = messaging.claim_pending(self.con, run["id"], safe_boundary=False)
        self.assertEqual([item["kind"] for item in claimed], ["interrupt"])
        self.assertEqual(messaging.claim_pending(self.con, run["id"], safe_boundary=True)[0]["kind"], "tell")


if __name__ == "__main__":
    unittest.main()
