"""GET /api/host-directories: the cwd picker's one-level directory listing."""
import os
import unittest

from orchestra import api, auth
from tests.common import StateCase


class HostDirectoriesTests(StateCase):
    def setUp(self):
        super().setUp()
        self.old_home = os.environ.get("HOME")
        self.home = self.root / "home"
        (self.home / "projects" / "alpha" / ".git").mkdir(parents=True)
        (self.home / "projects" / "alpha" / ".git" / "HEAD").write_text("ref: refs/heads/feature/x\n", encoding="utf-8")
        (self.home / "projects" / "beta").mkdir()
        (self.home / "projects" / ".hidden").mkdir()
        (self.home / "projects" / "notes.txt").write_text("file", encoding="utf-8")
        os.environ["HOME"] = str(self.home)
        self.identity = auth.Identity("device", "op", frozenset({"*"}))
        self.api = api.API(self.con)

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home
        super().tearDown()

    def call(self, query=None, identity="device"):
        who = self.identity if identity == "device" else identity
        return self.api.handle("GET", "/api/host-directories", query or {}, None, who).data["data"]

    def test_lists_subdirectories_only_sorted_with_git_flag(self):
        value = self.call({"path": str(self.home / "projects")})
        self.assertEqual(value["path"], str((self.home / "projects").resolve()))
        self.assertEqual(value["parent"], str(self.home.resolve()))
        self.assertEqual([(e["name"], e["git"]) for e in value["entries"]], [("alpha", True), ("beta", False)])
        self.assertTrue(value["entries"][0]["path"].endswith("/projects/alpha"))
        self.assertFalse(value["truncated"])
        self.assertFalse(value["git"])

    def test_branch_is_read_from_git_head(self):
        value = self.call({"path": str(self.home / "projects" / "alpha")})
        self.assertTrue(value["git"])
        self.assertEqual(value["branch"], "feature/x")
        self.assertIsNone(self.call({"path": str(self.home / "projects")})["branch"])

    def test_hidden_directories_need_the_flag(self):
        names = [e["name"] for e in self.call({"path": str(self.home / "projects"), "hidden": "1"})["entries"]]
        self.assertEqual(names, [".hidden", "alpha", "beta"])

    def test_default_path_is_home(self):
        self.assertEqual(self.call()["path"], str(self.home.resolve()))

    def test_paths_outside_home_and_group_cwds_are_forbidden(self):
        outside = self.root / "elsewhere" / "deep"
        outside.mkdir(parents=True)
        with self.assertRaises(api.Problem) as problem:
            self.call({"path": str(outside)})
        self.assertEqual(problem.exception.status, 403)
        self.api.handle("POST", "/api/groups", {}, {"name": "Else", "cwd": str(outside.parent)}, self.identity)
        self.assertEqual(self.call({"path": str(outside)})["path"], str(outside.resolve()))

    def test_missing_or_file_path_is_404(self):
        for path in (str(self.home / "nope"), str(self.home / "projects" / "notes.txt")):
            with self.assertRaises(api.Problem) as problem:
                self.call({"path": path})
            self.assertEqual(problem.exception.status, 404)

    def test_only_operator_identities_may_browse(self):
        service = auth.Identity("service", "svc", frozenset({"*"}))
        worker = auth.Identity("run", "7", auth.RUN_AUTHORITIES, 7)
        for who in (service, worker):
            with self.assertRaises(api.Problem) as problem:
                self.call({"path": str(self.home)}, identity=who)
            self.assertEqual(problem.exception.status, 403)
        with self.assertRaises(api.Problem) as problem:
            self.call({"path": str(self.home)}, identity=None)
        self.assertEqual(problem.exception.status, 401)


if __name__ == "__main__":
    unittest.main()
