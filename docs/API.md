# Orchestra-next API v3

The service binds to `127.0.0.1:8766` by default. All resources use `/api/v3`; JSON responses include the API version, instance id, and board revision.

## Core resources

```text
GET|POST  /api/v3/runs
GET       /api/v3/runs/{id}
GET       /api/v3/runs/{id}/events
GET       /api/v3/runs/{id}/usage
GET       /api/v3/runs/{id}/dependencies
GET       /api/v3/runs/{id}/messages
GET|POST  /api/v3/runs/{id}/children
GET|POST  /api/v3/runs/{id}/artifacts
GET       /api/v3/runs/{id}/changes
POST      /api/v3/runs/{id}/merge

POST      /api/v3/runs/{id}/tell
POST      /api/v3/runs/{id}/interrupt
POST      /api/v3/runs/{id}/reroute
POST      /api/v3/runs/{id}/resume
POST      /api/v3/runs/{id}/retry
POST      /api/v3/runs/{id}/continue
POST      /api/v3/runs/{id}/stop

GET|POST  /api/v3/profiles
PATCH     /api/v3/profiles/{id-or-slug}
GET|POST  /api/v3/groups
PATCH     /api/v3/groups/{id-or-slug}
GET       /api/v3/attention
POST      /api/v3/attention/{id}/lease
POST      /api/v3/attention/{id}/answer
GET       /api/v3/controls
GET       /api/v3/callbacks
GET       /api/v3/events
GET       /api/v3/usage
GET       /api/v3/storage
POST      /api/v3/storage/plans
GET       /api/v3/storage/plans/{id}
POST      /api/v3/storage/plans/{id}/apply
```

List/feed endpoints accept an `after` cursor where applicable. The usage feed contains raw token facts only.

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

## Authentication

Bearer types are operator devices, scoped services, and active run workers. Service authorities are independently grantable: `read`, `dispatch`, `attention-answer`, `reroute`, `resume`, `retry`, and `stop`. Run credentials are self-scoped to read, delegate, open attention, and publish artifacts.

Bootstrap and pairing endpoints:

```text
POST /api/v3/auth/bootstrap
POST /api/v3/auth/pair
POST /api/v3/auth/pair/redeem
POST /api/v3/auth/service-tokens
```

Automated attention responders must lease an item before answering it. Operator devices may override a live lease.
Profile/group mutation, Git merge, storage pruning, pairing, and service-token issuance require an operator device; scoped services cannot turn a narrow bearer into broader authority.
