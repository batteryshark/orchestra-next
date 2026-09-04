import os
import plistlib
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from orchestra import service
from tests.common import StateCase


def _cp(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


class ServiceTests(StateCase):
    def setUp(self):
        super().setUp()
        self.launch = mock.patch("orchestra.service.subprocess.run", return_value=_cp(1)).start()
        mock.patch("orchestra.service.Path.home", return_value=self.root).start()
        self.addCleanup(mock.patch.stopall)

    def calls(self):
        return [c.args[0] for c in self.launch.call_args_list]

    def test_plist_contents(self):
        data = service.build_plist()
        self.assertEqual(data["Label"], "local.orchestra-next.daemon")
        self.assertEqual(data["ProgramArguments"], [sys.executable, "-m", "orchestra", "daemon"])
        self.assertTrue((Path(data["WorkingDirectory"]) / "orchestra" / "__init__.py").exists())
        self.assertTrue(data["RunAtLoad"] and data["KeepAlive"])
        log = str(self.state / "logs" / "daemon.log")
        self.assertEqual((data["StandardOutPath"], data["StandardErrorPath"]), (log, log))
        self.assertEqual(data["EnvironmentVariables"]["PATH"], os.environ["PATH"])
        self.assertEqual(data["EnvironmentVariables"]["ORCHESTRA_NEXT_HOME"], str(self.state))
        self.assertEqual(service.plist_path(), self.root / "Library/LaunchAgents/local.orchestra-next.daemon.plist")

    def test_install_writes_plist_and_bootstraps(self):
        self.launch.side_effect = [_cp(1), _cp(0), _cp(0), _cp(0, "\tstate = running\n\tpid = 42\n")]
        with mock.patch("sys.stdout"):
            self.assertEqual(service.install(start=True), 0)
        path = service.plist_path()
        self.assertEqual(plistlib.loads(path.read_bytes())["Label"], service.LABEL)
        self.assertFalse(path.with_suffix(".plist.tmp").exists())
        uid = os.getuid()
        self.assertEqual(self.calls(), [
            ["pgrep", "-f", "-- -m orchestra daemon"],
            ["launchctl", "bootstrap", f"gui/{uid}", str(path)],
            ["launchctl", "kickstart", "-k", f"gui/{uid}/{service.LABEL}"],
            ["launchctl", "print", f"gui/{uid}/{service.LABEL}"],
        ])

    def test_install_refuses_when_foreground_daemon_runs(self):
        self.launch.return_value = _cp(0, "4242\n")
        with mock.patch("sys.stderr"):
            self.assertEqual(service.install(), 1)
        self.assertFalse(service.plist_path().exists())
        self.assertEqual(len(self.calls()), 1)

    def test_bootstrap_falls_back_to_load(self):
        self.launch.side_effect = [_cp(1), _cp(5, err="Bootstrap failed: 5"), _cp(0), _cp(1)]
        with mock.patch("sys.stdout"):
            self.assertEqual(service.install(), 0)
        self.assertEqual(self.calls()[2][:3], ["launchctl", "load", "-w"])

    def test_uninstall_boots_out_and_removes_plist(self):
        service.write_plist(service.plist_path(), service.build_plist())
        self.launch.return_value = _cp(0)
        with mock.patch("sys.stdout"):
            self.assertEqual(service.uninstall(), 0)
        self.assertFalse(service.plist_path().exists())
        self.assertEqual(self.calls()[0][:2], ["launchctl", "bootout"])

    def test_restart_kickstarts(self):
        self.launch.return_value = _cp(0)
        with mock.patch("sys.stdout"):
            self.assertEqual(service.restart(), 0)
        self.assertEqual(self.calls()[0][:3], ["launchctl", "kickstart", "-k"])

    def test_parse_print(self):
        text = "gui/501/local.orchestra-next.daemon = {\n\tstate = running\n\tpid = 917\n\tlast exit code = 0\n}\n"
        self.assertEqual(service.parse_print(text), {"state": "running", "pid": 917, "last_exit_code": 0})

    def test_non_darwin_exits_1(self):
        with mock.patch("orchestra.service.sys.platform", "linux"), mock.patch("sys.stderr"):
            self.assertEqual(service.main(mock.Mock(action="status")), 1)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
