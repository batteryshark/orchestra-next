"""Small JSON API for the standalone Orchestra-next kernel."""
from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestra import antigravity, artifacts, attention, auth, child_runs, claude, db, dsh, groups, messaging, paths, profiles, runs, settings, storage, worktree
from orchestra.contracts import ContractError, RunRequest

PREFIX = "/api"
PROFILE_FIELDS = frozenset(("name", "provider", "model", "effort", "tier", "max_concurrency", "enabled", "archived", "note"))
CATALOG_REFRESH_FLOOR = 10.0  # seconds; a refresh sooner than this serves the cache (each probe spawns DSH)


@dataclass
class Response:
    status: int
    data: object = None
    headers: dict | None = None


@dataclass
class FileResponse:
    status: int
    path: Path
    media_type: str
    headers: dict | None = None


class Problem(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def envelope(con, data) -> dict:
    return {"instance_id": db.instance_id(con), "board_revision": db.board_revision(con), "data": data}


def _actor(identity) -> str:
    return f"{identity.kind}:{identity.subject_id}"


def _need(identity, authority: str, target=None):
    try:
        auth.authorize(identity, authority, target_run_id=target)
    except auth.AuthError as exc:
        raise Problem(401 if identity is None else 403, str(exc)) from exc


def _operator(identity) -> None:
    _need(identity, "read")
    if identity.kind not in ("device", "network"):
        raise Problem(403, "an operator device is required")


def _id(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise Problem(404, "invalid run id") from exc
    if result < 1:
        raise Problem(404, "invalid run id")
    return result


def _body(value) -> dict:
    if not isinstance(value, dict):
        raise Problem(400, "request body must be an object")
    return value


def _managed(row) -> dict:
    return dict(row)


def _event_page(con, query: dict, run_id: int | None = None) -> list[dict]:
    """One page of the events feed: `after`/`before` id cursors, `order`, `limit` (1..500)."""
    order = query.get("order", "asc")
    if order not in ("asc", "desc"):
        raise Problem(400, "order must be asc or desc")
    try:
        after = int(query.get("after", 0) or 0)
        before = int(query.get("before", 0) or 0)
        limit = max(1, min(int(query.get("limit", 500) or 500), 500))
    except ValueError as exc:
        raise Problem(400, "after, before, and limit must be integers") from exc
    clause, params = "id>?", [after]
    if run_id is not None:
        clause, params = "run_id=? AND " + clause, [run_id, *params]
    if before:
        clause += " AND id<?"; params.append(before)
    direction = " DESC" if order == "desc" else ""
    values = []
    for row in con.execute(f"SELECT * FROM events WHERE {clause} ORDER BY id{direction} LIMIT {limit}", params):
        value = dict(row); value["payload"] = json.loads(value.pop("payload_json")); values.append(value)
    return values


DELEGATION_EVENT = "delegation.antigravity"
DELEGATION_USAGE = ("input", "output", "thinking", "cache_read", "total")
DELEGATION_RESPONSE_LIMIT = 8_000
DELEGATION_TEXT_LIMIT = 2_000


def _delegations(con, run_id: int) -> list[dict]:
    values = []
    for row in con.execute("SELECT id,payload_json,created_at FROM events WHERE run_id=? AND type=? ORDER BY id DESC", (run_id, DELEGATION_EVENT)):
        values.append({**json.loads(row["payload_json"]), "event_id": row["id"], "created_at": row["created_at"]})
    return values


def _record_delegation(con, run, data: dict, *, actor: str) -> dict:
    model, mode, usage = data.get("model"), data.get("mode"), data.get("usage")
    if not isinstance(model, str) or not model.strip():
        raise Problem(400, "model must be a non-empty string")
    if mode != "review":
        raise Problem(400, "mode must be review")
    if not isinstance(usage, dict) or any(isinstance(usage.get(key), bool) or not isinstance(usage.get(key), int) or usage[key] < 0 for key in DELEGATION_USAGE):
        raise Problem(400, "usage must contain non-negative integer " + ", ".join(DELEGATION_USAGE))
    usage = {key: usage[key] for key in DELEGATION_USAGE}
    conversation = data.get("conversation_id") if isinstance(data.get("conversation_id"), str) else None
    status = str(data.get("status") or "ERROR")[:40]
    turns = data.get("num_turns") if isinstance(data.get("num_turns"), int) and not isinstance(data.get("num_turns"), bool) else 0
    duration = data.get("duration_seconds") if isinstance(data.get("duration_seconds"), (int, float)) and not isinstance(data.get("duration_seconds"), bool) else None
    def text(key, limit):
        value = data.get(key)
        return value[:limit] if isinstance(value, str) else None
    # Antigravity reports cumulative usage per conversation; store the delta against the newest prior turn.
    prior = next((item for item in _delegations(con, run["id"]) if conversation and item.get("conversation_id") == conversation), None)
    base = (prior or {}).get("usage") or {}
    delta = {key: max(0, usage[key] - int(base.get(key) or 0)) for key in DELEGATION_USAGE}
    payload = {
        "model": model.strip(), "mode": mode, "objective": text("objective", DELEGATION_RESPONSE_LIMIT) or "",
        "conversation_id": conversation, "status": status, "num_turns": turns, "duration_seconds": duration,
        "usage": usage, "delta": delta, "response": text("response", DELEGATION_RESPONSE_LIMIT),
        "truncated": bool(data.get("truncated")) or len(data.get("response") or "") > DELEGATION_RESPONSE_LIMIT,
        "error": text("error", DELEGATION_TEXT_LIMIT), "stderr": text("stderr", DELEGATION_TEXT_LIMIT),
    }
    with con:
        db.append_event(con, run["id"], DELEGATION_EVENT, payload)
        event_id = int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
        if conversation:
            # Tokens stay out of the run's tokens_* columns: Antigravity is a distinct source.
            con.execute("INSERT OR IGNORE INTO usage_events(run_id,session_id,source_seq,event_type,provider,model,descendant_session_id,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,total_tokens,cache_epoch,observed_at,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (run["id"], conversation, turns, "antigravity.delegate", "antigravity", payload["model"], None, delta["input"], delta["output"], delta["cache_read"], 0, delta["total"], run["cache_epoch"], db.now(), json.dumps(usage)))
        db.record_control(con, actor=actor, action="run.delegate", outcome=status, target_type="run", target_id=run["id"], detail={"model": payload["model"], "conversation_id": conversation, "delta": delta})
    return {**payload, "event_id": event_id}


class API:
    def __init__(self, con):
        self.con = con

    def handle(self, method: str, path: str, query: dict, body, identity) -> Response | FileResponse:
        if path == PREFIX + "/health" and method == "GET":
            return Response(200, envelope(self.con, {"status": "ok"}))
        if path == PREFIX + "/openapi.json" and method == "GET":
            return Response(200, openapi())
        if path == PREFIX + "/auth/pair/redeem" and method == "POST":
            data = _body(body)
            device, token = auth.redeem_pairing(self.con, data.get("pairing_id", ""), data.get("code", ""), data.get("name", ""))
            if data.get("cookie") is True:
                return Response(201, envelope(self.con, {"device": device}), headers={"Set-Cookie": auth.cookie_header(token)})
            return Response(201, envelope(self.con, {"device": device, "token": token}))
        if not path.startswith(PREFIX + "/"):
            raise Problem(404, "not found")
        parts = path[len(PREFIX) + 1:].split("/")

        if parts == ["runs"]:
            if method == "GET":
                _need(identity, "read")
                order = query.get("order", "asc")
                if order not in ("asc", "desc"):
                    raise Problem(400, "order must be asc or desc")
                try:
                    limit = max(1, min(int(query.get("limit", 200) or 200), 200))
                    after = int(query.get("after", 0) or 0)
                    before = int(query.get("before", 0) or 0)
                except ValueError as exc:
                    raise Problem(400, "after, before, and limit must be integers") from exc
                where, params = [], []
                if after:
                    where.append("id>?"); params.append(after)
                if before:
                    where.append("id<?"); params.append(before)
                statuses = [item for item in (query.get("status") or "").split(",") if item]
                unknown = [item for item in statuses if item not in db.RUN_ACTIVE + db.RUN_TERMINAL]
                if unknown:
                    raise Problem(400, "unknown status: " + ", ".join(unknown))
                if statuses:
                    where.append("status IN (%s)" % ",".join("?" for _ in statuses)); params.extend(statuses)
                if query.get("group"):
                    target = groups.find(self.con, query["group"])
                    if target is None:
                        raise Problem(400, f"group {query['group']!r} does not exist")
                    where.append("group_id=?"); params.append(target["group_id"])
                if query.get("profile"):
                    target = profiles.find(self.con, query["profile"])
                    if target is None:
                        raise Problem(400, f"profile {query['profile']!r} does not exist")
                    where.append("profile_id=?"); params.append(target["profile_id"])
                clause = (" WHERE " + " AND ".join(where)) if where else ""
                direction = " DESC" if order == "desc" else ""
                rows = self.con.execute(f"SELECT * FROM runs{clause} ORDER BY id{direction} LIMIT {limit}", params).fetchall()
                return Response(200, envelope(self.con, [runs.payload(row) for row in rows]))
            if method == "POST":
                _need(identity, "dispatch")
                try:
                    request = RunRequest.from_mapping(_body(body))
                    run, created = runs.submit(self.con, request)
                except (ContractError, ValueError) as exc:
                    raise Problem(400, str(exc)) from exc
                return Response(201 if created else 200, envelope(self.con, runs.payload(run, detail=True)))

        if parts == ["messages"] and method == "GET":
            _need(identity, "read")
            status, kind = query.get("status") or None, query.get("kind") or None
            if status and status not in messaging.STATUSES:
                raise Problem(400, "status must be one of " + ", ".join(messaging.STATUSES))
            if kind and kind not in messaging.KINDS:
                raise Problem(400, "kind must be one of " + ", ".join(messaging.KINDS))
            try:
                run_id = int(query.get("run", 0) or 0)
                before = int(query.get("before", 0) or 0)
                limit = max(1, min(int(query.get("limit", 200) or 200), 200))
            except ValueError as exc:
                raise Problem(400, "run, before, and limit must be integers") from exc
            return Response(200, envelope(self.con, messaging.ledger(self.con, status=status, kind=kind, run_id=run_id, before=before, limit=limit)))

        if len(parts) >= 2 and parts[0] == "runs":
            run_id = _id(parts[1])
            run = runs.find(self.con, run_id)
            if run is None:
                raise Problem(404, f"run {run_id} does not exist")
            suffix = parts[2:]
            if not suffix and method == "GET":
                _need(identity, "read", target=run_id)
                data = runs.payload(run, detail=True)
                data["child_count"] = int(self.con.execute("SELECT COUNT(*) FROM runs WHERE parent_run_id=?", (run_id,)).fetchone()[0])
                return Response(200, envelope(self.con, data))
            if suffix == ["events"] and method == "GET":
                _need(identity, "read", target=run_id)
                return Response(200, envelope(self.con, _event_page(self.con, query, run_id)))
            if suffix == ["usage"] and method == "GET":
                _need(identity, "read", target=run_id)
                after = int(query.get("after", 0) or 0)
                return Response(200, envelope(self.con, [dict(row) for row in self.con.execute("SELECT * FROM usage_events WHERE run_id=? AND id>? ORDER BY id LIMIT 500", (run_id, after))]))
            if suffix == ["dependencies"] and method == "GET":
                _need(identity, "read", target=run_id)
                return Response(200, envelope(self.con, [dict(row) for row in self.con.execute("SELECT * FROM run_dependencies WHERE run_id=?", (run_id,))]))
            if suffix == ["messages"] and method == "GET":
                _need(identity, "read", target=run_id)
                return Response(200, envelope(self.con, messaging.thread(self.con, run_id)))
            if suffix in (["tell"], ["interrupt"], ["pause"], ["stop"]) and method == "POST":
                kind = suffix[0]
                authority = "stop" if kind == "stop" else "resume"
                _need(identity, authority, target=run_id)
                data = _body(body)
                item = messaging.queue(self.con, run_id, kind=kind, body=str(data.get("message") or data.get("reason") or ""), sender=_actor(identity))
                return Response(202, envelope(self.con, item))
            if suffix == ["stop-tree"] and method == "POST":
                _need(identity, "stop", target=run_id)
                reason = str(_body(body).get("reason") or "")
                stopped = []
                for target in [run_id] + child_runs.descendants(self.con, run_id):
                    try:
                        messaging.queue(self.con, target, kind="stop", body=reason, sender=_actor(identity))
                    except messaging.RunClosed:
                        continue
                    stopped.append(target)
                return Response(202, envelope(self.con, {"run_id": run_id, "stopped": stopped}))
            if suffix == ["reroute"] and method == "POST":
                _need(identity, "reroute", target=run_id)
                data = _body(body)
                provider, model, effort = data.get("provider"), data.get("model"), data.get("effort")
                try:
                    catalog, _ = dsh.cached_catalog(run["workdir"] or run["cwd"])
                except dsh.DshError as exc:
                    raise Problem(503, str(exc)) from exc
                if (provider, model) not in catalog or effort is not None and effort not in catalog[(provider, model)]:
                    raise Problem(400, "route is not advertised by DSH ACP")
                item = messaging.queue(self.con, run_id, kind="reroute", body=json.dumps({"provider": provider, "model": model, "effort": effort, "body": data.get("message")}), sender=_actor(identity))
                return Response(202, envelope(self.con, item))
            if suffix == ["resume"] and method == "POST":
                _need(identity, "resume", target=run_id)
                with self.con:
                    changed = self.con.execute("UPDATE runs SET status='queued',waiting_kind=NULL,waiting_detail=NULL,updated_at=? WHERE id=? AND status='waiting'", (db.now(), run_id))
                    db.record_control(self.con, actor=_actor(identity), action="run.resume", outcome="queued" if changed.rowcount else "not_waiting", target_type="run", target_id=run_id)
                if not changed.rowcount:
                    raise Problem(409, "run is not waiting")
                return Response(202, envelope(self.con, runs.payload(runs.find(self.con, run_id))))
            if suffix == ["retry"] and method == "POST":
                _need(identity, "retry", target=run_id)
                data = _body(body)
                created, _ = runs.clone(self.con, run_id, request_id=data.get("request_id", ""), kind="retry", requested_by=_actor(identity))
                return Response(201, envelope(self.con, runs.payload(created)))
            if suffix == ["continue"] and method == "POST":
                _need(identity, "retry", target=run_id)
                data = _body(body)
                created, _ = runs.clone(self.con, run_id, request_id=data.get("request_id", ""), kind="continuation", requested_by=_actor(identity), direction=data.get("direction"))
                return Response(201, envelope(self.con, runs.payload(created)))
            if suffix == ["attention"] and method == "POST":
                _need(identity, "attention", target=run_id)
                data = _body(body)
                item = attention.open_request(self.con, run_id, kind=data.get("kind", "question"), prompt=data.get("prompt", ""), context=data.get("context"), actor=_actor(identity))
                return Response(201, envelope(self.con, item))
            if suffix == ["children"]:
                if method == "GET":
                    _need(identity, "read", target=run_id)
                    return Response(200, envelope(self.con, child_runs.for_parent(self.con, run_id)))
                if method == "POST":
                    _need(identity, "delegate", target=run_id)
                    child = child_runs.create(self.con, run_id, _body(body), actor=_actor(identity))
                    return Response(201, envelope(self.con, runs.payload(child)))
            if suffix == ["artifacts"]:
                if method == "GET":
                    _need(identity, "read", target=run_id)
                    return Response(200, envelope(self.con, artifacts.for_run(self.con, run_id)))
                if method == "POST":
                    _need(identity, "artifact", target=run_id)
                    data = _body(body)
                    return Response(201, envelope(self.con, artifacts.publish(self.con, run_id, data.get("path", ""), name=data.get("name"))))
            if suffix == ["delegate"] and method == "POST":
                # The daemon runs agy itself: the worker's shell sits inside the DSH sandbox, which cannot exec it.
                _need(identity, "delegate", target=run_id)
                data = _body(body)
                if not json.loads(run["request_snapshot"]).get("allow_antigravity"):
                    raise Problem(403, "Antigravity delegation is not authorized for this run; dispatch with --allow-antigravity")
                if data.get("mode", "review") != "review":
                    raise Problem(400, "delegate mode must be review")
                if not run["workdir"]:
                    raise Problem(409, "run has no worktree yet; delegation needs a working directory")
                objective = str(data.get("objective") or "").strip()
                if not objective:
                    raise Problem(400, "objective is required")
                try:
                    catalog = antigravity.cached_catalog()
                except ValueError as exc:
                    raise Problem(503, str(exc)) from exc
                model = data.get("model")
                if model not in catalog:
                    raise Problem(400, f"unknown Antigravity model {model!r}; available: {', '.join(catalog)}")
                # Each review is a fresh conversation: resumed ones gave empty answers and earn no cache credit.
                result = antigravity.delegate(objective, model, run["workdir"], None)
                record = _record_delegation(self.con, run, result, actor=_actor(identity))
                return Response(201 if record["status"] == "SUCCESS" else 502, envelope(self.con, record))
            if suffix == ["delegations"]:
                if method == "GET":
                    _need(identity, "read", target=run_id)
                    return Response(200, envelope(self.con, _delegations(self.con, run_id)))
                if method == "POST":
                    _need(identity, "delegate", target=run_id)
                    return Response(201, envelope(self.con, _record_delegation(self.con, run, _body(body), actor=_actor(identity))))
            if suffix == ["changes"] and method == "GET":
                _need(identity, "read", target=run_id)
                if not run["workdir"]:
                    return Response(200, envelope(self.con, {"status": "", "diff": ""}))
                import subprocess
                status = worktree.status(Path(run["workdir"]))
                try:
                    diff = subprocess.run(["git", "-C", run["workdir"], "diff", run["start_ref"] or "HEAD"], capture_output=True, text=True, timeout=30).stdout
                except subprocess.TimeoutExpired as exc:
                    raise Problem(504, "git diff did not finish within 30 seconds") from exc
                return Response(200, envelope(self.con, {"status": status, "diff": diff[:200_000]}))
            if suffix == ["merge"] and method == "POST":
                _operator(identity)
                try:
                    result = worktree.merge_into_owner(Path(run["cwd"]), run["branch"])
                except RuntimeError as exc:
                    db.record_control(self.con, actor=_actor(identity), action="run.merge", outcome="refused", target_type="run", target_id=run_id, detail={"error": str(exc)}); self.con.commit()
                    raise Problem(409, str(exc)) from exc
                db.record_control(self.con, actor=_actor(identity), action="run.merge", outcome="ok", target_type="run", target_id=run_id, detail=result); self.con.commit()
                return Response(200, envelope(self.con, result))
            if suffix == ["pin"] and method == "POST":
                _operator(identity)
                return Response(200, envelope(self.con, storage.pin(self.con, run_id, actor=_actor(identity), reason=_body(body).get("reason"))))

        if parts == ["profiles"]:
            if method == "GET":
                _need(identity, "read")
                return Response(200, envelope(self.con, [profiles.payload(row) for row in profiles.all_profiles(self.con, include_archived=True)]))
            if method == "POST":
                _operator(identity)
                data = _body(body)
                row = profiles.create(self.con, name=data.get("name"), slug=data.get("slug"), provider=data.get("provider"), model=data.get("model"), effort=data.get("effort"), tier=data.get("tier", 1), max_concurrency=data.get("max_concurrency"), note=data.get("note"), actor=_actor(identity))
                return Response(201, envelope(self.con, profiles.payload(row)))
        if len(parts) == 2 and parts[0] == "profiles" and method == "PATCH":
            _operator(identity)
            data = _body(body).copy()
            try:
                revision = int(data.pop("expected_revision"))
            except (KeyError, TypeError, ValueError) as exc:
                raise Problem(400, "expected_revision is required") from exc
            unknown = set(data) - PROFILE_FIELDS
            if unknown:
                raise Problem(400, "unknown profile fields: " + ", ".join(sorted(unknown)))
            try:
                row = profiles.update(self.con, parts[1], expected_revision=revision,
                                      actor=_actor(identity), **data)
            except (TypeError, ValueError) as exc:
                raise Problem(400, str(exc) or "invalid profile fields") from exc
            except RuntimeError as exc:
                raise Problem(409, str(exc)) from exc
            return Response(200, envelope(self.con, profiles.payload(row)))

        if parts == ["settings"]:
            if method == "GET":
                _need(identity, "read")
                return Response(200, envelope(self.con, settings.payload(self.con)))
            if method == "PATCH":
                _operator(identity)
                data = _body(body).copy()
                try:
                    revision = int(data.pop("expected_revision"))
                except (KeyError, TypeError, ValueError) as exc:
                    raise Problem(400, "expected_revision is required") from exc
                try:
                    settings.update(self.con, data, expected_revision=revision, actor=_actor(identity))
                except ValueError as exc:
                    raise Problem(400, str(exc)) from exc
                except RuntimeError as exc:
                    raise Problem(409, str(exc)) from exc
                return Response(200, envelope(self.con, settings.payload(self.con)))
        if len(parts) == 2 and parts[0] == "scheduler" and parts[1] in ("pause", "resume") and method == "POST":
            _operator(identity)
            note = _body(body).get("note")
            settings.set_paused(self.con, parts[1] == "pause", actor=_actor(identity), note=None if note is None else str(note)[:500])
            return Response(200, envelope(self.con, settings.payload(self.con)))

        if parts == ["groups"]:
            if method == "GET":
                _need(identity, "read")
                return Response(200, envelope(self.con, [dict(row) for row in groups.all_groups(self.con, include_archived=True)]))
            if method == "POST":
                _operator(identity)
                data = _body(body)
                row = groups.create(self.con, data.get("name"), slug=data.get("slug"), cwd=data.get("cwd"), actor=_actor(identity))
                return Response(201, envelope(self.con, dict(row)))
        if len(parts) == 2 and parts[0] == "groups" and method == "PATCH":
            _operator(identity)
            data = _body(body).copy()
            try:
                revision = int(data.pop("expected_revision"))
            except (KeyError, TypeError, ValueError) as exc:
                raise Problem(400, "expected_revision is required") from exc
            unknown = set(data) - {"name", "cwd", "archived"}
            if unknown or len(data) != 1:
                raise Problem(400, "change exactly one of name, cwd, or archived")
            try:
                if "name" in data:
                    row = groups.rename(self.con, parts[1], data["name"], expected_revision=revision, actor=_actor(identity))
                elif "cwd" in data:
                    row = groups.set_cwd(self.con, parts[1], data["cwd"], expected_revision=revision, actor=_actor(identity))
                else:
                    if not isinstance(data["archived"], bool):
                        raise Problem(400, "archived must be a boolean")
                    row = groups.set_archived(self.con, parts[1], data["archived"], expected_revision=revision, actor=_actor(identity))
            except RuntimeError as exc:
                raise Problem(409, str(exc)) from exc
            return Response(200, envelope(self.con, dict(row)))

        if parts == ["attention"] and method == "GET":
            _need(identity, "read")
            return Response(200, envelope(self.con, attention.inbox(self.con, status=query.get("status", "open"))))
        if len(parts) == 3 and parts[0] == "attention" and parts[2] == "lease" and method == "POST":
            _need(identity, "attention-answer")
            data = _body(body)
            return Response(201, envelope(self.con, attention.lease(self.con, parts[1], holder=_actor(identity), seconds=data.get("seconds", 60))))
        if len(parts) == 3 and parts[0] == "attention" and parts[2] == "answer" and method == "POST":
            if identity and identity.kind in ("device", "network"):
                _need(identity, "read")
                human = True
            else:
                _need(identity, "attention-answer")
                human = False
            data = _body(body)
            item = attention.answer(self.con, parts[1], answer=data.get("answer", ""), actor=_actor(identity), lease_id=data.get("lease_id"), human=human)
            return Response(200, envelope(self.con, item))

        if parts == ["controls"] and method == "GET":
            _need(identity, "read")
            after = int(query.get("after", 0) or 0)
            return Response(200, envelope(self.con, [dict(row) for row in self.con.execute("SELECT * FROM control_events WHERE id>? ORDER BY id LIMIT 500", (after,))]))
        if parts == ["events"] and method == "GET":
            _need(identity, "read")
            return Response(200, envelope(self.con, _event_page(self.con, query)))
        if parts == ["usage"] and method == "GET":
            _need(identity, "read")
            after = int(query.get("after", 0) or 0)
            rows = self.con.execute("SELECT * FROM usage_events WHERE id>? ORDER BY id LIMIT 500", (after,))
            return Response(200, envelope(self.con, [dict(row) for row in rows]))
        if parts == ["usage", "summary"] and method == "GET":
            _need(identity, "read")
            return Response(200, envelope(self.con, _usage_summary(self.con, query)))
        if parts == ["callbacks"] and method == "GET":
            _need(identity, "read")
            after = int(query.get("after", 0) or 0)
            rows = self.con.execute("SELECT * FROM control_events WHERE action LIKE 'callback.%' AND id>? ORDER BY id LIMIT 200", (after,))
            return Response(200, envelope(self.con, [dict(row) for row in rows]))
        if parts == ["storage"] and method == "GET":
            _need(identity, "read")
            return Response(200, envelope(self.con, storage.report(self.con)))
        if parts == ["storage", "plans"] and method == "POST":
            _operator(identity)
            return Response(201, envelope(self.con, storage.create_plan(self.con, actor=_actor(identity), older_than_days=_body(body).get("older_than_days", 30))))
        if len(parts) == 3 and parts[:2] == ["storage", "plans"] and method == "GET":
            _need(identity, "read")
            item = storage.get_plan(self.con, parts[2])
            if item is None:
                raise Problem(404, "prune plan does not exist")
            return Response(200, envelope(self.con, item))
        if len(parts) == 4 and parts[:2] == ["storage", "plans"] and parts[3] == "apply" and method == "POST":
            _operator(identity)
            return Response(200, envelope(self.con, storage.apply_plan(self.con, parts[2], actor=_actor(identity))))
        if len(parts) == 3 and parts[0] == "artifacts" and parts[2] == "content" and method == "GET":
            _need(identity, "read")
            item = artifacts.stored_file(self.con, parts[1])
            if item is None:
                raise Problem(404, "artifact does not exist")
            file_path, metadata = item
            fallback = "".join(c if 32 < ord(c) < 127 and c != '"' else "_" for c in metadata["name"]) or "artifact"
            disposition = f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{urllib.parse.quote(metadata['name'])}"
            return FileResponse(200, file_path, metadata["media_type"], headers={"Content-Disposition": disposition})
        if parts == ["models"] and method == "GET":
            _need(identity, "read")
            age = dsh.catalog_cache_age()
            refresh = query.get("refresh") == "1" and (age is None or age >= CATALOG_REFRESH_FLOOR)
            try:
                choices, checked_at = dsh.cached_catalog(str(paths.state_dir()), refresh=refresh)
            except dsh.DshError as exc:
                raise Problem(503, str(exc)) from exc
            models = [{"provider": provider, "model": model, "efforts": sorted(efforts)}
                      for (provider, model), efforts in sorted(choices.items())]
            return Response(200, envelope(self.con, {"models": models, "capabilities": {"native_web_search": dsh.web_search_capability()}, "checked_at": checked_at}))
        if parts == ["antigravity", "models"] and method == "GET":
            _need(identity, "read")
            try:
                return Response(200, envelope(self.con, {"models": antigravity.cached_catalog(refresh=query.get("refresh") == "1")}))
            except ValueError as exc:
                raise Problem(503, str(exc)) from exc
        if parts == ["readiness"] and method == "GET":
            _need(identity, "read")
            def probe(fn):
                try:
                    return {"ok": True, **fn()}
                except Exception as exc:
                    return {"ok": False, "error": str(exc)}
            return Response(200, envelope(self.con, {
                "schema": db.meta_get(self.con, "schema_version"),
                "dsh": probe(dsh.check_profile),
                "claude": probe(lambda: claude.check(require_auth=False)),
                "models_cached": dsh.catalog_cache_age() is not None,
            }))
        if parts == ["auth", "me"] and method == "GET":
            if identity is None:
                return Response(200, envelope(self.con, {"authenticated": False}))
            data = {"authenticated": True, "kind": identity.kind, "id": identity.subject_id,
                    "authorities": sorted(identity.authorities)}
            if identity.kind == "device":
                row = self.con.execute("SELECT name,created_at,last_seen_at FROM devices WHERE device_id=?", (identity.subject_id,)).fetchone()
                if row:
                    data["device"] = dict(row)
            if identity.kind == "run":
                data["run_id"] = identity.run_id
            return Response(200, envelope(self.con, data))
        if parts == ["auth", "logout"] and method == "POST":
            headers = {"Set-Cookie": auth.cookie_header("", max_age=0)}
            if identity is not None and identity.kind == "device":
                try:
                    auth.revoke_device(self.con, identity.subject_id)
                except auth.AuthError:
                    pass
                db.record_control(self.con, actor=_actor(identity), action="device.logout", outcome="ok", target_type="device", target_id=identity.subject_id)
                self.con.commit()
            return Response(200, envelope(self.con, {"logged_out": True}), headers=headers)
        if parts == ["auth", "devices"] and method == "GET":
            _operator(identity)
            rows = self.con.execute("SELECT device_id,name,created_at,last_seen_at,revoked_at FROM devices ORDER BY created_at").fetchall()
            return Response(200, envelope(self.con, [dict(row) for row in rows]))
        if len(parts) == 4 and parts[:2] == ["auth", "devices"] and parts[3] == "revoke" and method == "POST":
            _operator(identity)
            try:
                changed = auth.revoke_device(self.con, parts[2])
            except auth.AuthError as exc:
                raise Problem(409, str(exc)) from exc
            if not changed:
                raise Problem(404, "device does not exist or is already revoked")
            db.record_control(self.con, actor=_actor(identity), action="device.revoke", outcome="ok", target_type="device", target_id=parts[2]); self.con.commit()
            return Response(200, envelope(self.con, {"revoked": True}))
        if parts == ["auth", "service-tokens"] and method == "GET":
            _operator(identity)
            values = []
            for row in self.con.execute("SELECT token_id,name,authorities_json,created_at,last_seen_at,revoked_at FROM service_tokens ORDER BY created_at"):
                value = dict(row)
                value["authorities"] = json.loads(value.pop("authorities_json") or "[]")
                values.append(value)
            return Response(200, envelope(self.con, values))
        if len(parts) == 4 and parts[:2] == ["auth", "service-tokens"] and parts[3] == "revoke" and method == "POST":
            _operator(identity)
            if not auth.revoke_service_token(self.con, parts[2]):
                raise Problem(404, "service token does not exist or is already revoked")
            db.record_control(self.con, actor=_actor(identity), action="service-token.revoke", outcome="ok", target_type="service", target_id=parts[2]); self.con.commit()
            return Response(200, envelope(self.con, {"revoked": True}))
        if parts == ["auth", "pair"] and method == "POST":
            _operator(identity)
            return Response(201, envelope(self.con, auth.create_pairing(self.con, created_by_device_id=identity.subject_id if identity.kind == "device" else None)))
        if parts == ["auth", "service-tokens"] and method == "POST":
            _operator(identity)
            data = _body(body)
            item, token = auth.create_service_token(self.con, data.get("name", "service"), data.get("authorities"))
            return Response(201, envelope(self.con, {"service": item, "token": token}))
        raise Problem(404, "not found")


USAGE_WINDOWS = {"24h": timedelta(hours=24), "7d": timedelta(days=7)}


def _usage_summary(con, query: dict) -> dict:
    """Raw token totals per provider/model over a window: no prices, no quota."""
    window = str(query.get("window") or "24h")
    if window not in USAGE_WINDOWS:
        raise Problem(400, "window must be one of " + ", ".join(USAGE_WINDOWS))
    since = str(query.get("since") or (datetime.now(timezone.utc) - USAGE_WINDOWS[window]).isoformat())
    # ponytail: observed_at is unindexed; add an index if the table outgrows a 2 s poll.
    rows = con.execute(
        "SELECT provider, model, COUNT(DISTINCT run_id) AS runs, SUM(input_tokens) AS input, SUM(output_tokens) AS output,"
        " SUM(cache_read_tokens) AS cache_read, SUM(cache_write_tokens) AS cache_write, SUM(total_tokens) AS total"
        " FROM usage_events WHERE observed_at>=? GROUP BY provider, model ORDER BY total DESC", (since,)).fetchall()
    routes = [dict(row) for row in rows]
    totals = {key: sum(route[key] for route in routes) for key in ("input", "output", "cache_read", "cache_write", "total")}
    runs = con.execute("SELECT COUNT(DISTINCT run_id) FROM usage_events WHERE observed_at>=?", (since,)).fetchone()[0]
    return {"window": window, "since": since, "runs": runs, "totals": totals, "routes": routes}


def openapi() -> dict:
    return {"openapi": "3.1.0", "info": {"title": "Orchestra-next API", "version": "1"}, "servers": [{"url": "http://127.0.0.1:8766/api"}], "paths": {"/runs": {}, "/messages": {}, "/profiles": {}, "/groups": {}, "/settings": {}, "/scheduler/pause": {}, "/scheduler/resume": {}, "/attention": {}, "/storage": {}}}
