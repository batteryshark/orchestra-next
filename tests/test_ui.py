"""Browser transport: static console, security headers, cookies, CSRF."""
import http.client
import json
import threading
import unittest

from orchestra import artifacts, auth, config, http as transport, paths
from tests.common import StateCase


class UiTransportCase(StateCase):
    def setUp(self):
        super().setUp()
        self.server = transport.make_server(addr="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.device, self.token = auth.bootstrap_device(self.con, "Test operator")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def request(self, method, path, *, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        encoded = None if body is None else json.dumps(body).encode()
        sent = dict(headers or {})
        if encoded is not None:
            sent.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=encoded, headers=sent)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        try:
            value = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            value = raw
        return response, value

    def pairing_code(self):
        return auth.create_pairing(self.con, created_by_device_id=self.device["device_id"])

    def redeem_cookie(self, name="Browser"):
        pairing = self.pairing_code()
        response, value = self.request("POST", "/api/auth/pair/redeem",
                                       body={"code": pairing["code"], "name": name, "cookie": True})
        self.assertEqual(response.status, 201)
        header = response.getheader("Set-Cookie") or ""
        raw = header.split(";")[0]
        self.assertTrue(raw.startswith(auth.COOKIE_NAME + "="))
        return raw, value, header


class DocumentTests(UiTransportCase):
    def test_serves_document_css_js_with_types_and_no_cache(self):
        for path, expected in (("/", "text/html"), ("/app.css", "text/css"), ("/app.js", "text/javascript")):
            response, _ = self.request("GET", path)
            self.assertEqual(response.status, 200, path)
            self.assertIn(expected, response.getheader("Content-Type", ""), path)
            self.assertEqual(response.getheader("Cache-Control"), "no-cache", path)
        response, _ = self.request("GET", "/nope")
        self.assertEqual(response.status, 404)

    def test_security_headers_on_document_and_api(self):
        for path in ("/", "/api/health"):
            response, _ = self.request(path=path, method="GET")
            self.assertEqual(response.getheader("Content-Security-Policy"), transport.CSP, path)
            self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff", path)
            self.assertEqual(response.getheader("Referrer-Policy"), "no-referrer", path)

    def test_host_header_validation_rejects_rebinding(self):
        for path in ("/", "/api/health"):
            response, value = self.request("GET", path, headers={"Host": "evil.example:8766"})
            self.assertEqual(response.status, 400, path)
            self.assertIn("Host", value["error"]["message"])
        for host in (f"localhost:{self.port}", "[::1]:8766", f"127.0.0.1:{self.port}"):
            response, _ = self.request("GET", "/api/health", headers={"Host": host})
            self.assertEqual(response.status, 200, host)


