import json
import os
import unittest

from orchestra import api, auth, config, db, dsh, scheduler
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

    def test_profile_run_and_cursor_feeds_use_only_next_vocabulary(self):
        profile = self.call("POST", "/api/profiles", {"name": "Fake", "provider": "fake", "model": "model", "effort": "low", "tier": 2})
        self.assertEqual(profile.status, 201)
        updated = self.call("PATCH", "/api/profiles/fake", {"expected_revision": 1, "note": "default"})
        self.assertEqual(updated.data["data"]["revision"], 2)
        run = self.call("POST", "/api/runs", {"request_id": "api", "profile": "fake", "objective": "work", "cwd": str(self.repo)})
        value = run.data["data"]
        self.assertEqual(value["strategy"], "goal")
        self.assertNotIn("runtime", json.dumps(value))
        events = self.call("GET", f"/api/runs/{value['id']}/events", query={"after": 0})
        self.assertEqual(events.data["data"][0]["type"], "run.queued")
        fleet_events = self.call("GET", "/api/events", query={"after": 0})
        self.assertEqual(fleet_events.data["data"][0]["run_id"], value["id"])
        self.assertEqual(self.call("GET", "/api/usage").data["data"], [])

    def test_group_update_is_revision_guarded(self):
        created = self.call("POST", "/api/groups", {"name": "Build"}).data["data"]
        updated = self.call("PATCH", f"/api/groups/{created['slug']}",
                            {"expected_revision": 1, "name": "Build fleet"})
        self.assertEqual(updated.data["data"]["name"], "Build fleet")
        self.assertEqual(updated.data["data"]["revision"], 2)

    def test_attention_lease_and_service_authorities(self):
        self.call("POST", "/api/profiles", {"name": "Fake", "provider": "fake", "model": "model"})
        run = self.call("POST", "/api/runs", {"request_id": "a", "profile": "fake", "objective": "work", "cwd": str(self.repo)}).data["data"]
        worker = auth.Identity("run", str(run["id"]), auth.RUN_AUTHORITIES, run["id"])
        opened = self.api.handle("POST", f"/api/runs/{run['id']}/attention", {}, {"prompt": "question"}, worker).data["data"]
        service = auth.Identity("service", "s", frozenset({"attention-answer"}))
        lease = self.api.handle("POST", f"/api/attention/{opened['attention_id']}/lease", {}, {"seconds": 30}, service).data["data"]
        answered = self.api.handle("POST", f"/api/attention/{opened['attention_id']}/answer", {}, {"answer": "answer", "lease_id": lease["lease_id"]}, service)
        self.assertEqual(answered.data["data"]["status"], "answered")
        read_service = auth.Identity("service", "reader", frozenset({"read"}))
        with self.assertRaises(api.Problem) as problem:
            self.api.handle("POST", "/api/auth/service-tokens", {},
                            {"name": "escalate", "authorities": ["stop"]}, read_service)
        self.assertEqual(problem.exception.status, 403)

    def test_removed_subsystems_have_no_routes_or_openapi_entries(self):
        document = json.dumps(api.openapi())
        for word in ("runtime", "runway", "observer", "migration", "/api/v2"):
            self.assertNotIn(word, document.lower())
        with self.assertRaises(api.Problem) as problem:
            self.call("GET", "/api/runtimes")
        self.assertEqual(problem.exception.status, 404)

    def test_v2_identity_is_ignored_and_next_uses_its_own_port(self):
        os.environ["ORCHESTRA_HOME"] = str(self.root / "v2-state")
        try:
            self.assertEqual(config.read()["port"], 8766)
            self.assertNotEqual(str(self.state), os.environ["ORCHESTRA_HOME"])
        finally:
            os.environ.pop("ORCHESTRA_HOME")


