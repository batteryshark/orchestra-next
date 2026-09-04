"""Self-update from the Git remote: show the running commit, check origin, fast-forward, restart.

Breaking changes are accepted by design: the operator asked for "newest version" semantics.
The only refusals are a dirty checkout and a non-fast-forward history, because both would
lose work that exists only on this machine.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import orchestra

REMOTE = "origin"
BRANCH = "main"
FETCH_TIMEOUT = 60


class UpdateError(RuntimeError):
    pass


def repo_root() -> Path:
    return Path(orchestra.__file__).resolve().parent.parent


def _git(*args: str, timeout: int = 30) -> str:
    res = subprocess.run(["git", "-C", str(repo_root()), *args], capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise UpdateError((res.stderr or res.stdout).strip() or f"git {args[0]} failed")
    return res.stdout.strip()


def _commit(ref: str) -> dict:
    sha, date, subject = _git("log", "-1", "--format=%H%n%cI%n%s", ref).split("\n", 2)
    return {"sha": sha, "short": sha[:9], "date": date, "subject": subject}


def status() -> dict:
    """The running checkout: commit, branch, dirtiness, remote. Never touches the network."""
    try:
        head = _commit("HEAD")
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
        try:
            remote = _git("remote", "get-url", REMOTE)
        except UpdateError:
            remote = None
    except (UpdateError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "reason": str(exc), "root": str(repo_root())}
    return {"available": True, "root": str(repo_root()), "branch": branch, "dirty": dirty, "remote": remote, **head}


def check() -> dict:
    """Fetch origin and compare. Returns the commits this checkout is behind (newest first)."""
    current = status()
    if not current["available"]:
        raise UpdateError(current["reason"])
    if not current["remote"]:
        raise UpdateError(f"no git remote named {REMOTE!r}")
    _git("fetch", "--quiet", REMOTE, BRANCH, timeout=FETCH_TIMEOUT)
    target = f"{REMOTE}/{BRANCH}"
    behind = int(_git("rev-list", "--count", f"HEAD..{target}"))
    ahead = int(_git("rev-list", "--count", f"{target}..HEAD"))
    raw = _git("log", "--format=%H%x1f%cI%x1f%s", f"HEAD..{target}") if behind else ""
    commits = [dict(zip(("sha", "date", "subject"), line.split("\x1f", 2))) for line in raw.splitlines()]
    for item in commits:
        item["short"] = item["sha"][:9]
    return {**current, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "target": target,
            "remote_commit": _commit(target), "behind": behind, "ahead": ahead, "commits": commits,
            "can_update": behind > 0 and ahead == 0 and not current["dirty"]}


def apply() -> dict:
    """Fast-forward to origin/main. Refuses a dirty tree or diverged history."""
    result = check()
    if result["dirty"]:
        raise UpdateError("the checkout has local changes; commit or stash them first")
    if result["ahead"]:
        raise UpdateError(f"this checkout is {result['ahead']} commit(s) ahead of {result['target']}; push or reset first")
    if not result["behind"]:
        return {**result, "updated": False}
    _git("merge", "--ff-only", result["target"], timeout=FETCH_TIMEOUT)
    return {**check(), "updated": True, "previous": result["sha"]}


def schedule_restart(delay: float = 1.0) -> str:
    """Restart the daemon so the new code loads. Under launchd: kickstart. Foreground: exit 75 and let the operator restart."""
    from orchestra import service

    def go():
        time.sleep(delay)
        if service.plist_path().exists():
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{__import__('os').getuid()}/{service.LABEL}"], capture_output=True)
        else:
            sys.stdout.flush()
            import os
            os._exit(75)

    threading.Thread(target=go, daemon=True, name="orchestra-restart").start()
    return "service" if service.plist_path().exists() else "exit"
