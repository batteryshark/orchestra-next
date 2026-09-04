import subprocess
import unittest
from pathlib import Path
from unittest import mock

from orchestra import api, auth, update
from tests.common import StateCase


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(cwd, name):
    (cwd / name).write_text(name)
    git(cwd, "add", name)
    git(cwd, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", name)


class UpdateTests(StateCase):
    def setUp(self):
        super().setUp()
        self.origin = self.root / "origin.git"
        seed = self.root / "seed"
        seed.mkdir()
        git(seed, "init", "-q", "-b", "main")
        commit(seed, "a")
        git(seed, "clone", "-q", "--bare", str(seed), str(self.origin))
        self.clone = self.root / "clone"
        git(self.root, "clone", "-q", str(self.origin), str(self.clone))
        self.upstream = self.root / "other"
        git(self.root, "clone", "-q", str(self.origin), str(self.upstream))
        self.patch = mock.patch.object(update, "repo_root", return_value=self.clone)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def test_status_check_and_fast_forward(self):
        status = update.status()
        self.assertTrue(status["available"])
        self.assertEqual((status["branch"], status["dirty"], status["subject"]), ("main", False, "a"))
        self.assertEqual(update.check()["behind"], 0)
        commit(self.upstream, "b"); commit(self.upstream, "c")
        git(self.upstream, "push", "-q", "origin", "main")
        result = update.check()
        self.assertEqual((result["behind"], result["ahead"], result["can_update"]), (2, 0, True))
        self.assertEqual([c["subject"] for c in result["commits"]], ["c", "b"])
        applied = update.apply()
        self.assertTrue(applied["updated"])
        self.assertEqual((applied["subject"], applied["behind"], applied["previous"]), ("c", 0, status["sha"]))
        self.assertFalse(update.apply()["updated"])

    def test_dirty_and_diverged_checkouts_are_refused(self):
        commit(self.upstream, "b"); git(self.upstream, "push", "-q", "origin", "main")
        (self.clone / "a").write_text("edited")
        self.assertFalse(update.check()["can_update"])
        with self.assertRaisesRegex(update.UpdateError, "local changes"):
            update.apply()
        git(self.clone, "checkout", "--", "a")
        commit(self.clone, "local")
        with self.assertRaisesRegex(update.UpdateError, "ahead"):
            update.apply()
        self.assertEqual(update.check()["ahead"], 1)

    def test_api_routes_need_an_operator_and_report_conflicts(self):
        service = api.API(self.con)
        reader = auth.Identity("service", "s", frozenset({"read"}))
        operator = auth.Identity("network", "127.0.0.1", frozenset({"*"}))
        with self.assertRaises(api.Problem) as problem:
            service.handle("GET", "/api/update", {}, None, reader)
        self.assertEqual(problem.exception.status, 403)
        self.assertEqual(service.handle("GET", "/api/update", {}, None, operator).data["data"]["subject"], "a")
        (self.clone / "a").write_text("edited")
        commit(self.upstream, "b"); git(self.upstream, "push", "-q", "origin", "main")
        with self.assertRaises(api.Problem) as problem:
            service.handle("POST", "/api/update/apply", {}, {}, operator)
        self.assertEqual(problem.exception.status, 409)
        git(self.clone, "checkout", "--", "a")
        with mock.patch.object(update, "schedule_restart", return_value="service") as restart:
            body = service.handle("POST", "/api/update/apply", {}, {}, operator).data["data"]
        self.assertEqual((body["updated"], body["restart"], body["subject"]), (True, "service", "b"))
        restart.assert_called_once()
        row = self.con.execute("SELECT action, outcome FROM control_events WHERE action='update.apply'").fetchone()
        self.assertEqual(tuple(row), ("update.apply", "ok"))


if __name__ == "__main__":
    unittest.main()
