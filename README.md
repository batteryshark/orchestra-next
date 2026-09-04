# Orchestra-next

Orchestra-next is a local durable-run service for [DeepSeek Harness (DSH)](https://github.com/deepseek-ai/deepseek-harness). It keeps Orchestra's fleet mechanics—groups, dependencies, isolated Git worktrees, children, attention, artifacts, controls, callbacks, raw usage, and event feeds—but has one execution path:

```text
API/CLI → durable run → resident per-run DSH ACP process → configured model
```

This is an experimental fork. It does not read `~/.orchestra`, does not expose the `orchestra` command, and does not import V2 state.

## Requirements

- Python 3.11 or newer
- DSH exactly `0.1.2-alpha.3`
- Git
- Node/npm and the official authenticated `claude` CLI only for the optional
  `claude-subscription` route

There are no Python runtime dependencies.

## Setup

```sh
pip install -e .
orchestra-next init
orchestra-next dsh setup
orchestra-next dsh check --capabilities

# Optional private Claude subscription route
orchestra-next claude setup
orchestra-next claude check
```

`dsh setup` installs the repository's `orchestra-next` profile under the existing DSH home. It does not change DSH credentials, provider settings, or endpoints. `dsh check` reports native DeepSeek search as unavailable when it cannot detect `DEEPSEEK_API_KEY`; it never displays the value. The profile uses uncompressed per-run JSONL, durable checkpoints, no outbound telemetry, native DSH web search/fetch, goals, compaction, jobs, skills, todo, and the normal shell/filesystem tools. Normal subagents, workflows, Ralph, plan mode, and native user elicitation are disabled. An explicit Ralph run loads the small `ralph.patch.yml` overlay.

`claude setup` reproducibly installs the lockfile-pinned local sidecar
(`@openchamber/opencode-claude` 0.14.0 and Bun 1.4.0) beneath Orchestra-next
state. It does not read, copy, or
change Claude credentials. `claude check` asks the official CLI for login status
without returning account data. A Claude run gets its own authenticated
loopback listener on an OS-assigned port, persistent session binding, and
sidecar process; all are released with the run supervisor.

Configure a route from DSH's live ACP catalog:

```sh
orchestra-next daemon

# in another terminal
orchestra-next profile-create "DeepSeek worker" deepseek-official deepseek-v4-flash \
  --slug deepseek --tier 2

orchestra-next run deepseek "Implement and verify the requested change" \
  --cwd /path/to/repository \
  --verify python -m unittest

# The optional sidecar looks like any other DSH route.
orchestra-next profile-create "Claude worker" claude-subscription sonnet \
  --slug claude --tier 2
```

The subscription sidecar is for private evaluation. Anthropic's current Agent
SDK documentation requires prior approval before a third-party product offers
claude.ai login or subscription rate limits; use an API-key route for public
deployment unless that approval exists.

The daemon listens on `127.0.0.1:8766`; the API prefix is `/api`. The operator console is served at `/`. State lives at `~/.orchestra-next`. Override these with the non-secret bootstrap file or `ORCHESTRA_NEXT_HOME`/`ORCHESTRA_NEXT_URL`. Browsers pair with a code from `orchestra-next pair`; on a tailnet you can instead set `"trust_tailnet": true` in the bootstrap file and skip pairing entirely (see docs/API.md).

The console is three static files under `orchestra/ui/` with no build step. It covers fleet status and dispatch, run activity with tell, interrupt, pause, resume, and stop, the attention inbox, Git changes, raw token usage, and run evidence. Keyboard: `/` filter, `j`/`k` move, `Enter` open, `t` direct a run, `1`–`5` run sections, `Esc` back.

## Run as a service

On macOS, `orchestra-next service` manages a per-user launchd LaunchAgent (`local.orchestra-next.daemon`) that runs `python -m orchestra daemon` at login and restarts it if it exits:

```sh
orchestra-next service install --start   # write the plist, load it, start now
orchestra-next service status            # JSON: installed, loaded, state, pid
orchestra-next service restart           # launchctl kickstart -k
orchestra-next service uninstall         # bootout and remove the plist
```

The plist lives at `~/Library/LaunchAgents/local.orchestra-next.daemon.plist`. It runs the interpreter that ran `install`, from the repo root, with your current `PATH` so `dsh`, `agy`, `claude`, and `codex` resolve. Both streams log to `~/.orchestra-next/logs/daemon.log`. `install` refuses to run while a foreground `orchestra-next daemon` is running. Other platforms exit 1.

## Run behavior

A goal run must create a DSH goal before substantive work. DSH drives continuation rounds inside the same process and session. Orchestra-next reconciles the session JSONL by `(session_id, seq)`, including goal state, rounds, messages, tool lifecycle, compaction, and exact input/output/cache token facts.

Transient process failure gets one same-session resume. Semantic questions and child waits checkpoint the worktree, stop DSH, release capacity, and later resume the same session. Permission requests park the exact ACP process for up to ten minutes while releasing scheduler capacity. Rerouting cancels current activity, changes the ACP model configuration, increments the cache epoch, and continues the same goal.

Completion checkpoints Git evidence and runs the optional verifier as a direct argv. A failed verifier receives up to two repair cycles in the same DSH session; continued failure opens attention instead of marking the run complete.

Worker-facing bridge commands infer the current run from their environment:

```sh
orchestra-next ask "Which deployment target should I use?"
orchestra-next child deepseek "Investigate the failing subsystem"
orchestra-next artifact reports/result.json
```

Worker authentication is stored in a run-owned `0600` file. The bearer token is never placed directly in DSH's subprocess environment.

Claude sidecar HTTP authentication uses a separate short-lived bearer. The
sidecar reads it from another run-owned `0600` file; DSH receives only that
ephemeral provider credential. A stable run header prevents identical prompts
in different runs from sharing Claude sessions.

## Tests

```sh
python run_tests.py
```

The suite uses a deterministic fake ACP server and makes no provider or network calls. Paid-provider evaluation is intentionally separate; compare completion rate, wall time, cache reads/writes, compaction, attention, and restart recovery against V2 before moving Workbridge.

See [DESIGN.md](DESIGN.md) for ownership boundaries,
[docs/API.md](docs/API.md) for the public surface,
[docs/CONSOLE.md](docs/CONSOLE.md) for the operator console maintainer notes, and
[docs/UI_PROPOSAL.md](docs/UI_PROPOSAL.md) for the proposed operator interface.
The [Antigravity delegation spike](docs/ANTIGRAVITY_SPIKE.md) records the
evaluated official-CLI integration boundary.
