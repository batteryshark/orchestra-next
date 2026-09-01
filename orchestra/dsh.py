"""The one supported DSH deployment and launch path."""
from __future__ import annotations

import filecmp
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from orchestra import paths

PINNED_VERSION = "0.1.2-alpha.3"
PROFILE_NAME = "orchestra-next"
PROFILE_FILES = ("package.json", "cordis.patch.yml", "ralph.patch.yml")


class DshError(RuntimeError):
    pass


def executable() -> str:
    return os.environ.get("ORCHESTRA_NEXT_DSH", "dsh")


def installed_version() -> str:
    try:
        result = subprocess.run([executable(), "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DshError(f"DSH {PINNED_VERSION} is required but dsh is unavailable: {exc}") from exc
    text = (result.stdout + "\n" + result.stderr).strip()
    match = re.search(r"\b(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)\b", text)
    if result.returncode != 0 or match is None:
        raise DshError(f"cannot determine DSH version: {text[:300] or 'no output'}")
    return match.group(1)


def require_version() -> str:
    version = installed_version()
    if version != PINNED_VERSION:
        raise DshError(f"unsupported DSH {version}; Orchestra-next requires exactly {PINNED_VERSION}")
    return version


def profile_dir() -> Path:
    return paths.dsh_home() / "profiles" / PROFILE_NAME


def setup_profile() -> tuple[Path, bool]:
    """Install only the versioned profile. Credentials and settings are untouched."""
    destination = profile_dir()
    source = paths.dsh_profile_source()
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    changed = False
    for name in PROFILE_FILES:
        incoming, target = source / name, destination / name
        if not target.exists() or not filecmp.cmp(incoming, target, shallow=False):
            temporary = target.with_suffix(target.suffix + ".tmp")
            shutil.copyfile(incoming, temporary)
            os.replace(temporary, target)
            changed = True
    return destination, changed


def check_profile(*, require_binary: bool = True) -> dict:
    version = require_version() if require_binary else None
    destination = profile_dir()
    missing = [name for name in PROFILE_FILES if not (destination / name).is_file()]
    changed = [name for name in PROFILE_FILES if name not in missing and not filecmp.cmp(paths.dsh_profile_source() / name, destination / name, shallow=False)]
    if missing or changed:
        detail = ", ".join([*(f"missing {name}" for name in missing), *(f"changed {name}" for name in changed)])
        raise DshError(f"DSH profile {PROFILE_NAME!r} is not canonical: {detail}; run `orchestra-next dsh setup`")
    return {
        "version": version,
        "profile": PROFILE_NAME,
        "path": str(destination),
        "canonical": True,
        "capabilities": {"native_web_search": web_search_capability()},
    }


def web_search_capability() -> dict:
    """Detect DSH's search credential without returning or changing it."""
    if os.environ.get("DEEPSEEK_API_KEY", "").strip():
        return {"available": True, "source": "environment"}
    credential_file = paths.dsh_home() / ".credentials.yaml"
    try:
        document = credential_file.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        document = ""
    match = re.search(r"(?m)^\s{2}DEEPSEEK_API_KEY:\s*(.*?)\s*$", document)
    empty = {"", "''", '\"\"', "null", "Null", "NULL", "~"}
    if match and match.group(1) not in empty:
        return {"available": True, "source": "dsh-credentials"}
    return {
        "available": False,
        "reason": "DEEPSEEK_API_KEY was not detected; DSH native web search is unavailable",
    }


def write_auth_file(run_id: int, token: str) -> Path:
    target = paths.run_auth_path(run_id)
    descriptor, temporary = tempfile.mkstemp(prefix=".worker-auth-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"token": token}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return target


def launch(run: dict, token: str, provider_env: dict[str, str] | None = None) -> tuple[list[str], dict[str, str]]:
    """Return the only DSH argv/env. The bearer exists only in a 0600 file."""
    require_version()
    check_profile(require_binary=False)
    run_id = int(run["id"])
    auth_file = write_auth_file(run_id, token)
    env = dict(os.environ)
    env.pop("ORCHESTRA_NEXT_TOKEN", None)
    env.update({
        "ORCHESTRA_NEXT_DSH_SESSION_ROOT": str(paths.run_session_dir(run_id)),
        "ORCHESTRA_NEXT_RUN_AUTH_FILE": str(auth_file),
        "ORCHESTRA_NEXT_RUN_ID": str(run_id),
        "ORCHESTRA_NEXT_URL": env.get("ORCHESTRA_NEXT_URL", "http://127.0.0.1:8766"),
        "DSH_TELEMETRY_DISABLED": "1",
        "DSH_PERMISSION_MODE": run["permission_mode"],
    })
    env.update(provider_env or {})
    if run["strategy"] == "ralph":
        remaining = max(1, int(run["max_rounds"]) - int(run.get("rounds_started") or 0))
        env["ORCHESTRA_NEXT_RALPH_ROUNDS"] = str(remaining)
    # Reapply the canonical policy after DSH's home-level patch. Credentials
    # and provider settings remain DSH-owned; safety/tool rows cannot be
    # re-enabled accidentally by a broad home customization.
    argv = [executable(), "--profile", PROFILE_NAME, "--patch",
            str(profile_dir() / "cordis.patch.yml")]
    if run["strategy"] == "ralph":
        argv.extend(["--patch", str(profile_dir() / "ralph.patch.yml")])
    return argv, env


def catalog(cwd: str) -> dict[tuple[str, str], set[str]]:
    """Read DSH's live ACP model/effort options without making a model call."""
    from orchestra.acp import Peer
    require_version()
    check_profile(require_binary=False)
    with tempfile.TemporaryDirectory(prefix="orchestra-next-dsh-check-") as root:
        env = dict(os.environ)
        env.update({
            "ORCHESTRA_NEXT_DSH_SESSION_ROOT": root,
            "ORCHESTRA_NEXT_CLAUDE_PROXY_URL": "http://127.0.0.1:1/v1",
            "ORCHESTRA_NEXT_CLAUDE_PROXY_TOKEN": "catalog-only",
            "ORCHESTRA_NEXT_RUN_ID": "catalog",
            "DSH_TELEMETRY_DISABLED": "1",
            "DSH_PERMISSION_MODE": "read-only",
        })
        peer = Peer([executable(), "--profile", PROFILE_NAME, "--patch",
                     str(profile_dir() / "cordis.patch.yml")], cwd=cwd, env=env,
                    log_path=Path(root) / "acp.jsonl")
        try:
            peer.start()
            peer.initialize()
            created = peer.new_session(cwd)
            options = created.get("configOptions") or []
            model_option = next((item for item in options if item.get("id") == "model"), None)
            if not model_option or model_option.get("type") != "select":
                raise DshError("DSH ACP did not advertise a model selector")
            choices = []
            for item in model_option.get("options") or []:
                choices.extend(item.get("options") or [] if "group" in item else [item])
            result: dict[tuple[str, str], set[str]] = {}
            session_id = created["sessionId"]
            for choice in choices:
                try:
                    provider, model = json.loads(choice["value"])
                except (KeyError, TypeError, ValueError):
                    continue
                changed = peer.call("session/set_config_option", {
                    "sessionId": session_id, "configId": "model", "value": choice["value"]})
                effort_option = next((item for item in (changed.get("configOptions") or [])
                                      if item.get("id") == "reasoning_effort"), None)
                efforts: set[str] = set()
                if effort_option and effort_option.get("type") == "select":
                    for item in effort_option.get("options") or []:
                        nested = item.get("options") or [] if "group" in item else [item]
                        efforts.update(str(value["value"]) for value in nested if value.get("value"))
                result[(str(provider), str(model))] = efforts
            peer.call("session/close", {"sessionId": session_id})
            if not result:
                raise DshError("DSH ACP advertised no usable models")
            return result
        finally:
            peer.close()


_CATALOG_LOCK = threading.Lock()
_CATALOG: tuple[float, str, dict] | None = None  # (monotonic, checked_at_iso, result)
CATALOG_TTL = 300.0


def cached_catalog(cwd: str, *, refresh: bool = False) -> tuple[dict, str]:
    """Serve the advertised route catalog from a short-lived cache.

    A catalog probe spawns a DSH process, so mutation validators keep calling
    ``catalog`` directly; this cache exists for read endpoints and preflight.
    """
    # ponytail: one global entry ignores cwd; key by cwd if per-repo catalogs ever exist
    global _CATALOG
    with _CATALOG_LOCK:
        if not refresh and _CATALOG is not None and time.monotonic() - _CATALOG[0] < CATALOG_TTL:
            return _CATALOG[2], _CATALOG[1]
        result = catalog(cwd)
        _CATALOG = (time.monotonic(), datetime.now(timezone.utc).isoformat(), result)
        return _CATALOG[2], _CATALOG[1]


def catalog_cache_age() -> float | None:
    with _CATALOG_LOCK:
        return None if _CATALOG is None else time.monotonic() - _CATALOG[0]
