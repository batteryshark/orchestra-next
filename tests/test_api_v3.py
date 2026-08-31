import json
import os
import unittest

from orchestra import api, auth, config
from tests.common import StateCase


class ApiTests(StateCase):
    def setUp(self):
        super().setUp()
        self.install_profile()
        self.identity = auth.Identity("device", "test", frozenset({"*"}))
        self.api = api.API(self.con)
        self.repo = self.git_repo()

    def call(self, method, path, body=None, query=None):
        return self.api.handle(method, path, query or {}, body, self.identity)

    def test_profile_run_and_cursor_feeds_use_only_v3_vocabulary(self):
        profile = self.call("POST", "/api/v3/profiles", {"name": "Fake", "provider": "fake", "model": "model", "effort": "low", "tier": 2})
        self.assertEqual(profile.status, 201)
        updated = self.call("PATCH", "/api/v3/profiles/fake", {"expected_revision": 1, "note": "default"})
        self.assertEqual(updated.data["data"]["revision"], 2)
        run = self.call("POST", "/api/v3/runs", {"request_id": "api", "profile": "fake", "objective": "work", "cwd": str(self.repo)})
        value = run.data["data"]
        self.assertEqual(value["strategy"], "goal")
        self.assertNotIn("runtime", json.dumps(value))
        events = self.call("GET", f"/api/v3/runs/{value['id']}/events", query={"after": 0})
        self.assertEqual(events.data["data"][0]["type"], "run.queued")
        fleet_events = self.call("GET", "/api/v3/events", query={"after": 0})
        self.assertEqual(fleet_events.data["data"][0]["run_id"], value["id"])
        self.assertEqual(self.call("GET", "/api/v3/usage").data["data"], [])

    def test_group_update_is_revision_guarded(self):
        created = self.call("POST", "/api/v3/groups", {"name": "Build"}).data["data"]
        updated = self.call("PATCH", f"/api/v3/groups/{created['slug']}",
                            {"expected_revision": 1, "name": "Build fleet"})
        self.assertEqual(updated.data["data"]["name"], "Build fleet")
        self.assertEqual(updated.data["data"]["revision"], 2)

    def test_attention_lease_and_service_authorities(self):
        self.call("POST", "/api/v3/profiles", {"name": "Fake", "provider": "fake", "model": "model"})
        run = self.call("POST", "/api/v3/runs", {"request_id": "a", "profile": "fake", "objective": "work", "cwd": str(self.repo)}).data["data"]
        worker = auth.Identity("run", str(run["id"]), auth.RUN_AUTHORITIES, run["id"])
        opened = self.api.handle("POST", f"/api/v3/runs/{run['id']}/attention", {}, {"prompt": "question"}, worker).data["data"]
        service = auth.Identity("service", "s", frozenset({"attention-answer"}))
        lease = self.api.handle("POST", f"/api/v3/attention/{opened['attention_id']}/lease", {}, {"seconds": 30}, service).data["data"]
        answered = self.api.handle("POST", f"/api/v3/attention/{opened['attention_id']}/answer", {}, {"answer": "answer", "lease_id": lease["lease_id"]}, service)
        self.assertEqual(answered.data["data"]["status"], "answered")
        read_service = auth.Identity("service", "reader", frozenset({"read"}))
        with self.assertRaises(api.Problem) as problem:
            self.api.handle("POST", "/api/v3/auth/service-tokens", {},
                            {"name": "escalate", "authorities": ["stop"]}, read_service)
        self.assertEqual(problem.exception.status, 403)

    def test_removed_subsystems_have_no_routes_or_openapi_entries(self):
        document = json.dumps(api.openapi())
        for word in ("runtime", "runway", "observer", "migration", "/api/v2"):
            self.assertNotIn(word, document.lower())
        with self.assertRaises(api.Problem) as problem:
            self.call("GET", "/api/v3/runtimes")
        self.assertEqual(problem.exception.status, 404)

    def test_v2_identity_is_ignored_and_v3_uses_its_own_port(self):
        os.environ["ORCHESTRA_HOME"] = str(self.root / "v2-state")
        try:
            self.assertEqual(config.read()["port"], 8766)
            self.assertNotEqual(str(self.state), os.environ["ORCHESTRA_HOME"])
        finally:
            os.environ.pop("ORCHESTRA_HOME")

if __name__ == "__main__":
    unittest.main()
