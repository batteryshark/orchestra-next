"""Per-run Claude subscription sidecar behind an ordinary DSH provider."""
from __future__ import annotations

import filecmp
import json
import os
import secrets
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from orchestra import paths

PROVIDER = "claude-subscription"
BRIDGE_VERSION = "0.14.0"
BUN_VERSION = "1.4.0"
SOURCE_FILES = ("package.json", "package-lock.json", "runner.ts")
START_TIMEOUT_SECONDS = 30


class ClaudeError(RuntimeError):
    pass


def required(provider: str | None) -> bool:
    return provider == PROVIDER


def _copy_source() -> tuple[Path, bool]:
    source, destination = paths.claude_sidecar_source(), paths.claude_sidecar_dir()
    changed = False
    for name in SOURCE_FILES:
        incoming, target = source / name, destination / name
        if not target.exists() or not filecmp.cmp(incoming, target, shallow=False):
            descriptor, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=destination)
            os.close(descriptor)
            try:
                shutil.copyfile(incoming, temporary)
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
            changed = True
    return destination, changed


def _installed(directory: Path) -> bool:
    try:
        bridge = json.loads((directory / "node_modules/@openchamber/opencode-claude/package.json").read_text())
        bun = json.loads((directory / "node_modules/bun/package.json").read_text())
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (bridge.get("version"), bun.get("version")) == (BRIDGE_VERSION, BUN_VERSION) and (
        directory / "node_modules/.bin/bun").is_file()


def setup() -> tuple[Path, bool]:
    """Install the pinned sidecar without reading or changing Claude credentials."""
    directory, changed = _copy_source()
    if changed or not _installed(directory):
        npm = os.environ.get("ORCHESTRA_NEXT_NPM", "npm")
        try:
            result = subprocess.run(
                [npm, "ci", "--no-audit", "--no-fund"], cwd=directory,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClaudeError(f"cannot install Claude sidecar dependencies: {exc}") from exc
        if result.returncode != 0 or not _installed(directory):
            detail = (result.stderr or result.stdout).strip()[-2000:]
            raise ClaudeError(f"Claude sidecar dependency install failed: {detail or 'incomplete install'}")
        changed = True
    return directory, changed


def _auth_status() -> dict:
    executable = os.environ.get("ORCHESTRA_NEXT_CLAUDE", "claude")
    env = dict(os.environ)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop(name, None)
    try:
        result = subprocess.run(
            [executable, "auth", "status", "--json"], env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaudeError(f"official Claude CLI is unavailable: {exc}") from exc
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeError("official Claude CLI returned malformed auth status") from exc
    if result.returncode != 0 or value.get("loggedIn") is not True:
        raise ClaudeError("Claude subscription is not logged in; run `claude auth login`")
    return {"logged_in": True, "method": value.get("authMethod") or "unknown"}


def check(*, require_auth: bool = True) -> dict:
    directory = paths.claude_sidecar_dir()
    missing = [name for name in SOURCE_FILES if not (directory / name).is_file()]
    changed = [name for name in SOURCE_FILES if name not in missing and not filecmp.cmp(
        paths.claude_sidecar_source() / name, directory / name, shallow=False)]
    if missing or changed or not _installed(directory):
        detail = ", ".join([*(f"missing {name}" for name in missing), *(f"changed {name}" for name in changed)])
        raise ClaudeError(f"Claude sidecar is not canonical{': ' + detail if detail else ''}; run `orchestra-next claude setup`")
    return {
        "path": str(directory), "provider": PROVIDER, "bridge_version": BRIDGE_VERSION,
        "bun_version": BUN_VERSION, "canonical": True,
        **({"auth": _auth_status()} if require_auth else {}),
    }


def _write_auth_file(run_id: int, token: str) -> Path:
    target = paths.run_claude_auth_path(run_id)
    descriptor, temporary = tempfile.mkstemp(prefix=".claude-auth-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"token": token}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


@dataclass
class Sidecar:
    process: subprocess.Popen
    url: str
    token: str
    auth_file: Path
    log_handle: object

    @property
    def provider_env(self) -> dict[str, str]:
        return {
            "ORCHESTRA_NEXT_CLAUDE_PROXY_URL": self.url,
            "ORCHESTRA_NEXT_CLAUDE_PROXY_TOKEN": self.token,
        }

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(self.process.pid, signal.SIGTERM)
                else:
                    self.process.terminate()
            except OSError:
                pass
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    if os.name == "posix":
                        os.killpg(self.process.pid, signal.SIGKILL)
                    else:
                        self.process.kill()
                except OSError:
                    pass
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        if self.process.stdout:
            self.process.stdout.close()
        self.auth_file.unlink(missing_ok=True)
        self.log_handle.close()


def start(run_id: int, cwd: Path) -> Sidecar:
    info = check()
    directory = Path(info["path"])
    token = secrets.token_urlsafe(32)
    auth_file = _write_auth_file(run_id, token)
    log_path = paths.run_dir(run_id) / "claude-sidecar.log"
    log_handle = log_path.open("a", encoding="utf-8")
    os.chmod(log_path, 0o600)
    env = dict(os.environ)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop(name, None)
    env["XDG_DATA_HOME"] = str(paths.run_claude_data_dir(run_id))
    command = [str(directory / "node_modules/.bin/bun"), str(directory / "runner.ts"),
               "--auth-file", str(auth_file), "--port", "0"]
    try:
        process = subprocess.Popen(
            command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=log_handle, text=True, encoding="utf-8", errors="replace",
            start_new_session=True,
        )
    except OSError as exc:
        auth_file.unlink(missing_ok=True)
        log_handle.close()
        raise ClaudeError(f"cannot start Claude sidecar: {exc}") from exc
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    ready = None
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline and process.poll() is None:
            if selector.select(timeout=min(0.25, deadline - time.monotonic())):
                line = process.stdout.readline(4097)
                if not line:
                    break
                if len(line) > 4096:
                    raise ClaudeError("Claude sidecar emitted an oversized startup frame")
                try:
                    ready = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ClaudeError("Claude sidecar emitted malformed startup JSON") from exc
                break
    except Exception:
        Sidecar(process, "", token, auth_file, log_handle).close()
        raise
    finally:
        selector.close()
    if not isinstance(ready, dict) or not isinstance(ready.get("url"), str):
        Sidecar(process, "", token, auth_file, log_handle).close()
        raise ClaudeError("Claude sidecar exited or timed out before becoming ready")
    parsed = urlsplit(ready["url"])
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path != "/v1":
        Sidecar(process, "", token, auth_file, log_handle).close()
        raise ClaudeError("Claude sidecar advertised a non-loopback endpoint")
    sidecar = Sidecar(process, ready["url"], token, auth_file, log_handle)
    request = urllib.request.Request(ready["url"] + "/health", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status != 200:
                raise ClaudeError(f"Claude sidecar health check returned {response.status}")
    except Exception as exc:
        sidecar.close()
        raise ClaudeError(f"Claude sidecar failed its authenticated health check: {exc}") from exc
    return sidecar
