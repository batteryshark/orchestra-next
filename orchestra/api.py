"""Small versioned JSON API for the standalone V3 kernel."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from orchestra import artifacts, attention, auth, child_runs, db, dsh, groups, messaging, profiles, runs, storage, worktree
from orchestra.contracts import ContractError, RunRequest

PREFIX = "/api/v3"


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
    return {"version": 3, "instance_id": db.instance_id(con), "board_revision": db.board_revision(con), "data": data}


def _actor(identity) -> str:
    return f"{identity.kind}:{identity.subject_id}"


def _need(identity, authority: str, target=None):
    try:
        auth.authorize(identity, authority, target_run_id=target)
    except auth.AuthError as exc:
        raise Problem(401 if identity is None else 403, str(exc)) from exc


def _operator(identity) -> None:
    _need(identity, "read")
    if identity.kind != "device":
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


class API:
    def __init__(self, con):
        self.con = con

    def handle(self, method: str, path: str, query: dict, body, identity) -> Response | FileResponse:
        if path == PREFIX + "/health" and method == "GET":
            return Response(200, envelope(self.con, {"status": "ok", "schema": "v3"}))
        if path == PREFIX + "/openapi.json" and method == "GET":
            return Response(200, openapi())
        if path == PREFIX + "/auth/bootstrap" and method == "POST":
            try:
                device, token = auth.bootstrap_device(self.con, _body(body).get("name", "First device"))
            except auth.AuthError as exc:
                raise Problem(409, str(exc)) from exc
            return Response(201, envelope(self.con, {"device": device, "token": token}))
        if path == PREFIX + "/auth/pair/redeem" and method == "POST":
            data = _body(body)
            device, token = auth.redeem_pairing(self.con, data.get("pairing_id", ""), data.get("code", ""), data.get("name", ""))
            return Response(201, envelope(self.con, {"device": device, "token": token}))
        if not path.startswith(PREFIX + "/"):
            raise Problem(404, "not found")
        parts = path[len(PREFIX) + 1:].split("/")

        if parts == ["runs"]:
            if method == "GET":
                _need(identity, "read")
                after = int(query.get("after", 0) or 0)
                rows = self.con.execute("SELECT * FROM runs WHERE id>? ORDER BY id LIMIT 200", (after,)).fetchall()
                return Response(200, envelope(self.con, [runs.payload(row) for row in rows]))
            if method == "POST":
                _need(identity, "dispatch")
                try:
                    request = RunRequest.from_mapping(_body(body))
                    run, created = runs.submit(self.con, request)
                except (ContractError, ValueError) as exc:
                    raise Problem(400, str(exc)) from exc
                return Response(201 if created else 200, envelope(self.con, runs.payload(run, detail=True)))

        if len(parts) >= 2 and parts[0] == "runs":
            run_id = _id(parts[1])
            run = runs.find(self.con, run_id)
            if run is None:
                raise Problem(404, f"run {run_id} does not exist")
            suffix = parts[2:]
            if not suffix and method == "GET":
                _need(identity, "read", target=run_id)
                return Response(200, envelope(self.con, runs.payload(run, detail=True)))
            if suffix == ["events"] and method == "GET":
                _need(identity, "read", target=run_id)
                after = int(query.get("after", 0) or 0)
                values = []
                for row in self.con.execute("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT 500", (run_id, after)):
                    value = dict(row); value["payload"] = json.loads(value.pop("payload_json")); values.append(value)
                return Response(200, envelope(self.con, values))
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
            if suffix in (["tell"], ["interrupt"], ["stop"] ) and method == "POST":
                kind = suffix[0]
                authority = "stop" if kind == "stop" else "resume"
                _need(identity, authority, target=run_id)
                data = _body(body)
                item = messaging.queue(self.con, run_id, kind=kind, body=str(data.get("message") or data.get("reason") or ""), sender=_actor(identity))
                return Response(202, envelope(self.con, item))
            if suffix == ["reroute"] and method == "POST":
                _need(identity, "reroute", target=run_id)
                data = _body(body)
                provider, model, effort = data.get("provider"), data.get("model"), data.get("effort")
                catalog = dsh.catalog(run["workdir"] or run["cwd"])
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
            if suffix == ["changes"] and method == "GET":
                _need(identity, "read", target=run_id)
                if not run["workdir"]:
                    return Response(200, envelope(self.con, {"status": "", "diff": ""}))
                import subprocess
                status = worktree.status(Path(run["workdir"]))
                diff = subprocess.run(["git", "-C", run["workdir"], "diff", run["start_ref"] or "HEAD"], capture_output=True, text=True).stdout
                return Response(200, envelope(self.con, {"status": status, "diff": diff[:200_000]}))
            if suffix == ["merge"] and method == "POST":
                _operator(identity)
                result = worktree.merge_into_owner(Path(run["cwd"]), run["branch"])
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
                row = profiles.update(self.con, parts[1], expected_revision=revision,
                                      actor=_actor(identity), **data)
            except (KeyError, TypeError, ValueError) as exc:
                raise Problem(400, "expected_revision and valid profile fields are required") from exc
            except RuntimeError as exc:
                raise Problem(409, str(exc)) from exc
            return Response(200, envelope(self.con, profiles.payload(row)))

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
            if identity and identity.kind == "device":
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
            after = int(query.get("after", 0) or 0)
            values = []
            for row in self.con.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT 500", (after,)):
                value = dict(row)
                value["payload"] = json.loads(value.pop("payload_json"))
                values.append(value)
            return Response(200, envelope(self.con, values))
        if parts == ["usage"] and method == "GET":
            _need(identity, "read")
            after = int(query.get("after", 0) or 0)
            rows = self.con.execute("SELECT * FROM usage_events WHERE id>? ORDER BY id LIMIT 500", (after,))
            return Response(200, envelope(self.con, [dict(row) for row in rows]))
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
            path, metadata = item
            return FileResponse(200, path, metadata["media_type"])
        if parts == ["auth", "pair"] and method == "POST":
            _operator(identity)
            return Response(201, envelope(self.con, auth.create_pairing(self.con, created_by_device_id=identity.subject_id if identity.kind == "device" else None)))
        if parts == ["auth", "service-tokens"] and method == "POST":
            _operator(identity)
            data = _body(body)
            item, token = auth.create_service_token(self.con, data.get("name", "service"), data.get("authorities"))
            return Response(201, envelope(self.con, {"service": item, "token": token}))
        raise Problem(404, "not found")


def openapi() -> dict:
    return {"openapi": "3.1.0", "info": {"title": "Orchestra-next API", "version": "3.0.0a1"}, "servers": [{"url": "http://127.0.0.1:8766/api/v3"}], "paths": {"/runs": {}, "/profiles": {}, "/groups": {}, "/attention": {}, "/storage": {}}}
