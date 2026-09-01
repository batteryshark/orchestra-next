import json
import subprocess
import unittest
from pathlib import Path

from orchestra import artifacts, callbacks, runs, storage, worktree
from orchestra.contracts import RunRequest
from tests.common import StateCase


class EvidenceTests(StateCase):
    def setUp(self):
        super().setUp(); self.create_profile(); self.repo = self.git_repo()

    def new_run(self, request_id):
        return runs.submit(self.con, RunRequest.from_mapping({"request_id": request_id, "profile": "fake", "objective": "work", "cwd": str(self.repo)}))[0]

    def test_worktree_identity_and_merge_evidence(self):
        run = self.new_run("wt")
        location, branch = worktree.create(self.repo, run["id"], "general")
        self.assertTrue(branch.startswith("orchestra-next/run-"))
        (location / "result.txt").write_text("done\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(location), "add", "result.txt"], check=True)
        subprocess.run(["git", "-C", str(location), "commit", "-qm", "result"], check=True)
        result = worktree.merge_into_owner(self.repo, branch)
        self.assertTrue(result["merged"])
        self.assertEqual((self.repo / "result.txt").read_text(), "done\n")

    def test_storage_plan_is_reviewable_and_recoverable(self):
        run = self.new_run("storage")
        work = self.root / "work"; work.mkdir(); (work / "a.txt").write_text("a")
        self.con.execute("UPDATE runs SET workdir=?,status='completed',finished_at=? WHERE id=?", (str(work), "2000-01-01T00:00:00+00:00", run["id"])); self.con.commit()
        artifacts.publish(self.con, run["id"], "a.txt")
        session = self.state / "runs" / str(run["id"]) / "dsh-session"; session.mkdir(parents=True); (session / "log").write_text("log")
        plan = storage.create_plan(self.con, actor="test", older_than_days=0)
        self.assertEqual(len(plan["items"]), 2)
        applied = storage.apply_plan(self.con, plan["plan_id"], actor="test")
        self.assertEqual(len(applied["result"]["moved"]), 2)
        self.assertTrue(Path(applied["result"]["trash"]).is_dir())

    def test_callback_contract_has_no_observer_event(self):
        value = json.loads(callbacks.envelope("run.terminal", {"run_id": 1}))
        self.assertNotIn("version", value)
        self.assertEqual(value["event"], "run.terminal")
        with self.assertRaises(ValueError):
            callbacks.envelope("observer.stopped", {})


if __name__ == "__main__":
    unittest.main()
