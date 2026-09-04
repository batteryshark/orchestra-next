# Orchestra-next API

The service binds to `127.0.0.1:8766` by default. All resources use `/api`; JSON responses include the API version, instance id, and board revision.

## Core resources

```text
GET|POST  /api/runs
GET       /api/runs/{id}
GET       /api/runs/{id}/events
GET       /api/runs/{id}/usage
GET       /api/runs/{id}/dependencies
GET       /api/runs/{id}/messages
GET|POST  /api/runs/{id}/children
GET|POST  /api/runs/{id}/artifacts
GET|POST  /api/runs/{id}/delegations
GET       /api/runs/{id}/changes
POST      /api/runs/{id}/merge

POST      /api/runs/{id}/tell
POST      /api/runs/{id}/interrupt
POST      /api/runs/{id}/pause
POST      /api/runs/{id}/reroute
POST      /api/runs/{id}/resume
POST      /api/runs/{id}/retry
POST      /api/runs/{id}/continue
POST      /api/runs/{id}/stop
POST      /api/runs/{id}/stop-tree

GET|POST  /api/profiles
PATCH     /api/profiles/{id-or-slug}
GET|POST  /api/groups
PATCH     /api/groups/{id-or-slug}
GET       /api/messages
GET       /api/attention
POST      /api/attention/{id}/lease
POST      /api/attention/{id}/answer
GET       /api/controls
GET       /api/callbacks
GET       /api/events
GET       /api/usage
GET       /api/storage
POST      /api/storage/plans
GET       /api/storage/plans/{id}
POST      /api/storage/plans/{id}/apply
```

List/feed endpoints accept an `after` cursor where applicable; `/api/runs`, `/api/events`, and `/api/runs/{id}/events` also take `order=asc|desc`, `limit` (runs 1..200, events 1..500), and a `before` cursor (`id < before`) for paging backwards. The usage feed contains raw token facts only.

`stop-tree` (body `{reason?}`, authority `stop`) queues a `stop` control for the run and every non-terminal descendant, walking child runs recursively. It answers `202` with `{run_id, stopped: [ids]}`; already-terminal runs are skipped. The CLI form is `orchestra-next stop-tree --run ID [--reason TEXT]`.

`pause` parks the run at the next safe boundary: the current step is cancelled, the worktree is checkpointed, the DSH process stops, and capacity is released. The run reports `status: waiting` with a null `waiting_kind`, a `waiting_detail` beginning with `paused`, and `paused: true` in its payload. `resume` continues the same session.

## Run request

```json
{
  "request_id": "unique-id",
  "profile": "profile-slug",
  "objective": "Executable objective",
  "group": "general",
  "strategy": "goal",
  "permission_mode": "workspace-write",
  "title": null,
  "cwd": null,
  "ref": null,
  "after": [],
  "requested_by": "operator",
  "limits": {"max_rounds": 32, "active_seconds": 7200},
  "verify": {"argv": ["command", "arg"], "timeout_seconds": 600},
  "max_children": null,
  "max_child_tier": null,
  "allow_antigravity": false
}
```

`strategy` is `goal` or `ralph`. Permission mode is `read-only`, `workspace-write`, or explicit `danger-full-access`. `allow_antigravity` (CLI `--allow-antigravity`) authorizes the worker to send its worktree to Google's Antigravity through `orchestra-next delegate`; it is off by default because it is an external data boundary.

A child run (`POST /api/runs/{id}/children`) inherits its parent's permission ceiling, verifier, `max_children`, and `max_child_tier`; the child body cannot raise any of them, and it may set `allow_antigravity` only when the parent's request has it.

## Antigravity delegations

