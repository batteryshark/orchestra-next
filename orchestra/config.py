"""Small non-secret daemon bootstrap."""
from __future__ import annotations

import ipaddress
import json
import os
import tempfile
from pathlib import Path

from orchestra import paths

DEFAULTS = {
    "bind": "127.0.0.1",
    "port": 8766,
    "callback_command": [],
    "trust_tailnet": False,
    "trust_loopback": False,
    "trusted_cidrs": [],
    "allowed_hosts": [],
}


class ConfigError(ValueError):
    pass


def _validate(value) -> dict:
    if not isinstance(value, dict):
        raise ConfigError("bootstrap must be a JSON object")
    unknown = set(value) - set(DEFAULTS)
    if unknown:
        raise ConfigError("unknown bootstrap fields: " + ", ".join(sorted(unknown)))
    result = {**DEFAULTS, **value}
    if not isinstance(result["bind"], str) or not result["bind"].strip():
        raise ConfigError("bind must be a non-empty string")
    if isinstance(result["port"], bool) or not isinstance(result["port"], int) or not 0 <= result["port"] <= 65535:
        raise ConfigError("port must be an integer from 0 to 65535")
    command = result["callback_command"]
    if not isinstance(command, list) or any(not isinstance(item, str) or not item for item in command):
        raise ConfigError("callback_command must be an argv array")
    for flag in ("trust_tailnet", "trust_loopback"):
        if not isinstance(result[flag], bool):
            raise ConfigError(f"{flag} must be a boolean")
    cidrs = result["trusted_cidrs"]
    if not isinstance(cidrs, list):
        raise ConfigError("trusted_cidrs must be a list of CIDR strings")
    for item in cidrs:
        try:
            ipaddress.ip_network(item)
        except ValueError as exc:
            raise ConfigError(f"trusted_cidrs entry {item!r} is not a valid network") from exc
    hosts = result["allowed_hosts"]
    if not isinstance(hosts, list) or any(not isinstance(item, str) or not item.strip() for item in hosts):
        raise ConfigError("allowed_hosts must be a list of host names")
    return result


def read(path: Path | None = None) -> dict:
    location = path or paths.bootstrap_path()
    if not location.is_file():
        return dict(DEFAULTS)
    try:
        return _validate(json.loads(location.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read bootstrap {location}: {exc}") from exc


def write(value: dict, path: Path | None = None) -> Path:
    location = path or paths.bootstrap_path()
    checked = _validate(value)
    location.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".bootstrap-", dir=location.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(checked, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, location)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return location


def ensure() -> tuple[Path, bool]:
    location = paths.bootstrap_path()
    if location.exists():
        read(location)
        return location, False
    return write(DEFAULTS), True


def api_url() -> str:
    explicit = os.environ.get("ORCHESTRA_NEXT_URL")
    if explicit:
        return explicit.rstrip("/")
    value = read()
    host = "127.0.0.1" if value["bind"] in ("0.0.0.0", "::") else value["bind"]
    return f"http://{host}:{value['port']}"


def callback_command() -> list[str]:
    return list(read()["callback_command"])
