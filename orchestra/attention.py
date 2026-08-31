"""Human/service attention with race-free leases."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from orchestra import db


class AttentionError(ValueError):
    pass


def open_request(con, run_id: int, *, kind: str, prompt: str, context=None,
                 blocking=True, expires_at=None, actor="orchestra") -> dict:
    if kind not in ("question", "decision", "alert", "permission", "verification", "protocol_failure"):
        raise AttentionError(f"unsupported attention kind: {kind}")
    attention_id, stamp = str(uuid.uuid4()), db.now()
    with con:
        con.execute("INSERT INTO attention_requests(attention_id,run_id,kind,prompt,context_json,blocking,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)", (attention_id, run_id, kind, prompt.strip(), json.dumps(context or {}, ensure_ascii=False), int(blocking), stamp, expires_at))
        if blocking:
            waiting_kind = "permission" if kind == "permission" else "verification" if kind == "verification" else "attention"
            con.execute("UPDATE runs SET status='waiting',waiting_kind=?,waiting_detail=?,updated_at=?,revision=revision+1 WHERE id=?", (waiting_kind, attention_id, stamp, run_id))
        db.append_event(con, run_id, "attention.opened", {"attention_id": attention_id, "kind": kind})
        db.record_control(con, actor=actor, action="attention.open", outcome="ok", target_type="attention", target_id=attention_id)
    return get(con, attention_id)


def get(con, attention_id: str) -> dict | None:
    row = con.execute("SELECT * FROM attention_requests WHERE attention_id=?", (attention_id,)).fetchone()
    if not row:
        return None
    value = dict(row)
    value["context"] = json.loads(value.pop("context_json"))
    response = con.execute("SELECT * FROM attention_responses WHERE attention_id=? ORDER BY created_at LIMIT 1", (attention_id,)).fetchone()
    value["response"] = dict(response) if response else None
    lease = con.execute("SELECT * FROM attention_leases WHERE attention_id=? AND released_at IS NULL", (attention_id,)).fetchone()
    value["lease"] = dict(lease) if lease else None
    return value


def inbox(con, *, status="open") -> list[dict]:
    return [get(con, row[0]) for row in con.execute("SELECT attention_id FROM attention_requests WHERE status=? ORDER BY created_at", (status,))]


def lease(con, attention_id: str, *, holder: str, seconds=60, actor=None) -> dict:
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=max(10, min(int(seconds), 600)))
    lease_id = str(uuid.uuid4())
    with con:
        request = con.execute("SELECT status FROM attention_requests WHERE attention_id=?", (attention_id,)).fetchone()
        if not request or request["status"] != "open":
            raise AttentionError("attention request is not open")
        current = con.execute("SELECT * FROM attention_leases WHERE attention_id=? AND released_at IS NULL", (attention_id,)).fetchone()
        if current and datetime.fromisoformat(current["expires_at"]) > now:
            raise AttentionError(f"attention is leased by {current['holder']}")
        if current:
            con.execute("UPDATE attention_leases SET released_at=? WHERE attention_id=?", (db.now(), attention_id))
            con.execute("DELETE FROM attention_leases WHERE attention_id=?", (attention_id,))
        con.execute("INSERT INTO attention_leases(attention_id,lease_id,holder,acquired_at,expires_at) VALUES(?,?,?,?,?)", (attention_id, lease_id, holder, now.isoformat(), expires.isoformat()))
        db.record_control(con, actor=actor or holder, action="attention.lease", outcome="ok", target_type="attention", target_id=attention_id, detail={"lease_id": lease_id, "expires_at": expires.isoformat()})
    return {"attention_id": attention_id, "lease_id": lease_id, "holder": holder, "expires_at": expires.isoformat()}


def answer(con, attention_id: str, *, answer: str, actor: str, lease_id=None, human=False) -> dict:
    answer = str(answer or "").strip()
    if not answer:
        raise AttentionError("answer is required")
    with con:
        row = con.execute("SELECT * FROM attention_requests WHERE attention_id=?", (attention_id,)).fetchone()
        if not row or row["status"] != "open":
            raise AttentionError("attention request is not open")
        current = con.execute("SELECT * FROM attention_leases WHERE attention_id=? AND released_at IS NULL", (attention_id,)).fetchone()
        if current and not human and current["lease_id"] != lease_id:
            raise AttentionError("a valid attention lease is required")
        stamp, response_id = db.now(), str(uuid.uuid4())
        changed = con.execute("UPDATE attention_requests SET status='answered',closed_at=? WHERE attention_id=? AND status='open'", (stamp, attention_id))
        if changed.rowcount != 1:
            raise AttentionError("attention request was already answered")
        con.execute("INSERT INTO attention_responses(response_id,attention_id,answer,actor,created_at) VALUES(?,?,?,?,?)", (response_id, attention_id, answer, actor, stamp))
        con.execute("UPDATE attention_leases SET released_at=? WHERE attention_id=? AND released_at IS NULL", (stamp, attention_id))
        if row["kind"] != "permission" and row["blocking"]:
            con.execute("UPDATE runs SET status='queued',waiting_kind=NULL,waiting_detail=?,updated_at=?,revision=revision+1 WHERE id=? AND status='waiting'", ("Attention answer: " + answer, stamp, row["run_id"]))
        db.append_event(con, row["run_id"], "attention.answered", {"attention_id": attention_id, "actor": actor})
        db.record_control(con, actor=actor, action="attention.answer", outcome="ok", target_type="attention", target_id=attention_id, detail={"human_override": bool(human)})
    return get(con, attention_id)
