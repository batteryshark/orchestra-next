"""Offline backup and restore of the state directory. Nothing is ever deleted."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from orchestra import db, paths

DAEMON_PATTERN = r"(orchestra-next|-m orchestra) daemon"


def daemon_running() -> bool:
    return subprocess.run(["pgrep", "-f", DAEMON_PATTERN], capture_output=True).returncode == 0


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            value.update(chunk)
    return value.hexdigest()


def _files(root: Path):
    return sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink() and p.name != "manifest.json")


def backup(destination=None) -> dict:
    if daemon_running():
        raise ValueError("stop the daemon before backup")
    root = Path(destination).expanduser() if destination else paths.backups_dir()
    target = root / f"orchestra-next-{_stamp()}"
    target.mkdir(mode=0o700, parents=True)
    source = db.connect()
    try:
        with sqlite3.connect(str(target / paths.db_path().name)) as copy:
            source.backup(copy)
    finally:
        source.close()
    if paths.bootstrap_path().is_file():
        shutil.copy2(paths.bootstrap_path(), target / paths.bootstrap_path().name)
    shutil.copytree(paths.artifacts_dir(), target / "artifacts", symlinks=True)
    manifest = {
        "schema_version": db.SCHEMA_VERSION,
        "created_at": db.now(),
        "files": {str(p.relative_to(target)): {"sha256": _digest(p), "size": p.stat().st_size} for p in _files(target)},
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"manifest": str(target / "manifest.json")}


def restore(source, *, apply=False) -> dict:
    source = Path(source).expanduser()
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if manifest["schema_version"] != db.SCHEMA_VERSION:
        raise ValueError(f"backup schema {manifest['schema_version']} does not match {db.SCHEMA_VERSION}")
    for rel, info in manifest["files"].items():
        path = source / rel
        if not path.is_file() or _digest(path) != info["sha256"]:
            raise ValueError(f"digest mismatch: {rel}")
    replaced = [p for p in (paths.db_path(), paths.bootstrap_path(), paths.artifacts_dir()) if p.exists()]
    plan = {"apply": apply, "restore": sorted(manifest["files"]), "move_aside": [str(p) for p in replaced]}
    if not apply:
        return plan
    if daemon_running():
        raise ValueError("stop the daemon before restore")
    trash = paths.owner_dir(paths.state_dir() / "trash" / f"restore-{_stamp()}")
    for path in replaced:
        shutil.move(str(path), str(trash / path.name))
    for extra in paths.state_dir().glob(paths.db_path().name + "-*"):  # WAL and SHM belong to the old database
        shutil.move(str(extra), str(trash / extra.name))
    for rel in manifest["files"]:
        dest = paths.state_dir() / rel
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(source / rel, dest)
    plan["trash"] = str(trash)
    return plan
