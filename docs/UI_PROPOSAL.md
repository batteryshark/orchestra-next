# Orchestra-next operator interface

Status: implemented — see CONSOLE.md for the maintained description

Deviations from this proposal:

- (a) Rendering builds DOM nodes with `textContent`; there is no audited escaping function and no `innerHTML`.
- (b) Three static files (`index.html`, `app.css`, `app.js`) instead of one packaged document, so the CSP carries no `unsafe-inline`.
- (c) Run detail has five sections: Activity, Changes, Artifacts, Usage, Evidence. Goal folded into the header strip; Family folded into Evidence.
- (d) Changes and Usage shipped in slice 1.
- (e) The API prefix is `/api` with an unversioned envelope (`instance_id`, `board_revision`, `data`).
- (f) A `pause` control was added next to tell, interrupt, reroute, resume, retry, continue, and stop.

Audience: Fable and the Orchestra-next implementer

## Decision

Orchestra-next should have its own operator interface at
`http://127.0.0.1:8766/`. DeepSeek Harness remains a headless execution engine
inside Orchestra runs. Its optional UI is a useful DSH laboratory, but it is
not the fleet interface and must not become a second control plane for a live
run.

The first interface should be a single, self-contained web document served by
the existing Python HTTP service. It should preserve the useful shape of the
V2 dashboard without carrying forward V2's runtime matrix, runway accounting,
Observer, migration, or harness-specific concepts. It must not introduce a
frontend build system or runtime dependency.

This is an operator console, not a chat client. Its primary job is to answer:

1. What work exists, and what is happening now?
2. What needs a human decision?
3. What did a run do, spend in raw tokens, change in Git, and produce?
4. What can the operator safely do next?

## Product boundaries

Orchestra owns the UI's durable facts and every mutation. DSH supplies model,
goal, tool, compaction, and usage events through Orchestra's normalized event
feed. The browser never talks directly to a DSH process or Claude sidecar.

The UI must include:

- fleet status, groups, profiles, and dispatch;
- run activity, messages, goals, rounds, tools, compaction, and recovery;
- dependencies, children, retries, continuations, and other lineage;
- attention, permission, and verification waits;
- artifacts, Git evidence, verifier results, and merge controls;
- raw token usage and cache epochs without prices or budget projections;
- tell, interrupt, reroute, resume, retry, continue, stop, and attention answer;
- controls, callbacks, storage, and service health.

The UI must not include:

- runtime or harness selection;
- provider-specific trace viewers;
- model discovery implemented outside DSH's advertised catalog;
- runway, quota, pricing, cost, burn-rate, or admission-hold calculations;
- Observer configuration or judgments;
- V2 migration or compatibility affordances;
- direct controls for an Orchestra-owned DSH or Claude sidecar process.

## Information architecture

Use four top-level destinations. On a wide screen they may be persistent
navigation; on a narrow screen they may collapse into a menu.

### Fleet

Fleet is the default view. It should show:

- compact counts for running, queued, waiting, failed, and open attention;
- a clear primary action to create a run;
- group filtering and text search;
- profile, strategy, and status filters;
- runs ordered by recent activity, not database insertion order;
- enough state on each row or card to diagnose it without opening it.

Each run summary should include its group sequence or title, objective excerpt,
status, waiting kind, profile, provider/model, strategy, goal rounds, active
time, raw token total, cache epoch when nonzero, child count, and last update.
Waiting and failed work should be visually prominent. Completed work should be
quiet. Color must reinforce text and iconography rather than carry meaning by
itself.

Creating a run needs a focused basic form: objective, profile, group, working
directory, and permission mode. Strategy, title, ref, dependency conditions,
limits, verifier argv/timeout, and child ceilings belong under an advanced
disclosure. `danger-full-access` requires an explicit confirmation describing
the selected working directory.

### Run detail

Run detail is the center of the product. It may be a full route or a large
drawer, but it must have a stable URL so a run can be bookmarked. The header
should always expose status, profile/route, strategy, active time, round
progress, permission mode, and the actions valid for the current lifecycle.

Use these detail sections:

**Activity** combines human messages, assistant text, goal transitions, tool
calls/results, compaction, verification, recovery, and lifecycle events into
one chronological stream. Routine machine events are collapsed by default but
remain inspectable. Tool output is bounded in the DOM and expandable. The view
must follow live work without stealing scroll position when the operator has
scrolled upward.

**Goal** shows the goal identifier and state, aggregate rounds used versus the
run limit, corrective goals, verification repair count, and any protocol
failure. Ralph runs instead show attempts, descendants, rounds, and terminal
Ralph result. Do not pretend a Ralph descendant is an independent Orchestra
run.

**Changes** shows branch, base and head evidence, status, diff statistics, and
the bounded patch returned by the API. A terminal run may offer merge when the
API permits it. Merge needs a deliberate confirmation and must display refusal
or conflict output without hiding it.

