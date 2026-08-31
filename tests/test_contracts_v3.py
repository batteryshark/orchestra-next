import os
import stat
import unittest
from pathlib import Path

from orchestra import client, config, db, dsh, paths, profiles
from orchestra.contracts import ContractError, RunRequest
from tests.common import StateCase


class ContractTests(StateCase):
    def test_identity_defaults_do_not_collide_with_v2(self):
        self.assertEqual(paths.home(), self.state)
        self.assertEqual(config.read()["port"], 8766)
        self.assertEqual(config.api_url(), "http://127.0.0.1:8766")
        self.assertEqual(db.meta_get(self.con, "schema_version"), "v3")

    def test_goal_and_ralph_defaults_and_ceilings(self):
        goal = RunRequest.from_mapping({"request_id": "g", "profile": "p", "objective": "do it"})
        ralph = RunRequest.from_mapping({"request_id": "r", "profile": "p", "objective": "do it", "strategy": "ralph"})
        self.assertEqual((goal.max_rounds, goal.active_seconds), (32, 7200))
        self.assertEqual(ralph.max_rounds, 8)
        with self.assertRaises(ContractError):
            RunRequest.from_mapping({"request_id": "x", "profile": "p", "objective": "x", "limits": {"max_rounds": 129}})
        with self.assertRaises(ContractError):
            RunRequest.from_mapping({"request_id": "x", "profile": "p", "objective": "x", "strategy": "ralph", "limits": {"max_rounds": 33}})

    def test_verifier_is_explicit_argv(self):
        request = RunRequest.from_mapping({"request_id": "x", "profile": "p", "objective": "x", "verify": {"argv": ["python", "-m", "tests"], "timeout_seconds": 9}})
        self.assertEqual(request.verify.argv, ("python", "-m", "tests"))
        with self.assertRaises(ContractError):
            RunRequest.from_mapping({"request_id": "x2", "profile": "p", "objective": "x", "verify": {"argv": "make test"}})

    def test_profile_route_requires_advertised_model_and_effort(self):
        row = self.create_profile()
        self.assertEqual((row["provider"], row["model"], row["effort"]), ("fake", "model", "low"))
        with self.assertRaises(ValueError):
            profiles.create(self.con, name="Bad", provider="fake", model="missing", catalog={("fake", "model"): {"low"}})
        with self.assertRaises(ValueError):
            profiles.create(self.con, name="Bad effort", provider="fake", model="model", effort="ultra", catalog={("fake", "model"): {"low"}})

    def test_profile_setup_is_idempotent_and_does_not_touch_credentials(self):
        self.dsh_home.mkdir(parents=True)
        credentials = self.dsh_home / ".credentials.yaml"
        credentials.write_text("secret: keep\n", encoding="utf-8")
        destination, changed = dsh.setup_profile()
        self.assertTrue(changed)
        self.assertEqual(credentials.read_text(), "secret: keep\n")
        self.assertEqual(dsh.setup_profile(), (destination, False))
        self.assertIn("session-persistence-jsonl", (destination / "cordis.patch.yml").read_text())
        self.assertIn("packChunks: false", (destination / "cordis.patch.yml").read_text())
        checked = dsh.check_profile()
        self.assertFalse(checked["capabilities"]["native_web_search"]["available"])
        os.environ["DEEPSEEK_API_KEY"] = "not-a-real-key"
        checked = dsh.check_profile()
        self.assertEqual(checked["capabilities"]["native_web_search"],
                         {"available": True, "source": "environment"})

    def test_version_rejection_is_clear(self):
        wrong = self.root / "wrong-dsh"
        wrong.write_text("#!/bin/sh\necho 'dsh 9.9.9'\n", encoding="utf-8")
        wrong.chmod(0o700)
        os.environ["ORCHESTRA_NEXT_DSH"] = str(wrong)
        with self.assertRaisesRegex(dsh.DshError, "requires exactly 0.1.2-alpha.3"):
            dsh.require_version()

    def test_worker_auth_is_file_only(self):
        self.install_profile()
        os.environ["ORCHESTRA_NEXT_TOKEN"] = "broader-operator-secret"
        run = {"id": 7, "strategy": "goal", "permission_mode": "workspace-write"}
        argv, env = dsh.launch(run, "or_secret")
        self.assertNotIn("or_secret", env.values())
        self.assertNotIn("broader-operator-secret", env.values())
        self.assertNotIn("ORCHESTRA_NEXT_TOKEN", env)
        auth_file = Path(env["ORCHESTRA_NEXT_RUN_AUTH_FILE"])
        self.assertEqual(stat.S_IMODE(auth_file.stat().st_mode), 0o600)
        self.assertIn("or_secret", auth_file.read_text())
        os.environ["ORCHESTRA_NEXT_RUN_AUTH_FILE"] = str(auth_file)
        self.assertEqual(client.load_token("http://fixture"), "or_secret")
        self.assertEqual(argv[1:3], ["--profile", "orchestra-next"])
        self.assertEqual(argv[3], "--patch")
        ralph = {"id": 8, "strategy": "ralph", "permission_mode": "workspace-write",
                 "max_rounds": 8, "rounds_started": 3}
        ralph_argv, ralph_env = dsh.launch(ralph, "another-secret")
        self.assertEqual(ralph_env["ORCHESTRA_NEXT_RALPH_ROUNDS"], "5")
        self.assertTrue(ralph_argv[-1].endswith("ralph.patch.yml"))


if __name__ == "__main__":
    unittest.main()