class ConsoleApiTests(StateCase):
    def setUp(self):
        super().setUp()
        self.install_profile()
        dsh._CATALOG = None
        self.identity = auth.Identity("device", "op", frozenset({"*"}))
        self.api = api.API(self.con)
        self.repo = self.git_repo()

    def tearDown(self):
        dsh._CATALOG = None
        super().tearDown()

    def call(self, method, path, body=None, query=None, identity="device"):
        who = self.identity if identity == "device" else identity
        return self.api.handle(method, path, query or {}, body, who)

    def submit(self, request_id, **overrides):
        body = {"request_id": request_id, "profile": "fake", "objective": "work", "cwd": str(self.repo)}
        body.update(overrides)
        return self.call("POST", "/api/runs", body).data["data"]

    def test_auth_me_reports_each_identity_kind(self):
        anonymous = self.call("GET", "/api/auth/me", identity=None).data["data"]
        self.assertEqual(anonymous, {"authenticated": False})
        service = auth.Identity("service", "svc", frozenset({"read"}))
        value = self.call("GET", "/api/auth/me", identity=service).data["data"]
        self.assertEqual((value["kind"], value["authorities"]), ("service", ["read"]))
        worker = auth.Identity("run", "7", auth.RUN_AUTHORITIES, 7)
        value = self.call("GET", "/api/auth/me", identity=worker).data["data"]
        self.assertEqual((value["kind"], value["run_id"]), ("run", 7))

    def test_models_endpoint_lists_catalog_and_serves_cache(self):
        value = self.call("GET", "/api/models").data["data"]
        self.assertEqual(value["models"], [{"provider": "fake", "model": "model", "efforts": ["high", "low"]}])
        self.assertIn("checked_at", value)
        original = dsh.catalog
        dsh.catalog = lambda cwd: (_ for _ in ()).throw(dsh.DshError("no probe expected"))
        try:
            cached = self.call("GET", "/api/models").data["data"]
            self.assertEqual(cached["models"], value["models"])
            throttled = self.call("GET", "/api/models", query={"refresh": "1"}).data["data"]
            self.assertEqual(throttled["checked_at"], value["checked_at"], "a refresh within the floor serves the cache")
        finally:
            dsh.catalog = original
        with self.assertRaises(api.Problem) as problem:
            self.call("POST", "/api/auth/bootstrap", {"name": "x"})
        self.assertEqual(problem.exception.status, 404)
        os.environ["ORCHESTRA_NEXT_DSH"] = "/nonexistent/dsh"
        dsh._CATALOG = None  # an empty cache is never throttled
        with self.assertRaises(api.Problem) as problem:
            self.call("GET", "/api/models", query={"refresh": "1"})
        self.assertEqual(problem.exception.status, 503)

    def test_runs_filters_order_and_limit(self):
        self.create_profile()
        first = self.submit("one")
        second = self.submit("two")
        self.con.execute("UPDATE runs SET status='failed',finished_at=? WHERE id=?", (db.now(), first["id"]))
        self.con.commit()
        newest = self.call("GET", "/api/runs", query={"order": "desc", "limit": "1"}).data["data"]
        self.assertEqual([run["id"] for run in newest], [second["id"]])
        failed = self.call("GET", "/api/runs", query={"status": "failed"}).data["data"]
        self.assertEqual([run["id"] for run in failed], [first["id"]])
        ascending = self.call("GET", "/api/runs", query={"after": 0}).data["data"]
        self.assertEqual([run["id"] for run in ascending], [first["id"], second["id"]])
        grouped = self.call("GET", "/api/runs", query={"group": "general"}).data["data"]
        self.assertEqual(len(grouped), 2)
        with self.assertRaises(api.Problem) as problem:
            self.call("GET", "/api/runs", query={"group": "missing"})
        self.assertEqual(problem.exception.status, 400)
        with self.assertRaises(api.Problem) as problem:
            self.call("GET", "/api/runs", query={"status": "bogus"})
        self.assertEqual(problem.exception.status, 400)

    def test_messages_ledger_pages_and_filters(self):
        self.create_profile()
        first = self.submit("one")
        second = self.submit("two")
        self.call("POST", f"/api/runs/{first['id']}/tell", {"message": "first tell"})
        self.call("POST", f"/api/runs/{second['id']}/pause", {})
        self.call("POST", f"/api/runs/{second['id']}/tell", {"message": "second tell"})
        rows = self.call("GET", "/api/messages").data["data"]
        self.assertEqual([(row["run_id"], row["kind"], row["status"]) for row in rows],
                         [(second["id"], "tell", "queued"), (second["id"], "pause", "queued"), (first["id"], "tell", "queued")])
        self.assertEqual(rows[0]["body"], "second tell")
        self.assertIn("run_title", rows[0])
        page = self.call("GET", "/api/messages", query={"limit": "1"}).data["data"]
        older = self.call("GET", "/api/messages", query={"limit": "1", "before": str(page[0]["id"])}).data["data"]
        self.assertEqual([row["kind"] for row in page + older], ["tell", "pause"])
        self.assertEqual([row["run_id"] for row in self.call("GET", "/api/messages", query={"run": str(first["id"])}).data["data"]], [first["id"]])
        self.assertEqual(len(self.call("GET", "/api/messages", query={"kind": "pause"}).data["data"]), 1)
        self.assertEqual(self.call("GET", "/api/messages", query={"status": "delivered"}).data["data"], [])
        for bad in ({"status": "bogus"}, {"kind": "shout"}, {"before": "x"}):
            with self.assertRaises(api.Problem) as problem:
                self.call("GET", "/api/messages", query=bad)
            self.assertEqual(problem.exception.status, 400)

    def test_device_and_service_token_management(self):
        keep, _ = auth.bootstrap_device(self.con, "Keep")
        pairing = auth.create_pairing(self.con, created_by_device_id=keep["device_id"])
        extra, _ = auth.redeem_pairing(self.con, "", pairing["code"], "Extra")
        listed = self.call("GET", "/api/auth/devices").data["data"]
        self.assertEqual({row["name"] for row in listed}, {"Keep", "Extra"})
        self.assertTrue(all("token_hash" not in row for row in listed))
        revoked = self.call("POST", f"/api/auth/devices/{extra['device_id']}/revoke").data["data"]
        self.assertTrue(revoked["revoked"])
        with self.assertRaises(api.Problem) as problem:
            self.call("POST", f"/api/auth/devices/{keep['device_id']}/revoke")
        self.assertEqual(problem.exception.status, 409)
        service, _ = auth.create_service_token(self.con, "poller", ["read"])
        listed = self.call("GET", "/api/auth/service-tokens").data["data"]
        self.assertEqual(listed[0]["authorities"], ["read"])
        self.assertTrue(self.call("POST", f"/api/auth/service-tokens/{service['token_id']}/revoke").data["data"]["revoked"])

    def test_board_revision_bumps_on_every_mutation(self):
        revisions = [db.board_revision(self.con)]
        self.call("POST", "/api/profiles", {"name": "Fake", "provider": "fake", "model": "model"})
        revisions.append(db.board_revision(self.con))
        run = self.submit("bump")
        revisions.append(db.board_revision(self.con))
        self.call("POST", f"/api/runs/{run['id']}/tell", {"message": "hello"})
        revisions.append(db.board_revision(self.con))
        self.con.execute("UPDATE runs SET status='waiting',waiting_kind='attention' WHERE id=?", (run["id"],))
        self.con.commit()
        self.call("POST", f"/api/runs/{run['id']}/resume", {})
        revisions.append(db.board_revision(self.con))
        self.call("POST", f"/api/runs/{run['id']}/pin", {"reason": "keep"})
        revisions.append(db.board_revision(self.con))
        self.assertEqual(revisions, sorted(set(revisions)), "board_revision must strictly increase")
        blocker = self.submit("blocker")
        self.con.execute("UPDATE runs SET status='failed',finished_at=? WHERE id=?", (db.now(), blocker["id"]))
        self.con.commit()
        dependent = self.submit("dependent", after=[{"run_id": blocker["id"], "condition": "success"}])
        before = db.board_revision(self.con)
        decision = scheduler.admit(self.con)
        self.assertIn(dependent["id"], decision["skipped"])
        self.assertGreater(db.board_revision(self.con), before)

    def test_pause_queues_and_payload_reports_paused(self):
        self.create_profile()
        run = self.submit("pausable")
        receipt = self.call("POST", f"/api/runs/{run['id']}/pause", {"message": "hold for review"})
        self.assertEqual(receipt.status, 202)
        self.assertEqual(receipt.data["data"]["kind"], "pause")
        with self.con:
            self.con.execute("UPDATE runs SET status='waiting',waiting_kind=NULL,"
                             "waiting_detail='paused: hold for review' WHERE id=?", (run["id"],))
        value = self.call("GET", f"/api/runs/{run['id']}").data["data"]
        self.assertTrue(value["paused"])
        listed = self.call("GET", "/api/runs", query={"status": "waiting"}).data["data"]
        self.assertTrue(listed[0]["paused"])

    def test_merge_refusal_is_a_409_with_the_reason(self):
        self.create_profile()
        run = self.submit("unmerged")
        with self.assertRaises(api.Problem) as problem:
            self.call("POST", f"/api/runs/{run['id']}/merge", {})
        self.assertEqual(problem.exception.status, 409)
        self.assertIn("branch", str(problem.exception))
        refused = self.con.execute("SELECT outcome FROM control_events WHERE action='run.merge'").fetchone()
        self.assertEqual(refused["outcome"], "refused")

    def test_profile_patch_reports_the_validation_detail(self):
        self.create_profile()
        with self.assertRaises(api.Problem) as problem:
            self.call("PATCH", "/api/profiles/fake", {"expected_revision": 1, "effort": "bogus"})
        self.assertEqual(problem.exception.status, 400)
        self.assertIn("bogus", str(problem.exception))
        with self.assertRaises(api.Problem) as problem:
            self.call("PATCH", "/api/profiles/fake", {"note": "no revision"})
        self.assertIn("expected_revision", str(problem.exception))
        with self.assertRaises(api.Problem) as problem:
            self.call("PATCH", "/api/profiles/fake", {"expected_revision": 1, "actor": "spoof"})
        self.assertEqual(problem.exception.status, 400)
        self.assertIn("actor", str(problem.exception))

    def test_readiness_reports_dsh_and_claude_state(self):
        value = self.call("GET", "/api/readiness").data["data"]
        self.assertEqual(value["schema"], db.SCHEMA_VERSION)
        self.assertTrue(value["dsh"]["ok"])
        self.assertFalse(value["claude"]["ok"])
        with self.assertRaises(api.Problem) as problem:
            self.call("GET", "/api/readiness", identity=None)
        self.assertEqual(problem.exception.status, 401)


if __name__ == "__main__":
    unittest.main()
