import json
import subprocess

from orchestra import child_runs, runs, supervise, worktree
from orchestra.contracts import RunRequest
from tests.common import StateCase


class DelegationTests(StateCase):
    def setUp(self):
        super().setUp()
        self.create_profile()
        self.repo = self.git_repo()

    def parent(self, request_id, **values):
        body = {"request_id": request_id, "profile": "fake", "objective": request_id, "cwd": str(self.repo)}
        body.update(values)
        return runs.submit(self.con, RunRequest.from_mapping(body))[0]

    def child(self, parent, request_id, **values):
        body = {"request_id": request_id, "profile": "fake", "objective": "child"}
        body.update(values)
        return child_runs.create(self.con, parent["id"], body, actor="run:1")

    def test_child_permission_mode_never_exceeds_parent(self):
        parent = self.parent("ww", permission_mode="workspace-write")
        self.assertEqual(self.child(parent, "ww-danger", permission_mode="danger-full-access")["permission_mode"], "workspace-write")
        self.assertEqual(self.child(parent, "ww-ro", permission_mode="read-only")["permission_mode"], "read-only")
        parent = self.parent("ro", permission_mode="read-only")
        self.assertEqual(self.child(parent, "ro-default")["permission_mode"], "read-only")
        self.assertEqual(self.child(parent, "ro-ww", permission_mode="workspace-write")["permission_mode"], "read-only")

    def test_child_uses_parent_verifier_not_its_own(self):
        parent = self.parent("verified", verify={"argv": ["true"]})
        child = self.child(parent, "verified-child", verify={"argv": ["sh", "-c", "touch pwned"]})
        self.assertEqual(json.loads(child["verify_json"])["argv"], ["true"])
        self.assertIsNone(self.child(self.parent("plain"), "plain-child", verify={"argv": ["false"]})["verify_json"])

    def test_child_limits_are_capped_by_parent(self):
        parent = self.parent("tiered", max_child_tier=2, max_children=3)
        child = self.child(parent, "tiered-child", max_child_tier=5, max_children=10)
        self.assertEqual(child["max_child_tier"], 2)
        self.assertEqual(child["max_children"], 3)
        inherited = self.child(parent, "tiered-default")
        self.assertEqual((inherited["max_child_tier"], inherited["max_children"]), (2, 3))

    def test_worktree_ref_is_never_a_git_option(self):
        run = self.parent("lock", ref="--lock")
        location = None
        try:
            location, _ = supervise._prepare_worktree(self.con, run)
        except SystemExit as exc:
            self.assertIn("--lock", str(exc))
        self.assertEqual(list((self.repo / ".git" / "worktrees").glob("*/locked")), [])
        if location is not None:
            self.assertTrue(worktree.remove(location, self.repo, force=True)["removed"])

    def test_checkpoint_does_not_run_repository_hooks(self):
        hooks = self.root / "hooks"
        hooks.mkdir()
        marker = self.root / "hook-ran"
        (hooks / "pre-commit").write_text(f'#!/bin/sh\ntouch "{marker}"\n', encoding="utf-8")
        (hooks / "pre-commit").chmod(0o755)
        subprocess.run(["git", "-C", str(self.repo), "config", "core.hooksPath", str(hooks)], check=True)
        (self.repo / "work.txt").write_text("done\n", encoding="utf-8")
        before = worktree.head(self.repo)
        evidence = supervise._checkpoint(1, self.repo)
        self.assertFalse(marker.exists())
        self.assertNotEqual(evidence["end_ref"], before)
        self.assertIn("work.txt", evidence["diff_stat"])


if __name__ == "__main__":
    import unittest
    unittest.main()
