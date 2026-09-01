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

GET|POST  /api/profiles
PATCH     /api/profiles/{id-or-slug}
GET|POST  /api/groups
PATCH     /api/groups/{id-or-slug}
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
  "max_child_tier": null
}
```

`strategy` is `goal` or `ralph`. Permission mode is `read-only`, `workspace-write`, or explicit `danger-full-access`.

A child run (`POST /api/runs/{id}/children`) inherits its parent's permission ceiling, verifier, `max_children`, and `max_child_tier`; the child body cannot raise any of them.

## Authentication

Bearer types are operator devices, scoped services, and active run workers. Service authorities are independently grantable: `read`, `dispatch`, `attention-answer`, `reroute`, `resume`, `retry`, and `stop`. Run credentials are self-scoped to read, delegate, open attention, and publish artifacts.

Bootstrap and pairing endpoints:

```text
POST /api/auth/bootstrap
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

`trust_tailnet` trusts the Tailscale address ranges (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`). `trust_loopback` trusts local processes, which fits a `tailscale serve` proxy. `trusted_cidrs` adds explicit ranges. All default to off; pairing then remains the only browser path. A trusted peer appears as identity kind `network` with full operator authority, so tailnet ACLs are the access control. Requests from trusted peers still pass the Host allowlist (loopback, the bind address, `*.ts.net`, `allowed_hosts`, and trusted-range IP literals), and a browser mutation from a trusted peer must carry a same-origin `Origin` header; clients that send no `Origin`, such as curl, pass.
