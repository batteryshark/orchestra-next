import os
import time
import unittest

from orchestra import acp, dsh
from tests.common import StateCase


class AcpTests(StateCase):
    def peer(self, mode="goal", on_request=None, on_notification=None):
        env = dict(os.environ)
        env["FAKE_DSH_MODE"] = mode
        env["ORCHESTRA_NEXT_DSH_SESSION_ROOT"] = str(self.root / "sessions")
        peer = acp.Peer([dsh.executable(), "--profile", "orchestra-next"], cwd=self.root,
                        env=env, log_path=self.root / "acp.jsonl",
                        on_request=on_request, on_notification=on_notification)
        peer.start()
        return peer

    def test_initialize_new_configure_and_resume(self):
        peer = self.peer()
        try:
            initialized = peer.initialize()
            self.assertIn("resume", initialized["agentCapabilities"]["sessionCapabilities"])
            created = peer.new_session(str(self.root))
            peer.configure(created["sessionId"], "fake", "model", "high")
            prompt = peer.prompt(created["sessionId"], "work")
            deadline = time.time() + 2
            while peer.response(prompt) is None and time.time() < deadline:
                time.sleep(0.02)
            session = created["sessionId"]
        finally:
            peer.close()
        resumed = self.peer()
        try:
            resumed.initialize()
            self.assertIn("configOptions", resumed.resume_session(session, str(self.root)))
        finally:
            resumed.close()

    def test_permission_request_is_answered_exactly(self):
        seen = []
        peer = self.peer("permission", on_request=lambda method, params: (seen.append(method), acp.permission_result(params, True))[1])
        try:
            peer.initialize(); created = peer.new_session(str(self.root))
            request = peer.prompt(created["sessionId"], "permission")
            deadline = time.time() + 2
            while peer.response(request) is None and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(seen, ["session/request_permission"])
        finally:
            peer.close()

    def test_cancel_settles_inflight_prompt(self):
        peer = self.peer("hang")
        try:
            peer.initialize(); created = peer.new_session(str(self.root))
            request = peer.prompt(created["sessionId"], "hang")
            peer.cancel(created["sessionId"])
            deadline = time.time() + 2
            response = None
            while response is None and time.time() < deadline:
                response = peer.response(request)
                time.sleep(0.02)
            self.assertEqual(response["result"]["stopReason"], "cancelled")
        finally:
            peer.close()

    def test_malformed_frame_fails_loudly(self):
        peer = self.peer("malformed")
        try:
            with self.assertRaisesRegex(acp.AcpError, "malformed ACP JSON"):
                peer.initialize()
        finally:
            peer.close()

    def test_live_catalog_uses_standard_config_options(self):
        self.install_profile()
        catalog = dsh.catalog(str(self.root))
        self.assertEqual(catalog[("fake", "model")], {"low", "high"})


if __name__ == "__main__":
    unittest.main()
