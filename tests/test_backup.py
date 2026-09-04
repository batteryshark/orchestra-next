import json
import unittest
from pathlib import Path
from unittest import mock

from orchestra import backup, cli, db, paths
from tests.common import StateCase


class BackupTests(StateCase):
    def setUp(self):
        super().setUp()
        self.con.close()
        patcher = mock.patch.object(backup, "daemon_running", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        paths.bootstrap_path().write_text('{"a":1}', encoding="utf-8")
        (paths.run_artifacts_dir(1) / "out.txt").write_text("v1", encoding="utf-8")
        self.instance = self._instance()

    def tearDown(self):
        self.con = db.connect()
        super().tearDown()

    def _instance(self):
        con = db.connect()
        try:
            return db.instance_id(con)
        finally:
            con.close()

    def test_round_trip(self):
        manifest = Path(backup.backup(self.root / "bk")["manifest"])
        self.assertEqual(json.loads(manifest.read_text())["schema_version"], db.SCHEMA_VERSION)
        (paths.run_artifacts_dir(1) / "out.txt").write_text("v2", encoding="utf-8")
        paths.db_path().unlink()
        db.connect().close()  # new instance id
        self.assertNotEqual(self._instance(), self.instance)
        plan = backup.restore(manifest.parent)
        self.assertFalse(plan["apply"])
        self.assertEqual((paths.run_artifacts_dir(1) / "out.txt").read_text(), "v2")
        result = backup.restore(manifest.parent, apply=True)
        self.assertEqual(self._instance(), self.instance)
        self.assertEqual((paths.run_artifacts_dir(1) / "out.txt").read_text(), "v1")
        moved = sorted(p.name for p in Path(result["trash"]).iterdir() if not p.name.startswith("orchestra-next.db-"))
        self.assertEqual(moved, ["artifacts", "bootstrap.json", "orchestra-next.db"])

    def test_digest_mismatch_refused(self):
        folder = Path(backup.backup(self.root / "bk")["manifest"]).parent
        (folder / "bootstrap.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            backup.restore(folder, apply=True)
        self.assertEqual(paths.bootstrap_path().read_text(), '{"a":1}')
        self.assertFalse((paths.state_dir() / "trash").exists())

    def test_daemon_refused(self):
        with mock.patch.object(backup, "daemon_running", return_value=True):
            self.assertEqual(cli.main(["backup", str(self.root / "bk")]), 1)
        self.assertFalse((self.root / "bk").exists())


if __name__ == "__main__":
    unittest.main()
