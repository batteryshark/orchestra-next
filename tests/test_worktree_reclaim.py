import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestra import db, fleet_config, runs, worktree
from orchestra.contracts import RunRequest


def _git(root, *args):
    subprocess.run(["git", *args], cwd=str(root), check=True,
                   capture_output=True, text=True)


class RetainedWorktreeTests(unittest.TestCase):
    """A settle that never finished used to leave its checkout forever."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ,
                              {"ORCHESTRA_HOME": str(self.root / "state")})
        self.env.start()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@example.invalid")
        _git(self.repo, "config", "user.name", "T")
        (self.repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        _git(self.repo, "add", "seed.txt")
        _git(self.repo, "commit", "-qm", "seed")
        self.con = db.connect(":memory:")
        fleet_config.create_runtime(self.con, "Exec", "exec", slug="exec",
                                    command=["agent"])
        fleet_config.create_profile(self.con, "P", "exec", slug="p", tier=2)

    def tearDown(self):
        self.con.close()
        self.env.stop()
        self.temp.cleanup()

    def _run_with_worktree(self, request_id, branch, dirty=False):
        row, _ = runs.submit(self.con, RunRequest.from_mapping({
            "request_id": request_id, "profile": "p", "context": "work",
            "cwd": str(self.repo)}))
        run_id = int(row["id"])
        work = self.root / "state" / "worktrees" / f"run-{run_id}"
        work.parent.mkdir(parents=True, exist_ok=True)
        _git(self.repo, "worktree", "add", "-q", "-b", branch, str(work))
        if dirty:
            (work / "unsaved.txt").write_text("work in progress\n",
                                              encoding="utf-8")
        self.con.execute(
            "UPDATE runs SET status='stopped',finished_at=?,workdir=?,branch=? "
            "WHERE id=?", (db.now(), str(work), branch, run_id))
        self.con.commit()
        return run_id, work

    def test_a_clean_checkout_is_reclaimed_and_a_dirty_one_is_reported(self):
        clean_id, clean = self._run_with_worktree("clean", "orchestra/clean")
        dirty_id, dirty = self._run_with_worktree(
            "dirty", "orchestra/dirty", dirty=True)

        listed = {e["run_id"]: e for e in worktree.retained(self.con)}
        self.assertEqual(set(listed), {clean_id, dirty_id})
        self.assertEqual(listed[clean_id]["risks"], [])
        self.assertTrue(listed[dirty_id]["risks"])

        result = worktree.reclaim(self.con)
        self.assertEqual(result["removed"], [str(clean)])
        self.assertFalse(clean.exists())
        # Uncommitted work is never discarded by a sweep.
        self.assertTrue(dirty.exists())
        self.assertEqual([e["run_id"] for e in result["kept"]], [dirty_id])
        # The branch survives its checkout: it is the run's evidence.
        self.assertTrue(worktree.branch_exists(self.repo, "orchestra/clean"))

    def test_a_live_run_keeps_its_checkout(self):
        run_id, work = self._run_with_worktree("live", "orchestra/live")
        self.con.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
        self.con.commit()
        self.assertEqual(worktree.retained(self.con), [])
        self.assertEqual(worktree.reclaim(self.con)["removed"], [])
        self.assertTrue(work.exists())


if __name__ == "__main__":
    unittest.main()
