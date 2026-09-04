"""macOS LaunchAgent for `orchestra-next daemon`.

A per-user LaunchAgent, never a system daemon: the agent CLIs need the login
keychain and the user's checkouts. `install` writes the plist and loads it;
starting the process is a separate explicit `--start`.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path

import orchestra
from orchestra import paths

LABEL = "local.orchestra-next.daemon"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def program() -> list[str]:
    return [sys.executable, "-m", "orchestra", "daemon"]


def repo_root() -> Path:
    return Path(orchestra.__file__).resolve().parent.parent


def build_plist() -> dict:
    log = str(paths.logs_dir() / "daemon.log")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")}
    # launchd gives the job a bare environment; forward the home override only when set.
    if os.environ.get("ORCHESTRA_NEXT_HOME"):
        env["ORCHESTRA_NEXT_HOME"] = os.environ["ORCHESTRA_NEXT_HOME"]
    return {
        "Label": LABEL,
        "ProgramArguments": program(),
        "WorkingDirectory": str(repo_root()),
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": env,
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "ProcessType": "Background",
    }


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _target() -> str:
    return f"{_domain()}/{LABEL}"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _fail(what: str, res: subprocess.CompletedProcess) -> int:
    print(f"orchestra-next service: {what} failed: {(res.stderr or res.stdout).strip()}", file=sys.stderr)
    return 1


def foreground_daemon_pids() -> list[int]:
    res = subprocess.run(["pgrep", "-f", "-- -m orchestra daemon"], capture_output=True, text=True)
    return [int(p) for p in res.stdout.split() if p.isdigit() and int(p) != os.getpid()]


def write_plist(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".plist.tmp")
    with open(tmp, "wb") as fh:
        plistlib.dump(data, fh)
    os.replace(tmp, path)


def install(start: bool = False) -> int:
    pids = foreground_daemon_pids()
    if pids:
        print(f"orchestra-next service: a foreground daemon is running (pid {', '.join(map(str, pids))}). "
              f"Stop it first: kill {' '.join(map(str, pids))}", file=sys.stderr)
        return 1
    path = plist_path()
    write_plist(path, build_plist())
    res = _launchctl("bootstrap", _domain(), str(path))
    if res.returncode != 0 and "already" not in (res.stderr + res.stdout).lower():
        res = _launchctl("load", "-w", str(path))
        if res.returncode != 0:
            return _fail("launchctl bootstrap/load", res)
    if start:
        res = _launchctl("kickstart", "-k", _target())
        if res.returncode != 0:
            return _fail("launchctl kickstart", res)
    return status()


def uninstall() -> int:
    path = plist_path()
    res = _launchctl("bootout", _target())
    if res.returncode != 0:
        _launchctl("unload", "-w", str(path))
    if path.exists():
        path.unlink()
    print(json.dumps({"label": LABEL, "plist": str(path), "installed": False}))
    return 0


def restart() -> int:
    res = _launchctl("kickstart", "-k", _target())
    if res.returncode != 0:
        return _fail("launchctl kickstart", res)
    return status()


def parse_print(text: str) -> dict:
    fields = {}
    for key in ("state", "pid", "last exit code"):
        m = re.search(rf"^\s*{re.escape(key)} = (.+?)\s*$", text, re.M)
        if m:
            fields[key.replace(" ", "_")] = int(m.group(1)) if m.group(1).isdigit() else m.group(1)
    return fields


def status() -> int:
    path = plist_path()
    res = _launchctl("print", _target())
    info = {"label": LABEL, "plist": str(path), "installed": path.exists(), "loaded": res.returncode == 0,
            "log": str(paths.logs_dir() / "daemon.log")}
    if res.returncode == 0:
        info.update(parse_print(res.stdout))
    print(json.dumps(info, indent=2))
    return 0


def main(args) -> int:
    if sys.platform != "darwin":
        print("orchestra-next service: launchd only; run `python -m orchestra daemon` under your own supervisor", file=sys.stderr)
        return 1
    if args.action == "install":
        return install(args.start)
    return {"uninstall": uninstall, "status": status, "restart": restart}[args.action]()
