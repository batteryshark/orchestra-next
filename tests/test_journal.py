import json
import unittest

from orchestra import journal
from orchestra.contracts import RunRequest
from orchestra import runs
from tests.common import StateCase


class JournalTests(StateCase):
    def setUp(self):
        super().setUp()
        self.create_profile()
        repo = self.git_repo()
        self.run, _ = runs.submit(self.con, RunRequest.from_mapping({"request_id": "journal", "profile": "fake", "objective": "test", "cwd": str(repo)}))
        self.sessions = self.root / "sessions"

    def write(self, name, rows, torn=None):
        path = self.sessions / name / "session.jsonl"
        path.parent.mkdir(parents=True)
        with path.open("wb") as handle:
            for index, row in enumerate(rows):
                row = dict(row)
                if index == 0:
                    row.setdefault("delegationDepth", 0)
                else:
                    row.setdefault("seq", index - 1)
                    row.setdefault("time", index + 1)
                handle.write(json.dumps(row).encode() + b"\n")
            if torn:
                handle.write(torn)
        return path

    def test_goal_compaction_usage_and_descendants_are_idempotent(self):
        usage = {"type": "assistant/message", "data": {"message": {"source": {"provider": "fake", "model": "model"}}, "usage": {"inputTokens": 10, "outputTokens": 4, "cacheReadTokens": 6, "cacheWriteTokens": 2, "totalTokens": 22}}}
        self.write("root", [
            {"type": "session", "version": 0, "id": "root", "createdAt": 1, "cwd": str(self.root)},
            {"type": "goal/change", "data": {"kind": "goal/change", "version": 1, "operation": "create", "goal": {"id": "g", "revision": 1, "objective": "fixture", "phase": "active", "maxGoalRounds": 32}, "roundsStarted": 0, "createdAt": 1, "updatedAt": 1}},
            {"type": "assistant/chunk", "data": {"turn": 1, "step": 1, "chunk": {"type": "text-delta", "delta": "streamed"}}},
            {"type": "compaction/summary", "data": {"compactionId": "c", "provider": "fake", "model": "compact", "summary": "short", "usage": {"inputTokens": 2, "outputTokens": 1, "totalTokens": 3}}}, usage,
            {"type": "goal/change", "data": {"kind": "goal/change", "version": 1, "operation": "complete", "goal": {"id": "g", "revision": 2, "objective": "fixture", "phase": "complete", "maxGoalRounds": 32}, "roundsStarted": 3, "createdAt": 1, "updatedAt": 2}},
        ], torn=b'{"type":')
        self.write("child", [
            {"type": "session", "version": 0, "id": "child", "parentSession": "root", "createdAt": 2, "cwd": str(self.root)}, usage,
        ])
        first = journal.reconcile(self.con, self.run["id"], self.sessions)
        second = journal.reconcile(self.con, self.run["id"], self.sessions)
        self.assertTrue(first["goal_complete"])
        self.assertEqual(second["events"], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM events WHERE type LIKE '%/chunk'").fetchone()[0], 0, "stream fragments are not projected")
        row = runs.find(self.con, self.run["id"])
        self.assertEqual((row["goal_state"], row["rounds_started"]), ("complete", 3))
        self.assertEqual((row["tokens_input"], row["tokens_output"], row["tokens_cache_read"], row["tokens_cache_write"], row["tokens_total"]), (22, 9, 12, 4, 47))
        descendant = self.con.execute("SELECT descendant_session_id FROM usage_events WHERE session_id='child'").fetchone()[0]
        self.assertEqual(descendant, "child")
        compact = self.con.execute("SELECT source_seq,event_type,model FROM usage_events WHERE event_type='compaction/summary'").fetchone()
        self.assertEqual(tuple(compact), (2, "compaction/summary", "compact"))  # seq 1 was the skipped chunk

    def test_committed_corruption_and_format_version_are_rejected(self):
        self.write("bad", [{"type": "session", "version": 1, "id": "bad"}])
        with self.assertRaises(journal.JournalError):
            journal.reconcile(self.con, self.run["id"], self.sessions)

    def test_committed_event_without_dsh_sequence_is_rejected_atomically(self):
        path = self.sessions / "missing-seq" / "session.jsonl"
        path.parent.mkdir(parents=True)
        rows = [
            {"type": "session", "version": 0, "id": "missing", "createdAt": 1,
             "delegationDepth": 0},
            {"type": "assistant/message", "time": 2, "data": {"usage": {
                "inputTokens": 1, "outputTokens": 1}}},
        ]
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        before = self.con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        with self.assertRaises(journal.JournalError):
            journal.reconcile(self.con, self.run["id"], self.sessions)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM events").fetchone()[0], before)


if __name__ == "__main__":
    unittest.main()
