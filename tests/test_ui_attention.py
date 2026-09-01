"""attentionLeadUp (app.js attention-logic block), sliced by sentinel and run under node."""
import json
import shutil
import unittest

from tests.test_ui_js import run_node, slice_section


def assistant(event_id, at, content):
    return {"id": event_id, "created_at": at, "type": "dsh.assistant/message",
            "payload": {"message": {"role": "assistant", "content": content}}}


def tool_call(event_id, at, title):
    return {"id": event_id, "created_at": at, "type": "acp.tool_call",
            "payload": {"sessionUpdate": "tool_call", "toolCallId": f"call-{event_id}", "title": title,
                        "status": "pending", "rawInput": {"command": "ls"}}}


T = [f"2026-01-01T00:00:{i:02d}+00:00" for i in range(10)]
OPENED = T[6]

# Newest first, like GET /api/runs/{id}/events?order=desc.
EVENTS = [
    assistant(9, T[8], [{"type": "text", "text": "after the question"}]),
    {"id": 8, "created_at": T[7], "type": "attention.opened", "payload": {"kind": "question"}},
    {"id": 7, "created_at": T[6], "type": "verification.failed", "payload": {"repair": 1, "output": "pytest: 2 failed\nmore"}},
    {"id": 6, "created_at": T[5], "type": "dsh.assistant/chunk", "payload": {"text": "stream noise"}},
    assistant(5, T[5], [{"type": "reasoning", "text": "\nonly thinking here"}, {"type": "tool-call", "id": "c1", "name": "read"}]),
    tool_call(4, T[4], "bash: pytest -q"),
    assistant(3, T[3], [{"type": "reasoning", "text": "hidden"}, {"type": "text", "text": "I will run the tests.\nsecond line"}]),
    {"id": 2, "created_at": T[2], "type": "dsh.user/message", "payload": {"content": "prompt", "source": {"kind": "user"}}},
    assistant(1, T[1], [{"type": "tool-call", "id": "c0", "name": "read"}]),
    tool_call(0, T[0], "read: README.md"),
]


class AttentionLeadUpTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("node") is None:
            self.skipTest("node is unavailable")
        self.prelude = "".join(slice_section(name) for name in ("constants", "fmt", "logic", "attention-logic"))

    def evaluate(self, expression: str):
        return run_node(self.prelude + f"\nconsole.log(JSON.stringify({expression}));")

    def test_last_four_meaningful_rows_before_created_at_oldest_first(self):
        rows = self.evaluate(f"attentionLeadUp({json.dumps(EVENTS)}, {json.dumps(OPENED)})")
        self.assertEqual([row["kind"] for row in rows], ["text", "tool", "reasoning", "verification"])
        self.assertEqual([row["at"] for row in rows], [T[3], T[4], T[5], T[6]])
        self.assertEqual(rows[0]["text"], "💬 I will run the tests.")
        self.assertEqual(rows[1]["text"], "⚙ bash: pytest -q")
        self.assertEqual(rows[2]["text"], "💭 only thinking here")
        self.assertEqual(rows[3]["text"], "❌ verification failed — pytest: 2 failed")
        self.assertEqual(sorted(rows[0]), ["at", "kind", "text"])

    def test_limit_and_long_text_excerpt(self):
        rows = self.evaluate(f"attentionLeadUp({json.dumps(EVENTS)}, {json.dumps(OPENED)}, 2)")
        self.assertEqual([row["kind"] for row in rows], ["reasoning", "verification"])
        long = [assistant(1, T[1], [{"type": "text", "text": "x" * 500}])]
        rows = self.evaluate(f"attentionLeadUp({json.dumps(long)}, {json.dumps(OPENED)})")
        self.assertEqual(len(rows[0]["text"]), len("💬 ") + 200)
        self.assertTrue(rows[0]["text"].endswith("…"))

    def test_nothing_meaningful_gives_empty_list(self):
        noise = [EVENTS[1], EVENTS[3], EVENTS[7], EVENTS[8]]
        self.assertEqual(self.evaluate(f"attentionLeadUp({json.dumps(noise)}, {json.dumps(OPENED)})"), [])
        self.assertEqual(self.evaluate("attentionLeadUp([], null)"), [])
        early = self.evaluate(f"attentionLeadUp({json.dumps(EVENTS)}, {json.dumps(T[0])})")
        self.assertEqual([row["text"] for row in early], ["⚙ read: README.md"])


if __name__ == "__main__":
    unittest.main()