class CookieAuthTests(UiTransportCase):
    def test_pair_redeem_with_cookie_flag_sets_httponly_cookie_and_hides_token(self):
        pairing = self.pairing_code()
        response, value = self.request("POST", "/api/auth/pair/redeem",
                                       body={"code": pairing["code"], "name": "Browser", "cookie": True})
        self.assertEqual(response.status, 201)
        header = response.getheader("Set-Cookie")
        self.assertIn("HttpOnly", header)
        self.assertIn("SameSite=Strict", header)
        self.assertIn("Path=/", header)
        self.assertNotIn("token", value["data"])
        replay, value = self.request("POST", "/api/auth/pair/redeem",
                                     body={"code": pairing["code"], "name": "Browser", "cookie": True})
        self.assertEqual(replay.status, 400)

    def test_cookie_authenticates_get_and_me(self):
        cookie, _, _ = self.redeem_cookie("Console")
        response, value = self.request("GET", "/api/auth/me", headers={"Cookie": cookie})
        self.assertEqual(response.status, 200)
        self.assertTrue(value["data"]["authenticated"])
        self.assertEqual(value["data"]["kind"], "device")
        self.assertEqual(value["data"]["device"]["name"], "Console")
        response, value = self.request("GET", "/api/auth/me")
        self.assertEqual(response.status, 200)
        self.assertFalse(value["data"]["authenticated"])

    def test_cookie_mutation_requires_matching_origin(self):
        cookie, _, _ = self.redeem_cookie()
        response, value = self.request("POST", "/api/auth/pair", body={}, headers={"Cookie": cookie})
        self.assertEqual(response.status, 403)
        self.assertIn("same-origin", value["error"]["message"])
        response, _ = self.request("POST", "/api/auth/pair", body={},
                                   headers={"Cookie": cookie, "Origin": "http://localhost:3000"})
        self.assertEqual(response.status, 403)
        response, value = self.request("POST", "/api/auth/pair", body={},
                                       headers={"Cookie": cookie, "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(response.status, 201)
        self.assertIn("code", value["data"])

    def test_bearer_mutations_skip_csrf(self):
        response, value = self.request("POST", "/api/auth/pair", body={},
                                       headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(response.status, 201)

    def test_logout_clears_cookie_and_revokes_browser_device(self):
        cookie, _, _ = self.redeem_cookie()
        response, value = self.request("POST", "/api/auth/logout",
                                       headers={"Cookie": cookie, "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(response.status, 200)
        self.assertIn("Max-Age=0", response.getheader("Set-Cookie"))
        response, value = self.request("GET", "/api/auth/me", headers={"Cookie": cookie})
        self.assertFalse(value["data"]["authenticated"])

    def test_untrusted_network_still_requires_credentials(self):
        response, value = self.request("GET", "/api/auth/me")
        self.assertFalse(value["data"]["authenticated"])
        response, _ = self.request("GET", "/api/runs")
        self.assertEqual(response.status, 401)

    def test_artifact_download_disposition_and_cookie_auth(self):
        cookie, _, _ = self.redeem_cookie()
        self.install_profile()
        self.create_profile()
        repo = self.git_repo()
        origin = {"Cookie": cookie, "Origin": f"http://127.0.0.1:{self.port}"}
        response, value = self.request("POST", "/api/runs", headers=origin,
                                       body={"request_id": "ui-artifact", "profile": "fake",
                                             "objective": "work", "cwd": str(repo)})
        self.assertEqual(response.status, 201)
        run_id = value["data"]["id"]
        work = self.root / "work"
        work.mkdir()
        (work / "result.txt").write_text("evidence", encoding="utf-8")
        self.con.execute("UPDATE runs SET workdir=? WHERE id=?", (str(work), run_id))
        self.con.commit()
        published = artifacts.publish(self.con, run_id, "result.txt")
        response, raw = self.request("GET", f"/api/artifacts/{published['artifact_id']}/content",
                                     headers={"Cookie": cookie})
        self.assertEqual(response.status, 200)
        self.assertEqual(raw, b"evidence")
        disposition = response.getheader("Content-Disposition")
        self.assertIn("attachment", disposition)
        self.assertIn("filename*=UTF-8''result.txt", disposition)


class TrustedNetworkTests(UiTransportCase):
    def setUp(self):
        StateCase.setUp(self)
        config.write({"trusted_cidrs": ["127.0.0.0/8"], "allowed_hosts": ["orchestra.example.internal"]},
                     paths.bootstrap_path())
        self.server = transport.make_server(addr="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.device, self.token = auth.bootstrap_device(self.con, "Test operator")

    def test_trusted_peer_is_an_operator_without_credentials(self):
        response, value = self.request("GET", "/api/auth/me")
        self.assertEqual(response.status, 200)
        self.assertTrue(value["data"]["authenticated"])
        self.assertEqual(value["data"]["kind"], "network")
        response, _ = self.request("GET", "/api/runs")
        self.assertEqual(response.status, 200)

    def test_trusted_peer_mutations_allow_bare_clients_but_not_cross_origin(self):
        response, value = self.request("POST", "/api/auth/pair", body={})
        self.assertEqual(response.status, 201)
        self.assertIn("code", value["data"])
        response, value = self.request("POST", "/api/auth/pair", body={},
                                       headers={"Origin": "http://localhost:3000"})
        self.assertEqual(response.status, 403)
        response, _ = self.request("POST", "/api/auth/pair", body={},
                                   headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(response.status, 201)

    def test_trust_extends_host_allowlist(self):
        for host in ("orchestra.example.internal:8766", "machine.tail1234.ts.net", "127.0.0.99:8766"):
            response, _ = self.request("GET", "/api/health", headers={"Host": host})
            self.assertEqual(response.status, 200, host)
        response, _ = self.request("GET", "/api/health", headers={"Host": "evil.example:8766"})
        self.assertEqual(response.status, 400)

    def test_invalid_bearer_from_trusted_peer_still_operates(self):
        response, value = self.request("GET", "/api/auth/me",
                                       headers={"Authorization": "Bearer od_bogus"})
        self.assertEqual(response.status, 200)
        self.assertEqual(value["data"]["kind"], "network")


class TrustConfigTests(StateCase):
    def test_bootstrap_rejects_bad_trust_values(self):
        with self.assertRaises(config.ConfigError):
            config._validate({"trusted_cidrs": ["not-a-network"]})
        with self.assertRaises(config.ConfigError):
            config._validate({"trust_tailnet": "yes"})
        value = config._validate({"trust_tailnet": True, "trust_loopback": True})
        self.assertTrue(value["trust_tailnet"])

    def test_tailnet_flag_trusts_cgnat_range(self):
        config.write({"trust_tailnet": True}, paths.bootstrap_path())
        server = transport.make_server(addr="127.0.0.1", port=0)
        try:
            import ipaddress
            self.assertTrue(any(ipaddress.ip_address("100.101.102.103") in network
                                for network in server.trusted_networks))
            self.assertFalse(any(ipaddress.ip_address("127.0.0.1") in network
                                 for network in server.trusted_networks))
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
