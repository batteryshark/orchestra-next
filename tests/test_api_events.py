"""Event feed paging: order, limit, and before/after cursors."""
import unittest

from orchestra import api, auth, db
from tests.common import StateCase


class EventPagingTests(StateCase):
    def setUp(self):
        super().setUp()
        self.install_profile()
        self.create_profile()
        self.identity = auth.Identity("device", "op", frozenset({"*"}))
        self.api = api.API(self.con)
        repo = str(self.git_repo())
        self.run_id = self.call("POST", "/api/runs", {"request_id": "one", "profile": "fake", "objective": "work", "cwd": repo})["id"]
        self.other_id = self.call("POST", "/api/runs", {"request_id": "two", "profile": "fake", "objective": "work", "cwd": repo})["id"]
        for seq in range(4):
            db.append_event(self.con, self.run_id, f"test.{seq}", {"seq": seq}, session_id="s", source_seq=seq)
        self.con.commit()

    def call(self, method, path, body=None, query=None):
        return self.api.handle(method, path, query or {}, body, self.identity).data["data"]

    def events(self, query=None, path=None):
        return self.call("GET", path or f"/api/runs/{self.run_id}/events", query=query)

    def test_default_page_is_ascending_and_unchanged(self):
        default = self.events()
        self.assertEqual([row["type"] for row in default], ["run.queued", "test.0", "test.1", "test.2", "test.3"])
        self.assertTrue(all(row["run_id"] == self.run_id for row in default))
        self.assertEqual(default, self.events({"after": 0}))
        self.assertEqual(default, self.events({"order": "asc", "limit": "500"}))
        self.assertEqual(self.events({"after": default[2]["id"]}), default[3:])
        self.assertEqual(default[0]["payload"], self.events()[0]["payload"])

    def test_desc_limit_and_before_page_backwards(self):
        ids = [row["id"] for row in self.events()]
        newest = self.events({"order": "desc", "limit": "2"})
        self.assertEqual([row["id"] for row in newest], ids[-1:-3:-1])
        earlier = self.events({"order": "desc", "limit": "2", "before": str(newest[-1]["id"])})
        self.assertEqual([row["id"] for row in earlier], ids[-3:-5:-1])
        self.assertEqual([row["id"] for row in self.events({"before": ids[1]})], ids[:1])
        self.assertEqual(len(self.events({"limit": "0"})), 1)
        self.assertEqual(self.events({"order": "desc", "before": ids[0]}), [])

    def test_global_feed_takes_before_and_order(self):
        ids = [row["id"] for row in self.events(path="/api/events")]
        self.assertEqual(len(ids), 6)
        page = self.events({"order": "desc", "limit": "3", "before": str(ids[-1])}, path="/api/events")
        self.assertEqual([row["id"] for row in page], ids[-2:-5:-1])
        self.assertEqual({row["run_id"] for row in self.events(path="/api/events")}, {self.run_id, self.other_id})

    def test_bad_query_values_are_400(self):
        for path in (f"/api/runs/{self.run_id}/events", "/api/events"):
            for query in ({"order": "sideways"}, {"limit": "many"}, {"before": "x"}, {"after": "1.5"}):
                with self.assertRaises(api.Problem) as problem:
                    self.events(query, path=path)
                self.assertEqual(problem.exception.status, 400, (path, query))


if __name__ == "__main__":
    unittest.main()
