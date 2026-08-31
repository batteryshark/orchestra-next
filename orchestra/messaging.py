"""Durable operator-to-run controls consumed at process boundaries."""
from __future__ import annotations

import json
import uuid

from orchestra import db
from orchestra.contracts import TERMINAL_STATES


class RunClosed(RuntimeError):
    pass


def queue(con, run_id: int, *, kind: str, body: str, sender: str, detail=None) -> dict:
    if kind not in ("tell", "interrupt", "reroute", "resume", "stop"):
        raise ValueError(f"unsupported run control: {kind}")
    run = con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise LookupError(f"run {run_id} does not exist")
    if run["status"] in TERMINAL_STATES:
        raise RunClosed(f"run {run_id} is terminal")
    message_id, stamp = str(uuid.uuid4()), db.now()
    payload = body if detail is None else json.dumps({"body": body, **detail}, ensure_ascii=False)
    with con:
        con.execute("INSERT INTO messages(message_id,run_id,sender,kind,body,created_at) VALUES(?,?,?,?,?,?)", (message_id, run_id, sender, kind, payload, stamp))
        db.record_control(con, actor=sender, action=f"run.{kind}", outcome="queued", target_type="run", target_id=run_id, detail=detail)
    return {"message_id": message_id, "run_id": run_id, "kind": kind, "status": "queued", "created_at": stamp}


def claim_pending(con, run_id: int, *, safe_boundary: bool = True) -> list[dict]:
    with con:
        rows = con.execute("SELECT * FROM messages WHERE run_id=? AND status='queued' "
                           + ("" if safe_boundary else "AND kind<>'tell' ")
                           + "ORDER BY created_at", (run_id,)).fetchall()
        for row in rows:
            con.execute("UPDATE messages SET status='delivered',delivered_at=? WHERE message_id=? AND status='queued'", (db.now(), row["message_id"]))
    return [dict(row) for row in rows]


def thread(con, run_id: int) -> list[dict]:
    return [dict(row) for row in con.execute("SELECT * FROM messages WHERE run_id=? ORDER BY created_at", (run_id,))]