`POST /api/runs/{id}/delegate` (`{objective, model, mode: "review"}`; run `delegate` authority or an operator device) makes the daemon run the official `agy` CLI itself (`--print --sandbox`, read-only review, cwd = the run worktree, `GEMINI_API_KEY`/`GOOGLE_API_KEY` scrubbed) and record the result; it blocks until the review returns (up to 11 minutes) and answers `201` on `SUCCESS` or `502` with the recorded failure. The daemon, not the worker, runs `agy` because the DSH sandbox cannot exec it. The model must appear in `GET /api/antigravity/models` (the live `agy models` catalog, cached 5 minutes). Each delegation is a fresh Antigravity conversation (resumed conversations returned empty answers and earn no cache credit); the worktree is mounted into the CLI sandbox with `--add-dir`, and the operator's agy `permissions.allow` rules decide which read commands the reviewer may run — without them every tool is auto-denied and the delegation records `DENIED`. A success with no response text records `EMPTY`. `orchestra-next delegate "<objective>" --model <id>` is the worker bridge over that route: it prints the response to stdout and a one-line JSON summary to stderr. `POST /api/runs/{id}/delegations` records an externally produced result with the same fields (`model`, `mode`, `objective`, `conversation_id`, `status`, `num_turns`, `duration_seconds`, `usage {input, output, thinking, cache_read, total}`, `response`, `truncated`, `error`, `stderr`).
Antigravity reports usage cumulatively per conversation, so the server stores the raw snapshot and a `delta` against the newest prior delegation with the same `conversation_id` (first turn: delta = usage; negatives clamp to 0). The delta becomes one `usage_events` row with provider `antigravity` and `event_type` `antigravity.delegate`; it never counts toward the run's `tokens_*` columns.
`GET /api/runs/{id}/delegations` returns the recorded delegation events newest first, each with `event_id` and `created_at`.

## Authentication

Bearer types are operator devices, scoped services, and active run workers. Service authorities are independently grantable: `read`, `dispatch`, `attention-answer`, `reroute`, `resume`, `retry`, and `stop`. Run credentials are self-scoped to read, delegate, open attention, and publish artifacts.

Bootstrap and pairing endpoints:

```text
POST /api/auth/pair
POST /api/auth/pair/redeem
POST /api/auth/service-tokens
```

Automated attention responders must lease an item before answering it. Operator devices may override a live lease.
Profile/group mutation, Git merge, storage pruning, pairing, and service-token issuance require an operator device; scoped services cannot turn a narrow bearer into broader authority.

### Trusted networks

The bootstrap file may declare networks whose peers are operators without a token:

```json
{"bind": "100.101.102.103", "trust_tailnet": true, "trust_loopback": false,
 "trusted_cidrs": [], "allowed_hosts": ["machine.tail1234.ts.net"]}
```

`trust_tailnet` trusts the Tailscale address ranges (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`). `trust_loopback` trusts local processes, which fits a `tailscale serve` proxy. `trusted_cidrs` adds explicit ranges. All default to off; pairing then remains the only browser path. A trusted peer appears as identity kind `network` with full operator authority, so tailnet ACLs are the access control. Requests from trusted peers still pass the Host allowlist (loopback, the bind address, `*.ts.net`, `allowed_hosts`, and trusted-range IP literals), and a browser mutation from a trusted peer must carry a same-origin `Origin` header; clients that send no `Origin`, such as curl, pass. Trust applies only to requests that present no credential: a revoked or invalid bearer from a trusted peer is refused. A process on the daemon's own host that connects through a trusted LAN or tailnet address is not a trusted peer; only `trust_loopback` vouches for local processes, and with it every local process — including a run's own shell — holds operator authority.

`worker_env` (default `[]`) restricts the environment passed to DSH workers and verifiers to the listed names or `PREFIX_*` patterns, plus `ORCHESTRA_NEXT_*`, `DSH_*`, `PATH`, `HOME`, `TMPDIR`, `LANG`, `LC_*`, `TZ`, and `TERM`. An empty list inherits the daemon's whole environment.

## Messages ledger

`GET /api/messages` lists every operator→run control across the fleet, newest first. Query: `status` (`queued`, `delivered`, `undeliverable`), `kind` (`tell`, `interrupt`, `pause`, `resume`, `reroute`, `stop`), `run` (run id), `limit` (1..200, default 200), `before` (the `id` cursor of the last row on the previous page). Each row carries `id`, `message_id`, `run_id`, `run_title`, `sender`, `kind`, `status`, `body` (first 200 characters; the inner text for reroute payloads), `created_at`, `delivered_at`.

A message is `queued` until the supervisor claims it at a process boundary (`delivered`, with `delivered_at`). When a run reaches a terminal state with queued messages, they become `undeliverable`.
