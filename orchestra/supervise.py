"""One durable run, one resident DSH ACP process."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestra import acp, attention, auth, callbacks, claude, config, db, dsh, journal, messaging, paths, runs, worktree
from orchestra.contracts import PERMISSION_WAIT_SECONDS, VERIFICATION_REPAIRS

POLL_SECONDS = 0.25
MAX_VERIFY_OUTPUT = 32_000


def _prompt(run) -> str:
    if run["strategy"] == "ralph":
        return (f"Use the ralph tool now with maxRounds={run['max_rounds']} to execute this "
                f"objective in fresh-context rounds. Do not emulate Ralph yourself. Objective:\n\n{run['objective']}")
    return ("This is the initial human-sourced objective for a durable Orchestra-next run. "
            "Before substantive work, your first tool action MUST create_goal with the exact "
            f"objective below and max_goal_rounds={run['max_rounds']}. Then work autonomously "
            "through same-session goal rounds until update_goal marks it complete.\n\nObjective:\n"
            + run["objective"])


def _resume_prompt(run) -> str:
    if run["strategy"] == "ralph":
        remaining = max(1, int(run["max_rounds"]) - int(run["rounds_started"]))
        return (f"The prior Ralph attempt was checkpointed. Use the ralph tool now with "
                f"maxRounds={remaining}. Incorporate the durable workspace "
                f"and this resolved wait before continuing:\n\n{run['waiting_detail'] or run['objective']}")
    return ("Resume and rearm the existing durable goal. Re-check the workspace and continue it "
            "from the durable session state."
            + ("\n\nResolved wait:\n" + run["waiting_detail"] if run["waiting_detail"] else ""))


def _checkpoint(run_id: int, workdir_path: Path) -> dict:
    before = worktree.status(workdir_path)
    excluded = worktree.untracked_context_paths(workdir_path)
    # The agent owns the tree and can point core.hooksPath at it; hooks must not run as the supervisor.
    git = ["git", "-c", "core.hooksPath=/dev/null", "-C", str(workdir_path)]
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True, text=True)
    if excluded:
        subprocess.run([*git, "reset", "-q", "HEAD", "--", *excluded], check=False, capture_output=True, text=True)
    staged = subprocess.run([*git, "diff", "--cached", "--quiet"])
    if staged.returncode == 1:
        result = subprocess.run([*git, "commit", "-m", f"orchestra-next: checkpoint run {run_id}"], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip()[:1000])
    end_ref = worktree.head(workdir_path)
    stat = subprocess.run([*git, "diff", "--stat", f"{end_ref}^", end_ref], capture_output=True, text=True).stdout.strip()
    return {"before": before[:16_000], "end_ref": end_ref, "diff_stat": stat[:8_000]}


def _verify(run, workdir_path: Path) -> tuple[bool, str]:
    if not run["verify_json"]:
        return True, ""
    value = json.loads(run["verify_json"])
    try:
        result = subprocess.run(value["argv"], cwd=workdir_path, capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=value["timeout_seconds"], shell=False, env=dsh.worker_env())
        output = ((result.stdout or "") + (result.stderr or ""))[-MAX_VERIFY_OUTPUT:]
        return result.returncode == 0, f"exit={result.returncode}\n{output}"
    except subprocess.TimeoutExpired as exc:
        output = ((exc.stdout or "") + (exc.stderr or ""))[-MAX_VERIFY_OUTPUT:]
        return False, f"timed out after {value['timeout_seconds']}s\n{output}"
    except OSError as exc:
        return False, f"verifier could not start: {exc}"


def _permission(run_id: int, method: str, params: dict) -> dict | None:
    if method != "session/request_permission":
        return None
    con = db.connect()
    try:
        expires = (datetime.now(timezone.utc) + timedelta(seconds=PERMISSION_WAIT_SECONDS)).isoformat()
        item = attention.open_request(con, run_id, kind="permission",
            prompt=str((params.get("toolCall") or {}).get("title") or "DSH requests permission"),
            context=params, expires_at=expires)
        deadline = time.monotonic() + PERMISSION_WAIT_SECONDS
        while time.monotonic() < deadline:
            current = attention.get(con, item["attention_id"])
            if current and current["response"]:
                answer = current["response"]["answer"].strip().lower()
                allow = answer in ("allow", "approve", "approved", "yes", "y", "allow once")
                with con:
                    con.execute("UPDATE runs SET status='running',waiting_kind=NULL,waiting_detail=NULL,updated_at=?,revision=revision+1 WHERE id=?", (db.now(), run_id))
                    db.record_control(con, actor=current["response"]["actor"], action="permission.answer", outcome="allowed" if allow else "rejected", target_type="run", target_id=run_id)
                return acp.permission_result(params, allow)
            time.sleep(0.25)
        with con:
            con.execute("UPDATE attention_requests SET status='expired',closed_at=? WHERE attention_id=? AND status='open'", (db.now(), item["attention_id"]))
            db.record_control(con, actor="orchestra", action="permission.timeout", outcome="rejected", target_type="run", target_id=run_id)
        attention.open_request(con, run_id, kind="alert",
            prompt="Permission request timed out and was rejected; the DSH process was checkpointed and released",
            context={"expired_permission": item["attention_id"]})
        return acp.permission_result(params, False)
    finally:
        con.close()


class _Updates:
    def __init__(self, run_id: int):
        self.run_id = run_id
        self.tools: dict[str, str] = {}
        self.ralph_status: str | None = None
        self.ralph_round_base = 0

    def begin_ralph(self, completed_rounds: int) -> None:
        self.ralph_status = None
        self.ralph_round_base = completed_rounds

    def __call__(self, method: str, params: dict) -> None:
        if method != "session/update":
            return
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        call_id = update.get("toolCallId")
        if kind == "tool_call" and call_id:
            self.tools[call_id] = str(update.get("title") or "")
        if kind == "tool_call_update" and self.tools.get(call_id) == "ralph":
            texts = []
            for item in update.get("content") or []:
                content = item.get("content") or {}
                if isinstance(content.get("text"), str):
                    texts.append(content["text"])
            rendered = "\n".join(texts)
            if update.get("status") == "failed":
                self.ralph_status = "failed"
            elif "reported completion" in rendered:
                self.ralph_status = "complete"
            elif "reported a blocker" in rendered:
                self.ralph_status = "blocked"
            elif "reached its" in rendered and "limit" in rendered:
                self.ralph_status = "budget-limited"
            match = re.search(r"after (\d+) rounds?|its (\d+) rounds? limit", rendered)
            if match:
                rounds = self.ralph_round_base + int(match.group(1) or match.group(2))
                own = db.connect()
                try:
                    own.execute("UPDATE runs SET rounds_started=MAX(rounds_started,?) WHERE id=?", (rounds, self.run_id)); own.commit()
                finally:
                    own.close()
        con = db.connect()
        try:
            db.append_event(con, self.run_id, "acp." + str(kind), update)
            con.commit()
        finally:
            con.close()


def _open_failure(con, run_id: int, kind: str, message: str) -> None:
    attention.open_request(con, run_id, kind=kind, prompt=message, context={"run_id": run_id})


def _prepare_worktree(con, run) -> tuple[Path, object]:
    if run["workdir"] and Path(run["workdir"]).is_dir():
        return Path(run["workdir"]), run
    root = Path(run["cwd"])
    group = con.execute("SELECT slug FROM run_groups WHERE group_id=?", (run["group_id"],)).fetchone()
    if run["branch"]:
        location = worktree.restore(root, run["id"], group["slug"], run["branch"])
        start_ref = run["start_ref"] or worktree.head(location)
        branch = run["branch"]
    else:
        location, branch = worktree.create(root, run["id"], group["slug"], run["start_ref"])
        start_ref = worktree.head(location)
    with con:
        con.execute("UPDATE runs SET workdir=?,branch=?,start_ref=?,updated_at=? WHERE id=?", (str(location), branch, start_ref, db.now(), run["id"]))
    return location, runs.find(con, run["id"])


def _finish(con, run_id: int, status: str, workdir_path: Path, *, summary=None, error=None) -> None:
    evidence = _checkpoint(run_id, workdir_path)
    stamp = db.now()
    with con:
        con.execute("UPDATE runs SET status=?,waiting_kind=NULL,waiting_detail=NULL,end_ref=?,git_status=?,git_diff_stat=?,summary=?,error=?,dsh_pid=NULL,finished_at=?,updated_at=?,revision=revision+1 WHERE id=?", (status, evidence["end_ref"], evidence["before"], evidence["diff_stat"], summary, error, stamp, stamp, run_id))
        db.append_event(con, run_id, "run.terminal", {"status": status, "end_ref": evidence["end_ref"]})
        db.record_control(con, actor="orchestra", action="run.finish", outcome=status, target_type="run", target_id=run_id)
    callbacks.emit(config.callback_command(), "run.terminal", {"run_id": run_id, "status": status}, audit_db=con)


def supervise(run_id: int) -> int:
    con = db.connect()
    peer = None
    sidecar = None
    try:
        with con:
            changed = con.execute("UPDATE runs SET status='starting',waiting_kind=NULL,waiting_detail=NULL,started_at=COALESCE(started_at,?),updated_at=?,revision=revision+1 WHERE id=? AND status='queued'", (db.now(), db.now(), run_id))
        if changed.rowcount != 1:
            return 0
        run = runs.find(con, run_id)
        workdir_path, run = _prepare_worktree(con, run)
        if run["strategy"] == "ralph" and int(run["rounds_started"]) >= int(run["max_rounds"]):
            _finish(con, run_id, "timed_out", workdir_path,
                    error="Ralph aggregate round budget was already exhausted")
            return 1
        token = auth.mint_run(con, run_id)
        if claude.required(run["route_provider"]):
            sidecar = claude.start(run_id, workdir_path)
        argv, env = dsh.launch(dict(run), token, sidecar.provider_env if sidecar else None)
        updates = _Updates(run_id)
        if run["strategy"] == "ralph":
            updates.begin_ralph(int(run["rounds_started"]))
        peer = acp.Peer(argv, cwd=workdir_path, env=env,
                        log_path=paths.run_dir(run_id) / "acp.jsonl",
                        on_request=lambda method, params: _permission(run_id, method, params),
                        on_notification=updates)
        peer.start()
        peer.initialize()
        if run["dsh_session_id"]:
            created = peer.resume_session(run["dsh_session_id"], str(workdir_path))
            session_id = run["dsh_session_id"]
        else:
            created = peer.new_session(str(workdir_path))
            session_id = created["sessionId"]
        peer.configure(session_id, run["route_provider"], run["route_model"], run["route_effort"])
        session_root = paths.run_session_dir(run_id)
        with con:
            con.execute("UPDATE runs SET status='running',dsh_session_id=?,dsh_journal_path=?,dsh_pid=?,updated_at=?,revision=revision+1 WHERE id=?", (session_id, str(session_root), peer.pid, db.now(), run_id))
            db.append_event(con, run_id, "run.started", {"session_id": session_id, "pid": peer.pid})
        text = _prompt(run) if not run["dsh_session_id"] else _resume_prompt(run)
        prompt_id = peer.prompt(session_id, text)
        prompt_settled = False
        corrective_sent = bool(run["goal_corrected"])
        last_accounted = time.monotonic()

        while True:
            current = runs.find(con, run_id)
            now_mono = time.monotonic()
            active_delta = now_mono - last_accounted if current["status"] == "running" else 0
            last_accounted = now_mono
            if active_delta:
                with con:
                    con.execute("UPDATE runs SET active_seconds=active_seconds+?,updated_at=? WHERE id=?", (active_delta, db.now(), run_id))
            projection = journal.reconcile(con, run_id, session_root)
            current = runs.find(con, run_id)
            if current["active_seconds"] >= current["active_seconds_limit"] or current["rounds_started"] > current["max_rounds"]:
                peer.cancel(session_id)
                _finish(con, run_id, "timed_out", workdir_path, error="aggregate run budget exhausted")
                return 1
            if current["status"] == "waiting" and current["waiting_kind"] in ("attention", "children"):
                _checkpoint(run_id, workdir_path)
                peer.close()
                with con:
                    con.execute("UPDATE runs SET dsh_pid=NULL,updated_at=? WHERE id=?", (db.now(), run_id))
                return 0
            for message in messaging.claim_pending(con, run_id, safe_boundary=prompt_settled):
                kind = message["kind"]
                if kind == "stop":
                    peer.cancel(session_id)
                    _finish(con, run_id, "stopped", workdir_path, summary=message["body"])
                    return 0
                if kind == "pause":
                    peer.cancel(session_id)
                    _checkpoint(run_id, workdir_path)
                    peer.close()
                    detail = f"paused: {message['body']}" if message["body"] else "paused by operator"
                    with con:
                        con.execute("UPDATE runs SET status='waiting',waiting_kind=NULL,waiting_detail=?,dsh_pid=NULL,updated_at=?,revision=revision+1 WHERE id=?",
                                    (detail, db.now(), run_id))
                        db.append_event(con, run_id, "run.paused", {"reason": message["body"] or None, "by": message["sender"]})
                    return 0
                if kind in ("interrupt", "reroute"):
                    peer.cancel(session_id)
                body = message["body"]
                if kind == "reroute":
                    route = json.loads(body)
                    if claude.required(route["provider"]) and sidecar is None:
                        sidecar = claude.start(run_id, workdir_path)
                        peer.close()
                        argv, env = dsh.launch(dict(current), token, sidecar.provider_env)
                        peer = acp.Peer(argv, cwd=workdir_path, env=env,
                                        log_path=paths.run_dir(run_id) / "acp.jsonl",
                                        on_request=lambda method, params: _permission(run_id, method, params),
                                        on_notification=updates)
                        peer.start()
                        peer.initialize()
                        peer.resume_session(session_id, str(workdir_path))
                        with con:
                            con.execute("UPDATE runs SET dsh_pid=?,updated_at=? WHERE id=?",
                                        (peer.pid, db.now(), run_id))
                    peer.configure(session_id, route["provider"], route["model"], route.get("effort"))
                    with con:
                        con.execute("UPDATE runs SET route_provider=?,route_model=?,route_effort=?,cache_epoch=cache_epoch+1,updated_at=?,revision=revision+1 WHERE id=?", (route["provider"], route["model"], route.get("effort"), db.now(), run_id))
                    body = route.get("body") or "Continue the same goal on the new route. Re-check any interrupted operation before repeating it."
                prompt_id = peer.prompt(session_id, body)
                prompt_settled = False
            if not prompt_settled:
                response = peer.response(prompt_id)
                if response is not None:
                    if "error" in response:
                        raise acp.AcpError(str(response["error"]))
                    prompt_settled = True
                    journal.reconcile(con, run_id, session_root)
                    current = runs.find(con, run_id)
                    if current["strategy"] == "goal" and current["goal_id"] is None:
                        if corrective_sent:
                            _open_failure(con, run_id, "protocol_failure", "DSH did not create the required durable goal after one corrective prompt")
                            peer.close()
                            return 1
                        corrective_sent = True
                        with con:
                            con.execute("UPDATE runs SET goal_corrected=1 WHERE id=?", (run_id,))
                        prompt_id = peer.prompt(session_id, f"Protocol correction: call create_goal now, before any more work, with max_goal_rounds={current['max_rounds']} and the original objective. Then continue the goal.")
                        prompt_settled = False
                    elif current["strategy"] == "ralph" and updates.ralph_status is None:
                        _open_failure(con, run_id, "protocol_failure", "DSH did not return a recognizable Ralph result")
                        peer.close()
                        return 1
            if current["strategy"] == "goal" and current["goal_state"] == "paused" and current["rounds_started"] >= current["max_rounds"]:
                peer.cancel(session_id)
                _finish(con, run_id, "timed_out", workdir_path, error="DSH goal round budget exhausted")
                return 1
            if current["strategy"] == "ralph" and updates.ralph_status in ("blocked", "budget-limited", "failed"):
                _checkpoint(run_id, workdir_path)
                _open_failure(con, run_id, "alert", f"Ralph stopped with status {updates.ralph_status}; its workflow was not replayed")
                peer.close()
                return 1
            completed = (current["strategy"] == "goal" and current["goal_state"] in ("complete", "completed")) or (current["strategy"] == "ralph" and updates.ralph_status == "complete")
            if completed:
                evidence = _checkpoint(run_id, workdir_path)
                with con:
                    con.execute("UPDATE runs SET end_ref=?,git_status=?,git_diff_stat=? WHERE id=?", (evidence["end_ref"], evidence["before"], evidence["diff_stat"], run_id))
                current = runs.find(con, run_id)
                verify_started = time.monotonic()
                ok, verifier_output = _verify(current, workdir_path)
                verify_elapsed = time.monotonic() - verify_started
                with con:
                    con.execute("UPDATE runs SET active_seconds=active_seconds+? WHERE id=?", (verify_elapsed, run_id))
                last_accounted = time.monotonic()
                current = runs.find(con, run_id)
                if current["active_seconds"] >= current["active_seconds_limit"]:
                    _finish(con, run_id, "timed_out", workdir_path, error="aggregate active wall-time budget exhausted during verification")
                    return 1
                if ok:
                    _finish(con, run_id, "completed", workdir_path, summary="DSH work and verification completed")
                    return 0
                repairs = int(current["verification_repairs"])
                if repairs >= VERIFICATION_REPAIRS:
                    _open_failure(con, run_id, "verification", "Verification still fails after two repair cycles",)
                    with con:
                        con.execute("UPDATE runs SET waiting_detail=?,dsh_pid=NULL WHERE id=?", (verifier_output, run_id))
                    peer.close()
                    return 1
                remaining = int(current["max_rounds"]) - int(current["rounds_started"])
                if remaining <= 0:
                    _open_failure(con, run_id, "verification",
                                  "Verification failed and the aggregate round budget is exhausted")
                    with con:
                        con.execute("UPDATE runs SET waiting_detail=?,dsh_pid=NULL WHERE id=?",
                                    (verifier_output, run_id))
                    peer.close()
                    return 1
                with con:
                    con.execute("UPDATE runs SET verification_repairs=verification_repairs+1,goal_state='corrective_pending',updated_at=? WHERE id=?", (db.now(), run_id))
                    db.append_event(con, run_id, "verification.failed", {"repair": repairs + 1, "output": verifier_output})
                if current["strategy"] == "goal":
                    correction = ("Verification failed. Create a new corrective replacement goal in this same session, "
                                  f"with max_goal_rounds={remaining}, then repair the work and complete it. "
                                  f"Bounded verifier output:\n{verifier_output}")
                else:
                    updates.begin_ralph(int(current["rounds_started"]))
                    peer.close()
                    argv, env = dsh.launch(dict(current), token, sidecar.provider_env if sidecar else None)
                    peer = acp.Peer(argv, cwd=workdir_path, env=env,
                                    log_path=paths.run_dir(run_id) / "acp.jsonl",
                                    on_request=lambda method, params: _permission(run_id, method, params),
                                    on_notification=updates)
                    peer.start()
                    peer.initialize()
                    peer.resume_session(session_id, str(workdir_path))
                    peer.configure(session_id, current["route_provider"],
                                   current["route_model"], current["route_effort"])
                    with con:
                        con.execute("UPDATE runs SET dsh_pid=?,updated_at=? WHERE id=?",
                                    (peer.pid, db.now(), run_id))
                    correction = (f"Verification failed. Use the ralph tool now with maxRounds={remaining}. Verifier output:\n{verifier_output}")
                prompt_id = peer.prompt(session_id, correction)
                prompt_settled = False
            if not peer.alive:
                raise acp.AcpError(peer.death_reason or "DSH process exited")
            time.sleep(POLL_SECONDS)
    except Exception as exc:
        run = runs.find(con, run_id)
        if run and run["workdir"]:
            try:
                _checkpoint(run_id, Path(run["workdir"]))
            except Exception:
                pass
        if run and run["strategy"] == "goal" and int(run["resume_count"]) < 1 and run["dsh_session_id"]:
            with con:
                con.execute("UPDATE runs SET status='queued',resume_count=resume_count+1,dsh_pid=NULL,error=?,updated_at=?,revision=revision+1 WHERE id=?", (str(exc)[:2000], db.now(), run_id))
                db.append_event(con, run_id, "run.resume_scheduled", {"error": str(exc)})
            return 1
        if run:
            _open_failure(con, run_id, "alert", f"DSH execution failed: {exc}")
        return 1
    finally:
        try:
            if peer:
                peer.close()
        finally:
            try:
                if sidecar:
                    sidecar.close()
                with con:
                    con.execute("UPDATE runs SET dsh_pid=NULL,updated_at=? WHERE id=?",
                                (db.now(), run_id))
                paths.run_auth_path(run_id).unlink(missing_ok=True)
            except OSError:
                pass
            con.close()


def spawn_supervisor(run_id: int) -> int:
    process = subprocess.Popen([sys.executable, "-m", "orchestra", "supervise", str(run_id)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return process.pid
