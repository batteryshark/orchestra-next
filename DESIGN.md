# Orchestra-next architecture

## Boundary

Orchestra-next schedules durable work. DSH performs model interaction.

Orchestra owns run state, FIFO/dependency admission, concurrency, Git worktrees and evidence, external waits, permissions, verification, children, artifacts, controls, authentication, callbacks, storage, and recovery. DSH owns the model request loop, tools, cache-aware compaction, pruning, persisted goals, same-session goal rounds, and explicit Ralph workflows.

There is no runtime abstraction or adapter matrix. `orchestra.dsh.launch` is the only DSH process builder and `orchestra.acp.Peer` is the only Orchestra-to-model transport. The optional Claude subscription route is a per-run loopback sidecar that presents a standard OpenAI-compatible provider to DSH; no Claude behavior enters the durable core.

## Durable state

The fresh SQLite store refuses every other schema. A run freezes its request and route profile but leaves provider credentials, endpoints, retention settings, and the live model catalog in DSH.

Each run has one root DSH session and a private directory:

```text
~/.orchestra-next/runs/<id>/
  acp.jsonl
  worker-auth
  claude-proxy-auth       # present only while a Claude sidecar is active
  claude-sidecar-data/    # per-run Claude session binding
  dsh-session/
```

The DSH journal is authoritative for model activity. Projection is idempotent on `(run_id, session_id, source_seq)`. Raw usage events retain provider/model, descendant identity, cache epoch, and input/output/cache-read/cache-write/total tokens. Orchestra-next contains no prices, quotas, estimates, or admission holds based on spend.

## Lifecycle

```text
queued → starting → running ↔ waiting → terminal
```

Waiting kinds are `attention`, `children`, `permission`, and `verification`. Attention and child waits checkpoint and stop DSH. Permission waits keep the exact process parked for ten minutes. Both release scheduler capacity because waiting runs are not counted as active.

Goal runs default to 32 rounds (maximum 128). Ralph defaults to 8 fresh-context rounds (maximum 32). Active processing defaults to two hours (maximum eight). All verifier repairs share those aggregate limits.

A new goal run's first prompt requires `create_goal`. One protocol correction is allowed. Missing it twice opens protocol-failure attention. A completed goal begins verification. A verifier failure creates a replacement corrective goal because completed DSH goals are immutable. After two repair cycles, the run waits for attention.

A goal process may resume once after a transient failure and receives an explicit goal-rearm prompt. Ralph interruption is not replayed automatically because its in-loop state is not resumable.

## Route and cache identity

Profiles contain only provider, model, optional effort, tier, capacity, lifecycle flags, note, and revision. Creation validates the route against live ACP config options.

`reroute` cancels current activity, sets `model` and `reasoning_effort` through ACP, increments `cache_epoch`, and appends direction to the same session. A new epoch is explicit evidence that provider cache continuity was intentionally broken.

Selecting `claude-subscription` lazily starts one authenticated sidecar on an
OS-assigned loopback port. DSH sees only its ordinary `openai-completions`
surface. DSH tools cross the sidecar as MCP park/resume calls, while a stable
run header and run-private binding store preserve the official Claude CLI
session across sidecar restarts. Rerouting into Claude restarts and resumes DSH
once so the process receives the new ephemeral endpoint; rerouting away leaves
the already-owned sidecar alive until normal supervisor cleanup.

## Safety

- The normal DSH profile disables telemetry, native elicitation, plan mode, subagents, workflows, and Ralph.
- Ralph capabilities exist only in the explicit overlay.
- Verifiers use `subprocess.run(argv, shell=False)` with bounded output and timeout.
- Worker bearers live in `0600` files referenced by a non-secret environment path.
- Claude proxy bearers are distinct, short-lived, stored in `0600` files, and accepted only by per-run loopback listeners.
- Artifact publication uses descriptor-relative, no-follow opens and immutable copies.
- Attention leases prevent automated responders from racing; a human device may override a lease, and every lease, answer, approval, route, and control is audited.
- Storage pruning is a reviewable plan whose application moves evidence to an instance-owned trash directory.

## Deferred integrations

Workbridge and slash-work do not change in this fork. The overnight delegate belongs in Workbridge's policy layer and should use scoped service identities plus attention leases. Its default authority should cover policy-bound, low-risk answers only. Destructive, external, secret-bearing, or ambiguous decisions remain human attention.

Image inputs, arbitrary MCP packs, external price/runway services, and publication are also deferred until provider-backed evaluation shows that this kernel is worth adopting.
