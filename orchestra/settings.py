"""Durable fleet settings: one JSON document in the meta table, revision-guarded."""
from __future__ import annotations

import json

from orchestra import db

DEFAULTS = {
    "max_active_runs": 4,
    "max_children_per_run": 100,  # the RunRequest max_children ceiling
    "max_child_depth": 3,
    "paused": False,
}
_KEY, _REV = "fleet_settings", "fleet_settings_revision"


def validate(changes: dict) -> dict:
    unknown = set(changes) - set(DEFAULTS)
    if unknown:
        raise ValueError("unknown settings: " + ", ".join(sorted(unknown)))
    for key, value in changes.items():
        if isinstance(DEFAULTS[key], bool):
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be a boolean")
        elif isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{key} must be an integer >= 1")
    return dict(changes)


def current(con) -> dict:
    stored = json.loads(db.meta_get(con, _KEY) or "{}")
    return {**DEFAULTS, **stored}


def revision(con) -> int:
    return int(db.meta_get(con, _REV) or 1)


def get(con, key: str):
    return current(con)[key]


def update(con, changes: dict, *, expected_revision: int, actor: str, action="settings.update", detail=None) -> dict:
    checked = validate(changes)
    with con:
        if revision(con) != expected_revision:
            raise RuntimeError("settings changed since they were read")
        merged = {**current(con), **checked}
        db.meta_set(con, _KEY, json.dumps(merged))
        db.meta_set(con, _REV, str(expected_revision + 1))
        db.record_control(con, actor=actor, action=action, outcome="ok", target_type="settings", detail=detail or checked)
    return merged


def set_paused(con, paused: bool, *, actor: str, note=None) -> dict:
    return update(con, {"paused": paused}, expected_revision=revision(con), actor=actor,
                  action="scheduler.pause" if paused else "scheduler.resume", detail={"paused": paused, "note": note})


def payload(con) -> dict:
    values = current(con)
    counts = {status: int(con.execute("SELECT COUNT(*) FROM runs WHERE status=?", (status,)).fetchone()[0]) for status in ("queued", "starting", "running")}
    running = counts["starting"] + counts["running"]
    return {"settings": values, "revision": revision(con),
            "scheduler": {"paused": values["paused"], "queued": counts["queued"], "running": running,
                          "capacity": max(0, values["max_active_runs"] - running)}}
