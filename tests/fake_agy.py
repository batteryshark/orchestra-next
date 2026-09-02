#!/usr/bin/env python3
"""Deterministic `agy` stand-in. No model, keyring, or network access."""
import json
import os
import sys
import time
import uuid

MODELS = ("gemini-3.8-flash-low", "gemini-3.1-pro-high", "claude-opus-4-6-thinking")
argv = sys.argv[1:]

if argv == ["models"]:
    print("Fetching available models...")
    for model in MODELS:
        print(f"{model}\tDisplay {model}")
    raise SystemExit(0)

if "--sandbox" not in argv or "--dangerously-skip-permissions" in argv or "--print" not in argv:
    raise SystemExit(99)


def option(name):
    return argv[argv.index(name) + 1] if name in argv else None


mode = os.environ.get("FAKE_AGY_MODE", "ok")
if mode == "hang":
    time.sleep(30)
state_path = os.environ["FAKE_AGY_STATE"]
try:
    with open(state_path, encoding="utf-8") as handle:
        state = json.load(handle)
except (OSError, ValueError):
    state = {}
conversation = option("--conversation") or str(uuid.uuid4())
turns = int(state.get(conversation, 0)) + 1
state[conversation] = turns
with open(state_path, "w", encoding="utf-8") as handle:
    json.dump(state, handle)
# Cumulative usage grows per turn within one conversation, like the real CLI.
usage = {"input_tokens": 1000 * turns, "output_tokens": 10 * turns, "thinking_tokens": 0,
         "cache_read_tokens": 0, "total_tokens": 1010 * turns}
status = "ERROR" if mode == "error" else "SUCCESS"
print(json.dumps({"conversation_id": conversation, "status": status,
                  "response": f"turn {turns} review of {os.getcwd()} with {option('--model')}",
                  "duration_seconds": 1.5, "num_turns": turns, "usage": usage}))
raise SystemExit(0 if status == "SUCCESS" else 1)
