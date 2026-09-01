"""Private filesystem layout for the experimental Orchestra-next instance."""
from __future__ import annotations

import os
import re
from pathlib import Path


def home() -> Path:
    raw = os.environ.get("ORCHESTRA_NEXT_HOME", "~/.orchestra-next")
    if not raw:
        raise SystemExit("orchestra-next: ORCHESTRA_NEXT_HOME must not be empty")
    candidate = Path(raw).expanduser()
    resolved = candidate.resolve()
    if resolved in (Path.cwd().resolve(), Path(resolved.anchor)):
        raise SystemExit("orchestra-next: state must use a dedicated directory")
    return candidate


def owner_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        path.chmod(0o700)
    return path


def state_dir() -> Path:
    return owner_dir(home())


def db_path() -> Path:
    return state_dir() / "orchestra-next.db"


def bootstrap_path() -> Path:
    return state_dir() / "bootstrap.json"


def dsh_profile_source() -> Path:
    return Path(__file__).with_name("dsh-profile")


def claude_sidecar_source() -> Path:
    return Path(__file__).with_name("claude-sidecar")


def claude_sidecar_dir() -> Path:
    return owner_dir(state_dir() / "claude-sidecar")


def dsh_home() -> Path:
    return Path(os.environ.get("DSH_HOME", "~/.dsh")).expanduser()


def _sub(name: str) -> Path:
    return owner_dir(state_dir() / name)


def runs_dir() -> Path:
    return _sub("runs")


def run_dir(run_id: int) -> Path:
    return owner_dir(runs_dir() / str(int(run_id)))


def run_session_dir(run_id: int) -> Path:
    return owner_dir(run_dir(run_id) / "dsh-session")


def run_auth_path(run_id: int) -> Path:
    return run_dir(run_id) / "worker-auth"


def run_claude_auth_path(run_id: int) -> Path:
    return run_dir(run_id) / "claude-proxy-auth"


def run_claude_data_dir(run_id: int) -> Path:
    return owner_dir(run_dir(run_id) / "claude-sidecar-data")


def logs_dir() -> Path:
    return _sub("logs")


def artifacts_dir() -> Path:
    return _sub("artifacts")


def run_artifacts_dir(run_id: int) -> Path:
    return owner_dir(artifacts_dir() / str(int(run_id)))


def worktrees_dir(group_slug: str | None = None) -> Path:
    root = _sub("worktrees")
    return owner_dir(root / slugify(group_slug)) if group_slug else root


def backups_dir() -> Path:
    return _sub("backups")


def slugify(raw: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", raw or "").strip("-") or "item"


def kebab(raw: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (raw or "").lower()).strip("-") or "item"
