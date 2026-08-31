"""Project DSH JSONL into normalized, idempotent run facts."""
from __future__ import annotations

import json
from pathlib import Path

from orchestra import db


class JournalError(RuntimeError):
    pass


def _records(path: Path):
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    for index, encoded in enumerate(lines):
        complete = encoded.endswith((b"\n", b"\r"))
        try:
            value = json.loads(encoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if index == len(lines) - 1 and not complete:
                return
            raise JournalError(f"corrupt DSH journal {path} line {index + 1}: {exc}") from exc
        if not isinstance(value, dict):
            raise JournalError(f"non-object DSH journal row in {path} line {index + 1}")
        yield index, value


def _usage(data: dict) -> dict | None:
    value = data.get("usage")
    if not isinstance(value, dict):
        return None
    input_tokens = int(value.get("inputTokens") or 0)
    output_tokens = int(value.get("outputTokens") or 0)
    cache_read = int(value.get("cacheReadTokens") or value.get("cachedInputTokens") or 0)
    cache_write = int(value.get("cacheWriteTokens") or 0)
    total = int(value.get("totalTokens") or
                input_tokens + output_tokens + cache_read + cache_write)
    return {"input": input_tokens, "output": output_tokens, "cache_read": cache_read, "cache_write": cache_write, "total": total}


def _goal_rounds(con, run_id: int) -> int:
    by_goal = {}
    for row in con.execute("SELECT payload_json FROM events WHERE run_id=? AND type='dsh.goal/change'", (run_id,)):
        try:
            data = json.loads(row[0]); goal_id = (data.get("goal") or {}).get("id")
            if goal_id:
                by_goal[goal_id] = max(by_goal.get(goal_id, 0), int(data.get("roundsStarted") or 0))
        except (TypeError, ValueError):
            continue
    return sum(by_goal.values())


def _reconcile(con, run_id: int, root: str | Path) -> dict:
    root = Path(root)
    inserted = usage_count = 0
    goal_complete = False
    if not root.exists():
        return {"events": 0, "usage_events": 0, "goal_complete": False}
    for path in sorted(root.rglob("session.jsonl")):
        rows = list(_records(path))
        if not rows:
            continue
        _, header = rows[0]
        if (header.get("type") != "session" or header.get("version") != 0
                or not isinstance(header.get("id"), str) or not header["id"]
                or not isinstance(header.get("createdAt"), int) or header["createdAt"] < 0
                or not isinstance(header.get("delegationDepth"), int)
                or header["delegationDepth"] < 0):
            raise JournalError(f"DSH journal {path} has no valid session header")
        session_id = header["id"]
        descendant = session_id if header.get("parentSession") else None
        for line_index, row in rows[1:]:
            event_type = row.get("type")
            data = row.get("data") or {}
            source_seq = row.get("seq")
            if (not isinstance(event_type, str) or not isinstance(data, dict)
                    or isinstance(source_seq, bool) or not isinstance(source_seq, int)
                    or source_seq < 0):
                raise JournalError(f"malformed DSH event in {path} line {line_index + 1}")
            if not db.append_event(con, run_id, "dsh." + event_type, data,
                                   session_id=session_id, source_seq=source_seq):
                continue
            inserted += 1
            if event_type == "goal/change":
                goal = data.get("goal") or {}
                phase = goal.get("phase")
                con.execute("UPDATE runs SET goal_id=?,goal_state=?,rounds_started=?,updated_at=?,revision=revision+1 WHERE id=?", (goal.get("id"), phase, _goal_rounds(con, run_id), db.now(), run_id))
                goal_complete = goal_complete or phase in ("complete", "completed")
            usage = _usage(data)
            if usage is not None:
                message = data.get("message") or {}
                source = message.get("source") or data
                epoch_row = con.execute("SELECT cache_epoch FROM runs WHERE id=?", (run_id,)).fetchone()
                epoch = int(epoch_row[0]) if epoch_row else 0
                con.execute("INSERT INTO usage_events(run_id,session_id,source_seq,event_type,provider,model,descendant_session_id,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,total_tokens,cache_epoch,observed_at,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, session_id, source_seq, event_type, source.get("provider"), source.get("model"), descendant, usage["input"], usage["output"], usage["cache_read"], usage["cache_write"], usage["total"], epoch, db.now(), json.dumps(data, ensure_ascii=False)))
                usage_count += 1
                con.execute("UPDATE runs SET tokens_input=tokens_input+?,tokens_output=tokens_output+?,tokens_cache_read=tokens_cache_read+?,tokens_cache_write=tokens_cache_write+?,tokens_total=tokens_total+? WHERE id=?", (usage["input"], usage["output"], usage["cache_read"], usage["cache_write"], usage["total"], run_id))
    return {"events": inserted, "usage_events": usage_count, "goal_complete": goal_complete}


def reconcile(con, run_id: int, root: str | Path) -> dict:
    """Apply one journal snapshot atomically, including its usage facts."""
    con.execute("SAVEPOINT dsh_reconcile")
    try:
        result = _reconcile(con, run_id, root)
    except BaseException:
        con.execute("ROLLBACK TO dsh_reconcile")
        con.execute("RELEASE dsh_reconcile")
        raise
    con.execute("RELEASE dsh_reconcile")
    con.commit()
    return result