**Artifacts** lists immutable artifacts with media type, size, digest, preview,
and download. Previewing an artifact must preserve authentication and must not
allow arbitrary paths.

**Usage** shows input, output, cache-read, cache-write, and total tokens. Break
usage down by provider/model, cache epoch, and Ralph descendant when facts are
available. Never infer currency, remaining subscription quota, or burn rate.

**Family** shows dependencies, parent/children, retry source, continuation
source, and root run. The relationships should be understandable without a
large graph; a compact tree or linked list is enough.

**Evidence** holds the request snapshot, profile snapshot, verifier argv and
output, worktree path, session identifier, resume count, timestamps, callback
records, and raw events. This is the diagnostic section, not the default view.

Run actions follow lifecycle rules:

- `tell` queues direction at the next safe boundary;
- `interrupt` stops current DSH activity and resumes the same session;
- `reroute` requires provider, model, optional effort, and optional direction;
- `resume` is available only while waiting;
- `retry` and `continue` are available only after a terminal result and make
  lineage explicit before confirmation;
- `stop` is prominent only for nonterminal work and requires confirmation;
- `merge` is separate from completion and never happens automatically.

### Attention

Attention is an inbox, not a notification toast. Show open items first, grouped
by run and ordered oldest first. Each item should display kind, prompt, bounded
context, expiration, current lease, and the run state around it.

A human device may answer directly and override an automation lease, but the UI
must disclose the lease and ask for confirmation before overriding it.
Permission attention should make the requested operation and ten-minute timeout
obvious. Verification attention should show bounded verifier output. Answering
an item and resuming its run are distinct operations unless the API response
establishes that the answer itself requeues the run.

### Configuration

Configuration contains profiles, groups, storage, authentication, and service
diagnostics.

Profiles expose only name, slug, provider, model, optional effort, tier,
optional concurrency, enabled/archive state, note, and revision. Provider,
model, and effort options come from DSH's live ACP catalog. The browser must not
invent or cache a permanent provider list.

Groups expose name, slug, default working directory, optional concurrency,
archive state, and revision. Existing private directory values should not be
assumed available when the API deliberately withholds them.

Storage is report-first. Creating a prune plan is a dry run. Applying it
requires displaying the exact plan and a second confirmation. Controls and
callbacks are searchable audit feeds. A small diagnostics panel should show API
schema/version, DSH profile/version compatibility, and optional Claude sidecar
readiness without exposing credentials or account metadata.

The standalone DSH UI may be offered as an optional “Open DSH laboratory” link
when configured. It must be labeled as bypassing Orchestra durability and must
not be embedded as a live-run controller.

## Visual direction

Retain Orchestra's identity and its dense, calm operator-console character.
Improve hierarchy and legibility rather than turning it into a generic admin
template. The fleet should be scannable at a glance; the activity stream should
feel closer to a capable development tool than a chat bubble transcript.

Design both dark and light themes with system preference as the default. Use a
small set of status colors, restrained motion, visible keyboard focus, semantic
HTML, and WCAG AA contrast. All core actions must work by keyboard. Respect
`prefers-reduced-motion`. At approximately 720 pixels and below, prioritize the
attention inbox, fleet list, run activity, and controls; secondary evidence may
move behind disclosures.

Avoid ornamental charts. Use a visualization only where it communicates a real
relationship, such as goal progress, usage by cache epoch, or run lineage.

## Technical shape

Keep the implementation deliberately small:

```text
GET /                         -> packaged dashboard.html
same-origin /api/*         -> existing stdlib HTTP/API service
dashboard state               -> API snapshots plus cursor polling
durable truth                 -> SQLite and normalized Orchestra events
```

The first version should remain one packaged HTML document with embedded CSS
and JavaScript. It may extract small repository-local assets when that clearly
improves maintainability, but it must not add npm, a bundler, a framework, a
frontend package lock, or a second web server. Reuse small proven pieces of the
V2 dashboard—diff rendering, stable incremental painting, follow-live behavior,
and QR generation—only when their assumptions still hold here.

Use cursor polling rather than inventing a websocket layer. Poll global events
and the selected run's events with `after`; use `board_revision` to decide when
to refresh snapshots. Back off while the tab is hidden and after transport
errors. A future SSE transport may preserve the same state reducer, but it is
not required for the first release.

Never inject API strings with `innerHTML` unless they pass a single audited
escaping function. Render large tool results and diffs lazily. Preserve open
disclosures, focus, selection, and scroll position across refreshes. Every
mutation needs a generated request identifier where the API accepts one, a
busy state that prevents duplicate submission, and an error message that keeps
the operator's input intact.

## API work required before the UI is complete

The current API contains the durable resources but is not yet a safe or
comfortable browser surface. Implement these narrow additions rather than
working around them in JavaScript:

