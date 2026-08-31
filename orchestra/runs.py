"""Durable run admission and lineage."""
from __future__ import annotations

import json
import os
import secrets

from orchestra import db, groups, profiles
from orchestra.contracts import RunRequest, TERMINAL_STATES


class AdmissionError(ValueError):
    pass


def find(con, run_id: int):
    return con.execute("SELECT * FROM runs WHERE id=?", (int(run_id),)).fetchone()


def find_by_request(con, request_id: str):
    return con.execute("SELECT * FROM runs WHERE request_id=?", (request_id,)).fetchone()


def submit(con, request: RunRequest, *, parent_run_id=None,
           retry_of_run_id=None, continuation_of_run_id=None) -> tuple[object, bool]:
    existing = find_by_request(con, request.request_id)
    if existing:
        if json.loads(existing["request_snapshot"]) != request.as_dict():
            raise AdmissionError("request_id was already used for a different run")
        return existing, False
    profile = profiles.find(con, request.profile)
    if profile is None or profile["archived"] or not profile["enabled"]:
        raise AdmissionError(f"profile {request.profile!r} is unavailable")
    group = groups.find(con, request.group)
    if group is None or group["archived"]:
        raise AdmissionError(f"group {request.group!r} is unavailable")
    cwd = groups.canonical_cwd(request.cwd or group["default_cwd"] or os.getcwd())
    dependencies = request.after
    for dependency in dependencies:
        if find(con, dependency.run_id) is None:
            raise AdmissionError(f"dependency run {dependency.run_id} does not exist")
    stamp = db.now()
    with con:
        seq = int(group["last_run_seq"]) + 1
        con.execute("UPDATE run_groups SET last_run_seq=?,updated_at=? WHERE group_id=?", (seq, stamp, group["group_id"]))
        profile_snapshot = profiles.payload(profile)
        cursor = con.execute("INSERT INTO runs(slug,request_id,group_id,group_seq,profile_id,root_run_id,parent_run_id,retry_of_run_id,continuation_of_run_id,title,objective,strategy,permission_mode,max_rounds,active_seconds_limit,verify_json,max_children,max_child_tier,cwd,start_ref,route_provider,route_model,route_effort,profile_snapshot,request_snapshot,requested_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            secrets.token_hex(8), request.request_id, group["group_id"], seq,
            profile["profile_id"], None, parent_run_id, retry_of_run_id,
            continuation_of_run_id, request.title, request.objective, request.strategy,
            request.permission_mode, request.max_rounds, request.active_seconds,
            json.dumps(request.verify.as_dict()) if request.verify else None,
            request.max_children, request.max_child_tier, cwd, request.ref,
            profile["provider"], profile["model"], profile["effort"],
            json.dumps(profile_snapshot, ensure_ascii=False),
            json.dumps(request.as_dict(), ensure_ascii=False), request.requested_by,
            stamp, stamp))
        run_id = int(cursor.lastrowid)
        root_id = run_id if parent_run_id is None else int(find(con, parent_run_id)["root_run_id"] or parent_run_id)
        con.execute("UPDATE runs SET root_run_id=? WHERE id=?", (root_id, run_id))
        con.executemany("INSERT INTO run_dependencies(run_id,depends_on_run_id,condition) VALUES(?,?,?)", ((run_id, item.run_id, item.condition) for item in dependencies))
        db.append_event(con, run_id, "run.queued", {"strategy": request.strategy, "profile": profile["slug"]})
        db.record_control(con, actor=request.requested_by, action="run.create", outcome="ok", target_type="run", target_id=run_id, request_id=request.request_id)
    return find(con, run_id), True


def clone(con, source_run_id: int, *, request_id: str, kind: str,
          requested_by="operator", direction: str | None = None):
    source = find(con, source_run_id)
    if source is None or source["status"] not in TERMINAL_STATES:
        raise AdmissionError("retry and continuation require a terminal source run")
    snapshot = json.loads(source["request_snapshot"])
    snapshot["request_id"] = request_id
    snapshot["requested_by"] = requested_by
    if direction:
        snapshot["objective"] = source["objective"] + "\n\nContinuation direction:\n" + direction
    request = RunRequest.from_mapping(snapshot)
    return submit(con, request,
                  retry_of_run_id=source_run_id if kind == "retry" else None,
                  continuation_of_run_id=source_run_id if kind == "continuation" else None)


def payload(row, *, detail=False) -> dict:
    value = {key: row[key] for key in (
        "id", "slug", "request_id", "group_id", "group_seq", "profile_id",
        "root_run_id", "parent_run_id", "retry_of_run_id", "continuation_of_run_id",
        "title", "objective", "strategy", "permission_mode", "max_rounds",
        "active_seconds_limit", "active_seconds", "status", "waiting_kind",
        "waiting_detail", "cwd", "workdir", "branch", "start_ref", "end_ref",
        "dsh_session_id", "resume_count", "goal_id", "goal_state", "rounds_started",
        "route_provider", "route_model", "route_effort", "cache_epoch",
        "tokens_input", "tokens_output", "tokens_cache_read", "tokens_cache_write",
        "tokens_total", "summary", "error", "requested_by", "created_at",
        "started_at", "updated_at", "finished_at", "revision")}
    value["verify"] = json.loads(row["verify_json"]) if row["verify_json"] else None
    if detail:
        value["profile_snapshot"] = json.loads(row["profile_snapshot"])
        value["request_snapshot"] = json.loads(row["request_snapshot"])
        value["git"] = {"status": row["git_status"], "diff_stat": row["git_diff_stat"]}
    return value
