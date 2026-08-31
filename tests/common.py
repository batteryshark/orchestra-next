import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestra import db, dsh, profiles


class StateCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.dsh_home = self.root / "dsh"
        self.old = {name: os.environ.get(name) for name in (
            "ORCHESTRA_NEXT_HOME", "DSH_HOME", "ORCHESTRA_NEXT_DSH", "FAKE_DSH_MODE",
            "DEEPSEEK_API_KEY", "ORCHESTRA_NEXT_TOKEN", "ORCHESTRA_NEXT_RUN_AUTH_FILE")}
        os.environ["ORCHESTRA_NEXT_HOME"] = str(self.state)
        os.environ["DSH_HOME"] = str(self.dsh_home)
        os.environ["ORCHESTRA_NEXT_DSH"] = str(Path(__file__).with_name("fake_dsh.py"))
        os.environ.pop("FAKE_DSH_MODE", None)
        os.environ.pop("DEEPSEEK_API_KEY", None)
        os.environ.pop("ORCHESTRA_NEXT_TOKEN", None)
        os.environ.pop("ORCHESTRA_NEXT_RUN_AUTH_FILE", None)
        self.con = db.connect()

    def tearDown(self):
        self.con.close()
        for name, value in self.old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temporary.cleanup()

    def install_profile(self):
        return dsh.setup_profile()

    def create_profile(self, **overrides):
        values = {"name": "Fake", "slug": "fake", "provider": "fake", "model": "model", "effort": "low", "tier": 2, "catalog": {("fake", "model"): {"low", "high"}}}
        values.update(overrides)
        return profiles.create(self.con, **values)

    def git_repo(self):
        root = self.root / "repo"
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
        (root / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True)
        return root