1. Serve the packaged dashboard at `/` and return appropriate security headers,
   including a restrictive content-security policy.
2. Support same-origin, `HttpOnly`, `SameSite=Strict` device cookies for browser
   pairing and logout. Continue accepting bearer tokens for CLI and services.
   Do not store an operator token in localStorage.
3. Enforce same-origin checks for cookie-authenticated mutations.
4. Add `GET /api/auth/me` so the dashboard can distinguish an unpaired
   browser, operator device, scoped service, and run worker.
5. Add `GET /api/models` backed directly by `dsh.catalog`, including allowed
   effort choices and current capability errors. Profile forms consume this
   endpoint.
6. Extend `GET /api/runs` with bounded `limit`, recent-first pagination, and
   status/group/profile filters. Preserve the existing cursor behavior for
   feed consumers.
7. Make artifact preview/download work with same-origin cookie authentication
   and explicit content disposition. Do not place credentials in URLs.
8. Expose enough readiness data to distinguish API health, unsupported DSH,
   noncanonical profile, and unavailable optional Claude authentication. Never
   return secrets or subscription details.

Do not add a catch-all “dashboard” endpoint that duplicates the domain model.
The UI should compose the existing resources. Add a purpose-built endpoint only
when a view cannot be correct or efficient with the current contracts.

## API mapping

| UI concern | API source or action |
| --- | --- |
| Fleet | `GET /api/runs`, `/profiles`, `/groups`, `/attention` |
| Dispatch | `POST /api/runs` |
| Run header | `GET /api/runs/{id}` |
| Activity | `GET /api/runs/{id}/events`, `/messages` |
| Goal, tools, compaction, recovery | normalized run events plus run detail |
| Usage | `GET /api/runs/{id}/usage` |
| Dependencies and family | `/dependencies`, `/children`, run lineage fields |
| Artifacts | `GET /api/runs/{id}/artifacts`, `/artifacts/{id}/content` |
| Git evidence | `GET /api/runs/{id}/changes`, `POST .../merge` |
| Direction and control | `tell`, `interrupt`, `reroute`, `resume`, `stop` |
| New lineage | `retry`, `continue` |
| Attention | `GET /attention`, `POST .../lease`, `POST .../answer` |
| Profiles and groups | `GET`, `POST`, and revision-checked `PATCH` |
| Audit | `GET /controls`, `/callbacks`, `/events` |
| Storage | `GET /storage`, create/get/apply `/storage/plans` |

## Delivery sequence

### Slice 1: usable console

- root document serving, browser pairing, security headers, and `auth/me`;
- fleet view, run creation, run detail header, and polling;
- activity stream, attention inbox, answer, tell, interrupt, resume, and stop;
- responsive layout and empty/error/loading states.

This slice is successful when an operator can run Orchestra-next for a day
without returning to the CLI for ordinary observation or intervention.

### Slice 2: evidence and routing

- goal/Ralph detail, usage/cache epochs, changes, artifacts, verification, and
  family;
- reroute, retry, continue, merge, and dependency-aware dispatch;
- live DSH catalog and profile/group management.

### Slice 3: administration and polish

- controls, callbacks, storage plans, pairing/service identities, and
  diagnostics;
- theme, accessibility, keyboard workflow, mobile refinement, and retained UI
  state;
- optional link to the standalone DSH laboratory.

## Acceptance criteria

- `http://127.0.0.1:8766/` is the only required browser entry point.
- V2 and Orchestra-next can run simultaneously without port, cookie, storage,
  or branding collisions.
- A new browser can pair without exposing its device token to JavaScript after
  redemption.
- The fleet and selected run converge after external CLI/API mutations without
  a page reload.
- Every run and waiting state has an understandable visual treatment.
- An operator can dispatch, inspect, direct, interrupt, reroute, answer, resume,
  retry, continue, stop, and merge subject to API authority and lifecycle.
- Goal rounds, raw usage buckets, cache epochs, compaction, recovery, verifier
  repairs, children, dependencies, artifacts, and Git evidence are visible.
- No price, quota, runway, Observer, migration, runtime, or harness-matrix code
  remains in the interface.
- The UI works at 375-pixel width, by keyboard, with reduced motion, and in both
  system color schemes.
- Tests cover document serving, browser auth and CSRF, HTML escaping, action
  lifecycle gating, cursor updates, scroll preservation, and V2 coexistence.
- The repository still has zero Python runtime dependencies and no frontend
  dependency or build step.

## Definition of done

The work is done when the dashboard is the normal way to operate
Orchestra-next, not merely a read-only demo. A user should be able to understand
the fleet, resolve attention, inspect evidence, and control a run without
knowing that DSH is running underneath it. DSH remains inspectable through
normalized evidence, but Orchestra remains the sole durable control plane.
