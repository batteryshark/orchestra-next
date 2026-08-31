"""Minimal ACP v1 client for one resident DSH process."""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

from orchestra import db

PROTOCOL_VERSION = 1
HANDSHAKE_TIMEOUT = 60.0


class AcpError(RuntimeError):
    pass


class Peer:
    def __init__(self, cmd, *, cwd, env, log_path, on_request=None, on_notification=None):
        self.cmd, self.cwd, self.env = list(cmd), str(cwd), env
        self.log_path = Path(log_path)
        self.on_request, self.on_notification = on_request, on_notification
        self.proc: subprocess.Popen | None = None
        self.dead = False
        self.death_reason: str | None = None
        self._next_id = 0
        self._responses: dict[int, dict] = {}
        self._methods: dict[int, str] = {}
        self._write_lock = threading.Lock()
        self._state = threading.Condition()
        self._log_lock = threading.Lock()
        self._readers: list[threading.Thread] = []

    def start(self) -> None:
        self.log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.cwd, env=self.env, text=True, encoding="utf-8", errors="replace", bufsize=1, start_new_session=True)
        except OSError as exc:
            raise AcpError(f"cannot start DSH: {exc}") from exc
        self._readers = [threading.Thread(target=self._read_stdout, daemon=True), threading.Thread(target=self._read_stderr, daemon=True)]
        for reader in self._readers:
            reader.start()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and not self.dead

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def close(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        for reader in self._readers:
            reader.join(timeout=2)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()

    def _append(self, value: str) -> None:
        with self._log_lock:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(value.rstrip("\n") + "\n")

    def _log(self, frame: dict, direction: str, method=None) -> None:
        self._append(json.dumps({"_dir": direction, "_ts": db.now(), **frame, **({"_method": method} if method else {})}, ensure_ascii=False, default=str))

    def _read_stdout(self) -> None:
        try:
            assert self.proc and self.proc.stdout
            for raw in self.proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError as exc:
                    self._append(raw)
                    raise AcpError(f"DSH emitted malformed ACP JSON: {exc}") from exc
                if not isinstance(frame, dict):
                    raise AcpError("DSH emitted a non-object ACP frame")
                self._dispatch(frame)
        except (OSError, AcpError) as exc:
            self.death_reason = str(exc)
        finally:
            self.dead = True
            with self._state:
                self._state.notify_all()

    def _read_stderr(self) -> None:
        try:
            assert self.proc and self.proc.stderr
            for line in self.proc.stderr:
                if line.strip():
                    self._append(json.dumps({"_dir": "stderr", "_ts": db.now(), "text": line.rstrip()}, ensure_ascii=False))
        except OSError:
            pass

    def _dispatch(self, frame: dict) -> None:
        if "id" in frame and ("result" in frame or "error" in frame):
            with self._state:
                method = self._methods.pop(frame["id"], None)
                self._log(frame, "in", method)
                self._responses[frame["id"]] = frame
                self._state.notify_all()
            return
        method, params = frame.get("method"), frame.get("params") or {}
        if not isinstance(method, str) or not isinstance(params, dict):
            raise AcpError("DSH emitted a malformed ACP method frame")
        self._log(frame, "in")
        if "id" in frame:
            self._answer(frame["id"], method, params)
        elif self.on_notification:
            self.on_notification(method, params)

    def _answer(self, request_id, method: str, params: dict) -> None:
        try:
            result = self.on_request(method, params) if self.on_request else None
            if result is None:
                self._send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"unsupported client method: {method}"}}, method)
            else:
                self._send({"jsonrpc": "2.0", "id": request_id, "result": result}, method)
        except Exception as exc:
            self._send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(exc)[:500]}}, method)

    def _send(self, frame: dict, method=None) -> None:
        encoded = json.dumps(frame, ensure_ascii=False, default=str)
        with self._write_lock:
            if not self.proc or not self.proc.stdin or self.proc.poll() is not None:
                raise AcpError(self.death_reason or "DSH ACP process is not running")
            self.proc.stdin.write(encoded + "\n")
            self.proc.stdin.flush()
        self._log(frame, "out", method)

    def request(self, method: str, params: dict) -> int:
        with self._state:
            self._next_id += 1
            request_id = self._next_id
            self._methods[request_id] = method
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return request_id

    def response(self, request_id: int) -> dict | None:
        with self._state:
            frame = self._responses.pop(request_id, None)
        if frame is None and not self.alive:
            raise AcpError(self.death_reason or "DSH ACP process exited")
        return frame

    def call(self, method: str, params: dict, timeout=HANDSHAKE_TIMEOUT):
        request_id = self.request(method, params)
        deadline = time.monotonic() + timeout
        with self._state:
            while request_id not in self._responses and self.alive:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AcpError(f"{method} timed out after {timeout:g}s")
                self._state.wait(min(remaining, 0.2))
            frame = self._responses.pop(request_id, None)
        if frame is None:
            raise AcpError(self.death_reason or "DSH ACP process exited")
        if "error" in frame:
            error = frame.get("error") or {}
            raise AcpError(f"{method}: {error.get('message') or error}")
        return frame.get("result")

    def initialize(self) -> dict:
        result = self.call("initialize", {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": {}})
        capabilities = (result or {}).get("agentCapabilities") or {}
        sessions = capabilities.get("sessionCapabilities") or {}
        missing = [name for name in ("close", "list", "resume") if name not in sessions]
        if (result or {}).get("protocolVersion") != PROTOCOL_VERSION or missing:
            raise AcpError("DSH ACP capability mismatch" + (": missing session " + ", ".join(missing) if missing else ""))
        return result

    def new_session(self, cwd: str) -> dict:
        return self.call("session/new", {"cwd": str(Path(cwd).resolve()), "mcpServers": []})

    def resume_session(self, session_id: str, cwd: str) -> dict:
        return self.call("session/resume", {"sessionId": session_id, "cwd": str(Path(cwd).resolve()), "mcpServers": []})

    def configure(self, session_id: str, provider: str, model: str, effort: str | None) -> None:
        self.call("session/set_config_option", {"sessionId": session_id, "configId": "model", "value": json.dumps([provider, model], separators=(",", ":"))})
        if effort:
            self.call("session/set_config_option", {"sessionId": session_id, "configId": "reasoning_effort", "value": effort})

    def prompt(self, session_id: str, text: str) -> int:
        return self.request("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]})

    def cancel(self, session_id: str) -> None:
        self._send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session_id}})


def permission_result(params: dict, allow: bool) -> dict:
    wanted = ("allow_once", "allow_always") if allow else ("reject_once", "reject_always")
    options = [item for item in params.get("options", []) if isinstance(item, dict)]
    for kind in wanted:
        for option in options:
            if option.get("kind") == kind:
                return {"outcome": {"outcome": "selected", "optionId": option.get("optionId")}}
    return {"outcome": {"outcome": "cancelled"}}
