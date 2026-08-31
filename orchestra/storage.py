"""Reviewable storage reporting and recoverable pruning."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestra import db, paths


def _size(path: Path) -> int:
    if path.is_file() and not path.is_symlink():
        return path.stat().st_size
    if path.is_dir() and not path.is_symlink():
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file() and not item.is_symlink())
    return 0


def report(con) -> dict:
    return {
        "database_bytes": _size(paths.db_path()),
        "run_bytes": _size(paths.runs_dir()),
        "artifact_bytes": _size(paths.artifacts_dir()),
        "worktree_bytes": _size(paths.worktrees_dir()),
        "runs": int(con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]),
        "pinned_runs": int(con.execute("SELECT COUNT(*) FROM evidence_pins").fetchone()[0]),
        "retention": "indefinite until an explicit prune plan is applied",
    }


def pin(con, run_id: int, *, actor: str, reason=None) -> dict:
    if not con.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
        raise LookupError(f"run {run_id} does not exist")
    with con:
        con.execute("INSERT INTO evidence_pins(run_id,reason,created_by,created_at) VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET reason=excluded.reason,created_by=excluded.created_by,created_at=excluded.created_at", (run_id, reason, actor, db.now()))
        db.record_control(con, actor=actor, action="evidence.pin", outcome="ok", target_type="run", target_id=run_id)
    return dict(con.execute("SELECT * FROM evidence_pins WHERE run_id=?", (run_id,)).fetchone())


def unpin(con, run_id: int, *, actor: str) -> bool:
    with con:
        removed = con.execute("DELETE FROM evidence_pins WHERE run_id=?", (run_id,)).rowcount == 1
        db.record_control(con, actor=actor, action="evidence.unpin", outcome="ok" if removed else "not_pinned", target_type="run", target_id=run_id)
    return removed


def create_plan(con, *, actor: str, older_than_days=30) -> dict:
    days = int(older_than_days)
    if days < 0:
        raise ValueError("older_than_days must be non-negative")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = con.execute("SELECT r.* FROM runs r LEFT JOIN evidence_pins p ON p.run_id=r.id WHERE r.status IN " + db.TERMINAL_SQL + " AND r.finished_at<=? AND p.run_id IS NULL ORDER BY r.id", (cutoff,)).fetchall()
    items = []
    for run in rows:
        session = paths.run_dir(run["id"]) / "dsh-session"
        if session.is_dir():
            items.append({"kind": "dsh_session", "run_id": run["id"], "path": str(session), "size_bytes": _size(session)})
        for artifact in con.execute("SELECT * FROM artifacts WHERE run_id=? AND pruned_at IS NULL", (run["id"],)):
            path = Path(artifact["stored_path"])
            if path.is_file():
                items.append({"kind": "artifact", "run_id": run["id"], "artifact_id": artifact["artifact_id"], "path": str(path), "size_bytes": _size(path)})
    plan_id = str(uuid.uuid4())
    criteria = {"older_than_days": days, "cutoff": cutoff}
    with con:
        con.execute("INSERT INTO prune_plans(plan_id,criteria_json,items_json,created_by,created_at) VALUES(?,?,?,?,?)", (plan_id, json.dumps(criteria), json.dumps(items), actor, db.now()))
        db.record_control(con, actor=actor, action="storage.plan", outcome="dry_run", target_type="prune_plan", target_id=plan_id, detail={"items": len(items)})
    return get_plan(con, plan_id)


def get_plan(con, plan_id: str) -> dict | None:
    row = con.execute("SELECT * FROM prune_plans WHERE plan_id=?", (plan_id,)).fetchone()
    if not row:
        return None
    value = dict(row)
    value["criteria"] = json.loads(value.pop("criteria_json"))
    value["items"] = json.loads(value.pop("items_json"))
    raw_result = value.pop("result_json")
    value["result"] = json.loads(raw_result) if raw_result else None
    return value


def apply_plan(con, plan_id: str, *, actor: str) -> dict:
    plan = get_plan(con, plan_id)
    if not plan or plan["applied_at"]:
        raise ValueError("prune plan is missing or already applied")
    trash = paths.owner_dir(paths.state_dir() / "trash" / plan_id)
    moved = []
    for index, item in enumerate(plan["items"]):
        source = Path(item["path"])
        allowed = paths.run_dir(item["run_id"]) if item["kind"] == "dsh_session" else paths.run_artifacts_dir(item["run_id"])
        try:
            source.resolve(strict=True).relative_to(allowed.resolve())
        except (OSError, ValueError):
            continue
        target = trash / f"{index}-{source.name}"
        os.replace(source, target)
        moved.append({**item, "recoverable_at": str(target)})
        if item["kind"] == "artifact":
            con.execute("UPDATE artifacts SET pruned_at=? WHERE artifact_id=?", (db.now(), item["artifact_id"]))
    result = {"moved": moved, "trash": str(trash)}
    with con:
        con.execute("UPDATE prune_plans SET applied_by=?,applied_at=?,result_json=? WHERE plan_id=?", (actor, db.now(), json.dumps(result), plan_id))
        db.record_control(con, actor=actor, action="storage.apply", outcome="ok", target_type="prune_plan", target_id=plan_id, detail={"moved": len(moved)})
    return get_plan(con, plan_id)
