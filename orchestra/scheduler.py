"""Dependency and concurrency admission for durable runs."""
from __future__ import annotations

from orchestra import db, settings


def _dependency_state(con, run_id: int) -> str:
    rows = con.execute("SELECT d.condition,r.status FROM run_dependencies d JOIN runs r ON r.id=d.depends_on_run_id WHERE d.run_id=?", (run_id,)).fetchall()
    for row in rows:
        if row["condition"] == "success" and row["status"] in db.RUN_TERMINAL and row["status"] != "completed":
            return "impossible"
        if row["condition"] == "success" and row["status"] != "completed":
            return "blocked"
        if row["condition"] == "terminal" and row["status"] not in db.RUN_TERMINAL:
            return "blocked"
    return "ready"


def admit(con, *, limit=None) -> dict:
    if settings.get(con, "paused"):
        return {"admitted": [], "skipped": []}
    limit = limit or settings.get(con, "max_active_runs")
    active = int(con.execute("SELECT COUNT(*) FROM runs WHERE status IN ('starting','running')").fetchone()[0])
    admitted, skipped = [], []
    if active >= limit:
        return {"admitted": admitted, "skipped": skipped}
    profile_active = {row[0]: row[1] for row in con.execute("SELECT profile_id,COUNT(*) FROM runs WHERE status IN ('starting','running') GROUP BY profile_id")}
    group_active = {row[0]: row[1] for row in con.execute("SELECT group_id,COUNT(*) FROM runs WHERE status IN ('starting','running') GROUP BY group_id")}
    queued = con.execute("SELECT r.*,p.max_concurrency profile_limit,g.max_concurrency group_limit FROM runs r JOIN profiles p ON p.profile_id=r.profile_id JOIN run_groups g ON g.group_id=r.group_id WHERE r.status='queued' ORDER BY r.created_at,r.id").fetchall()
    for run in queued:
        state = _dependency_state(con, run["id"])
        if state == "impossible":
            with con:
                con.execute("UPDATE runs SET status='skipped',error='success dependency did not complete',finished_at=?,updated_at=? WHERE id=?", (db.now(), db.now(), run["id"]))
                db.record_control(con, actor="orchestra", action="run.skip", outcome="skipped", target_type="run", target_id=run["id"])
            skipped.append(int(run["id"]))
            continue
        if state != "ready" or active >= limit:
            continue
        if run["profile_limit"] and profile_active.get(run["profile_id"], 0) >= run["profile_limit"]:
            continue
        if run["group_limit"] and group_active.get(run["group_id"], 0) >= run["group_limit"]:
            continue
        admitted.append(int(run["id"]))
        active += 1
        profile_active[run["profile_id"]] = profile_active.get(run["profile_id"], 0) + 1
        group_active[run["group_id"]] = group_active.get(run["group_id"], 0) + 1
    return {"admitted": admitted, "skipped": skipped}


def state(con) -> dict:
    return {status: int(con.execute("SELECT COUNT(*) FROM runs WHERE status=?", (status,)).fetchone()[0]) for status in ("queued", "starting", "running", "waiting")}
