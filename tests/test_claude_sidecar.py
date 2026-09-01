import json
import os
import stat
import subprocess
import textwrap
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from orchestra import claude, paths
from tests.common import StateCase


class ClaudeSidecarTests(StateCase):
    def _installed(self, runner: str | None = None) -> Path:
        directory, _ = claude._copy_source()
        bridge = directory / "node_modules/@openchamber/opencode-claude"
        bun = directory / "node_modules/bun"
        binary = directory / "node_modules/.bin/bun"
        bridge.mkdir(parents=True)
        bun.mkdir(parents=True)
        binary.parent.mkdir(parents=True, exist_ok=True)
        (bridge / "package.json").write_text(json.dumps({"version": claude.BRIDGE_VERSION}))
        (bun / "package.json").write_text(json.dumps({"version": claude.BUN_VERSION}))
        binary.write_text(runner or "#!/bin/sh\nexit 0\n")
        binary.chmod(0o700)
        return directory

    def test_setup_installs_exact_pins_without_authentication(self):
        def install(command, **kwargs):
            self._installed()
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(subprocess, "run", side_effect=install) as run:
            directory, changed = claude.setup()
        self.assertTrue(changed)
        self.assertEqual(directory, paths.claude_sidecar_dir())
        self.assertEqual(run.call_args.args[0][1:], ["ci", "--no-audit", "--no-fund"])
        self.assertEqual(claude.check(require_auth=False)["bridge_version"], "0.14.0")

    def test_check_rejects_missing_install_clearly(self):
        with self.assertRaisesRegex(claude.ClaudeError, "claude setup"):
            claude.check(require_auth=False)

    def test_per_run_sidecars_use_distinct_authenticated_ephemeral_ports_and_cleanup(self):
        runner = textwrap.dedent("""\
            #!/usr/bin/env python3
            import json, signal, sys
            from http.server import BaseHTTPRequestHandler, HTTPServer
            auth = sys.argv[sys.argv.index('--auth-file') + 1]
            token = json.load(open(auth))['token']
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args): pass
                def do_GET(self):
                    if self.headers.get('authorization') != 'Bearer ' + token:
                        self.send_response(401); self.end_headers(); return
                    self.send_response(200); self.send_header('content-type', 'application/json')
                    self.end_headers(); self.wfile.write(b'{"ok":true}')
            server = HTTPServer(('127.0.0.1', 0), Handler)
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
            print(json.dumps({'url': 'http://127.0.0.1:%d/v1' % server.server_port}), flush=True)
            server.serve_forever()
        """)
        directory = self._installed(runner)
        with mock.patch.object(claude, "check", return_value={"path": str(directory)}):
            first = claude.start(1, self.root)
            second = claude.start(2, self.root)
        try:
            self.assertNotEqual(first.url, second.url)
            self.assertEqual(stat.S_IMODE(first.auth_file.stat().st_mode), 0o600)
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(first.url + "/health", timeout=2)
            self.assertEqual(denied.exception.code, 401)
            denied.exception.close()
        finally:
            first.close(); second.close()
        self.assertFalse(first.auth_file.exists())
        self.assertIsNotNone(first.process.poll())

    def test_malformed_startup_is_bounded_and_cleans_auth(self):
        directory = self._installed("#!/bin/sh\necho not-json\n")
        with mock.patch.object(claude, "check", return_value={"path": str(directory)}):
            with self.assertRaisesRegex(claude.ClaudeError, "malformed startup"):
                claude.start(3, self.root)
        self.assertFalse(paths.run_claude_auth_path(3).exists())


if __name__ == "__main__":
    unittest.main()
