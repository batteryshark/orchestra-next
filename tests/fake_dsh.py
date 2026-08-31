#!/usr/bin/env python3
"""Deterministic ACP v1 DSH stand-in. No model or network calls."""
import json
import os
import sys
import time
import uuid
from pathlib import Path


def send(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


if "--version" in sys.argv:
    print("dsh 0.1.2-alpha.3")
    raise SystemExit(0)

root = Path(os.environ["ORCHESTRA_NEXT_DSH_SESSION_ROOT"])
root.mkdir(parents=True, exist_ok=True)
mode = os.environ.get("FAKE_DSH_MODE", "goal")
session_id = None
pending_prompt = None
configured = []
goal_number = 0
next_seq = 0


def journal_path():
    directory = root / "project" / str(session_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "session.jsonl"


def append(event):
    global next_seq
    if event.get("type") != "session":
        event = {**event, "seq": next_seq, "time": next_seq + 2}
        next_seq += 1
    with journal_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":")) + "\n")


def ensure_header():
    global next_seq
    path = journal_path()
    if not path.exists():
        append({"type": "session", "version": 0, "id": session_id,
                "createdAt": 1, "cwd": os.getcwd(), "delegationDepth": 0})
        return
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    next_seq = max((row.get("seq", -1) for row in rows), default=-1) + 1


def complete_goal():
    global goal_number
    goal_number += 1
    goal_id = "goal-" + session_id + "-" + str(goal_number)
    append({"type": "goal/change", "data": {"kind": "goal/change", "version": 1,
        "operation": "create", "goal": {
        "id": goal_id, "revision": 1, "objective": "fixture", "phase": "active",
        "maxGoalRounds": 32}, "roundsStarted": 0, "createdAt": 1, "updatedAt": 1}})
    append({"type": "compaction/start", "data": {"compactionId": "compact-1", "turn": 1}})
    append({"type": "compaction/end", "data": {"compactionId": "compact-1", "turn": 1}})
    append({"type": "assistant/message", "data": {"turn": 1, "step": 1,
        "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}],
                    "source": {"kind": "model", "provider": "fake", "model": "model"}},
        "usage": {"inputTokens": 20, "outputTokens": 5, "cacheReadTokens": 10,
                  "cacheWriteTokens": 2, "totalTokens": 37}}})
    append({"type": "goal/change", "data": {"kind": "goal/change", "version": 1,
        "operation": "complete", "goal": {
        "id": goal_id, "revision": 2, "objective": "fixture", "phase": "complete",
        "maxGoalRounds": 32}, "roundsStarted": 1, "createdAt": 1, "updatedAt": 2}})
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session_id,
        "update": {"sessionUpdate": "tool_call", "toolCallId": "goal-create", "title": "create_goal", "status": "in_progress"}}})
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session_id,
        "update": {"sessionUpdate": "tool_call_update", "toolCallId": "goal-create", "status": "completed"}}})


for line in sys.stdin:
    try:
        frame = json.loads(line)
    except ValueError:
        continue
    method = frame.get("method")
    request_id = frame.get("id")
    params = frame.get("params") or {}
    if method == "initialize":
        if mode == "malformed":
            print("not-json", flush=True)
            continue
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": 1, "agentInfo": {"name": "fake-dsh", "version": "0.1.2-alpha.3"},
            "agentCapabilities": {"promptCapabilities": {"image": False},
                "sessionCapabilities": {"close": {}, "list": {}, "resume": {}}}, "authMethods": []}})
    elif method in ("session/new", "session/resume"):
        session_id = params.get("sessionId") or str(uuid.uuid4())
        ensure_header()
        options = [{"id": "model", "type": "select", "currentValue": "[\"fake\",\"model\"]",
                    "options": [{"name": "Fake Model", "value": "[\"fake\",\"model\"]"}]}]
        result = {"configOptions": options}
        if method == "session/new":
            result["sessionId"] = session_id
        send({"jsonrpc": "2.0", "id": request_id, "result": result})
    elif method == "session/set_config_option":
        configured.append((params.get("configId"), params.get("value")))
        options = [{"id": "model", "type": "select", "currentValue": params.get("value"),
                    "options": [{"name": "Fake Model", "value": "[\"fake\",\"model\"]"},
                                {"name": "Other", "value": "[\"fake\",\"other\"]"}]},
                   {"id": "reasoning_effort", "type": "select", "currentValue": "low",
                    "options": [{"name": "Low", "value": "low"}, {"name": "High", "value": "high"}]}]
        send({"jsonrpc": "2.0", "id": request_id, "result": {"configOptions": options}})
    elif method == "session/prompt":
        if mode == "crash-once" and not (root / ".crashed").exists():
            (root / ".crashed").touch()
            raise SystemExit(7)
        if mode == "permission":
            pending_prompt = request_id
            send({"jsonrpc": "2.0", "id": 9001, "method": "session/request_permission", "params": {
                "sessionId": session_id, "toolCall": {"toolCallId": "shell", "title": "run command"},
                "options": [{"optionId": "yes", "name": "Allow", "kind": "allow_once"},
                            {"optionId": "no", "name": "Reject", "kind": "reject_once"}]}})
            continue
        if mode == "hang":
            pending_prompt = request_id
            continue
        if mode in ("ralph", "ralph-blocked", "ralph-budget"):
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session_id,
                "update": {"sessionUpdate": "tool_call", "toolCallId": "ralph", "title": "ralph", "status": "in_progress"}}})
            rendered = ("Ralph worker reported completion after 2 rounds." if mode == "ralph" else
                        "Ralph worker reported a blocker after 2 rounds." if mode == "ralph-blocked" else
                        "Ralph reached its 8 rounds limit; the worker reported work remaining.")
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session_id,
                "update": {"sessionUpdate": "tool_call_update", "toolCallId": "ralph", "status": "completed",
                           "content": [{"type": "content", "content": {"type": "text", "text": rendered}}]}}})
        else:
            complete_goal()
        send({"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "end_turn"}})
    elif request_id == 9001 and pending_prompt is not None:
        complete_goal()
        send({"jsonrpc": "2.0", "id": pending_prompt, "result": {"stopReason": "end_turn"}})
        pending_prompt = None
    elif method == "session/cancel":
        if pending_prompt is not None:
            send({"jsonrpc": "2.0", "id": pending_prompt, "result": {"stopReason": "cancelled"}})
            pending_prompt = None
    elif method == "session/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"sessions": []}})
    elif method == "session/close":
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
