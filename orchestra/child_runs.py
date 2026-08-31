"""Bounded child runs owned by the durable fleet, not DSH subagents."""
from __future__ import annotations

import json

from orchestra import db, profiles, runs
from orchestra.contracts import RunRequest, child_tier_allowed


class DelegationError(ValueError):
    pass


def create(con, parent_run_id: int, value: dict, *, actor="run"):
    parent = runs.find(con, parent_run_id)
    if parent is None or parent["status"] in db.RUN_TERMINAL:
        raise DelegationError("parent run is not active")
    existing = int(con.execute("SELECT COUNT(*) FROM runs WHERE parent_run_id=?", (parent_run_id,)).fetchone()[0])
    if parent["max_children"] is not None and existing >= parent["max_children"]:
        raise DelegationError("parent run reached max_children")
    profile = profiles.find(con, value.get("profile", ""))
    parent_profile = json.loads(parent["profile_snapshot"])
    if profile is None or not child_tier_allowed(int(parent_profile["tier"]), int(profile["tier"]), parent["max_child_tier"]):
        raise DelegationError("child profile exceeds the delegated tier")
    group = con.execute("SELECT slug FROM run_groups WHERE group_id=?", (parent["group_id"],)).fetchone()
    request_value = {
        "request_id": value.get("request_id"),
        "profile": value.get("profile"),
        "objective": value.get("objective"),
        "group": group["slug"],
        "strategy": value.get("strategy", "goal"),
        "permission_mode": value.get("permission_mode", parent["permission_mode"]),
        "title": value.get("title"),
        "cwd": parent["cwd"],
        "ref": parent["branch"],
        "requested_by": actor,
        "limits": value.get("limits", {}),
        "verify": value.get("verify"),
        "max_children": value.get("max_children"),
        "max_child_tier": value.get("max_child_tier"),
    }
    request = RunRequest.from_mapping(request_value)
    child, _ = runs.submit(con, request, parent_run_id=parent_run_id)
    with con:
        con.execute("UPDATE runs SET status='waiting',waiting_kind='children',waiting_detail=?,updated_at=? WHERE id=?", (str(child["id"]), db.now(), parent_run_id))
        db.append_event(con, parent_run_id, "children.waiting", {"child_run_id": child["id"]})
    return child


def for_parent(con, parent_run_id: int) -> list[dict]:
    return [runs.payload(row) for row in con.execute("SELECT * FROM runs WHERE parent_run_id=? ORDER BY id", (parent_run_id,))]


def settle(con) -> list[int]:
    resumed = []
    parents = con.execute("SELECT * FROM runs WHERE status='waiting' AND waiting_kind='children'").fetchall()
    for parent in parents:
        children = con.execute("SELECT status,summary,error FROM runs WHERE parent_run_id=?", (parent["id"],)).fetchall()
        if not children or any(row["status"] not in db.RUN_TERMINAL for row in children):
            continue
        summary = "\n".join(f"child {index + 1}: {row['status']} — {row['summary'] or row['error'] or 'no summary'}" for index, row in enumerate(children))
        with con:
            con.execute("UPDATE runs SET status='queued',waiting_kind=NULL,waiting_detail=?,updated_at=? WHERE id=?", (summary, db.now(), parent["id"]))
            db.append_event(con, parent["id"], "children.settled", {"summary": summary})
        resumed.append(int(parent["id"]))
    return resumed
