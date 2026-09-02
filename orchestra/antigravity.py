"""Bounded read-only delegation to Google's official Antigravity CLI (`agy`).

The CLI owns authentication in the OS keyring; Orchestra only invokes the
documented binary. See docs/ANTIGRAVITY_SPIKE.md for the policy boundary.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time

from orchestra import dsh

RESPONSE_STDOUT_LIMIT = 32_000
RESPONSE_STORED_LIMIT = 8_000
STDERR_LIMIT = 2_000
CATALOG_TIMEOUT = 60
PRINT_TIMEOUT = "10m"
PROCESS_TIMEOUT = 660
SCRUBBED = ("GEMINI_API_KEY", "GOOGLE_API_KEY")  # a subscription route must never become API billing
PROMPT_PREFIX = ("You are reviewing the repository in the current working directory for another agent. "
                 "Do not modify any file. Answer with findings and recommendations only.\n\n")


def executable() -> str:
    return os.environ.get("ORCHESTRA_NEXT_AGY", "agy")


def parse_catalog(text: str) -> list[str]:
    """`agy models` prints `<id>\\t<name>` rows after a progress line; lines without a tab are noise."""
    return [line.split("\t", 1)[0].strip() for line in text.splitlines() if "\t" in line and line.split("\t", 1)[0].strip()]


def catalog() -> list[str]:
    try:
        result = subprocess.run([executable(), "models"], capture_output=True, text=True, timeout=CATALOG_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"cannot list Antigravity models: {exc}") from exc
    ids = parse_catalog(result.stdout)
    if result.returncode != 0 or not ids:
        raise ValueError(f"cannot list Antigravity models: {(result.stderr or result.stdout).strip()[:300] or 'no output'}")
    return ids


_CATALOG_LOCK = threading.Lock()
_CATALOG: tuple[float, list[str]] | None = None
CATALOG_TTL = 300.0


def cached_catalog(*, refresh: bool = False) -> list[str]:
    """`agy models` spawns a process; serve the id list from a short cache."""
    global _CATALOG
    with _CATALOG_LOCK:
        if not refresh and _CATALOG is not None and time.monotonic() - _CATALOG[0] < CATALOG_TTL:
            return _CATALOG[1]
        _CATALOG = (time.monotonic(), catalog())
        return _CATALOG[1]


def build_argv(objective: str, model: str, conversation_id: str | None = None, workdir: str | None = None) -> list[str]:
    argv = [executable(), "--print", PROMPT_PREFIX + objective, "--model", model, "--output-format", "json",
            "--sandbox", "--disable-slash-commands", "--print-timeout", PRINT_TIMEOUT]
    if workdir:
        # The sandbox does not expose the cwd by itself; --add-dir mounts the worktree read paths.
        argv += ["--add-dir", workdir]
    if conversation_id:
        argv += ["--conversation", conversation_id]
    return argv


def build_env(base=None) -> dict[str, str]:
    env = dsh.worker_env(base)
    for name in SCRUBBED:
        env.pop(name, None)
    return env


def parse_output(stdout: str) -> dict | None:
    """The last non-empty stdout line that parses as a JSON object."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def record(objective: str, model: str, parsed: dict | None, *, status: str | None = None, stderr: str = "", error: str | None = None) -> dict:
    """Shape the CLI result into the delegation body posted to the server."""
    parsed = parsed or {}
    usage = parsed.get("usage") or {}
    response = parsed.get("response")
    response = response if isinstance(response, str) else None
    # Headless agy auto-denies tools it cannot prompt for and still reports SUCCESS with an empty response.
    if status is None and error is None and parsed.get("status") == "SUCCESS" and not (response or "").strip() and "auto-denied" in stderr:
        status, error = "DENIED", stderr.strip().splitlines()[-1][:STDERR_LIMIT]
    return {
        "model": model, "mode": "review", "objective": objective,
        "conversation_id": parsed.get("conversation_id") if isinstance(parsed.get("conversation_id"), str) else None,
        "status": status or (parsed.get("status") if isinstance(parsed.get("status"), str) else None) or "ERROR",
        "num_turns": _int(parsed.get("num_turns")),
        "duration_seconds": parsed.get("duration_seconds") if isinstance(parsed.get("duration_seconds"), (int, float)) else None,
        "usage": {"input": _int(usage.get("input_tokens")), "output": _int(usage.get("output_tokens")), "thinking": _int(usage.get("thinking_tokens")),
                  "cache_read": _int(usage.get("cache_read_tokens")), "total": _int(usage.get("total_tokens"))},
        "response": None if response is None else response[:RESPONSE_STDOUT_LIMIT],
        "truncated": response is not None and len(response) > RESPONSE_STDOUT_LIMIT,
        "error": None if error is None else error[:STDERR_LIMIT],
        "stderr": stderr[-STDERR_LIMIT:] or None,
    }


def delegate(objective: str, model: str, workdir: str, conversation_id: str | None = None) -> dict:
    argv = build_argv(objective, model, conversation_id, workdir)
    try:
        result = subprocess.run(argv, cwd=workdir, env=build_env(), shell=False, capture_output=True, text=True,
                                timeout=PROCESS_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr or ""
        return record(objective, model, parse_output(stdout), status="TIMEOUT", stderr=stderr, error=f"agy did not finish within {PROCESS_TIMEOUT} seconds")
    except OSError as exc:
        return record(objective, model, None, status="ERROR", error=f"cannot start agy: {exc}")
    parsed = parse_output(result.stdout)
    error = None
    if parsed is None:
        error = "agy printed no JSON result"
    elif result.returncode != 0:
        error = f"agy exited {result.returncode}"
    status = None if parsed and result.returncode == 0 else "ERROR"
    return record(objective, model, parsed, status=status, stderr=result.stderr, error=error)
