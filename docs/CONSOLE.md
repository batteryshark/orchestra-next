# Orchestra Next operator console

Maintainer notes for the browser console. The code is the source of truth; when this file and `orchestra/ui/app.js` disagree, fix this file.

## 1. What it is

The daemon serves one page at `/`. It is three static files under `orchestra/ui/`: `index.html`, `app.css`, `app.js`. There is no build step, no bundler, no package lock, and no runtime dependency. `app.js` is one ES module loaded with `<script type="module">`.

The audience is the operator: the person who dispatches runs, answers attention, inspects evidence, and controls a run. It is not a chat client and not a DSH front end.

It deliberately excludes: prices, quota, runway, burn rate, and admission holds; the Observer; runtime or harness selection; provider-specific trace viewers; V2 migration; and direct control of the DSH or Claude sidecar process. The browser talks only to `/api`.

## 2. Serving and security

`orchestra/http.py` serves the console.

- `STATIC` is a fixed dict of three paths: `/` → `index.html`, `/app.css`, `/app.js`. Every other path falls through to the API and returns 404. Static replies carry `Cache-Control: no-cache`.
- Every response carries `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and the CSP `default-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'`. There is no `unsafe-inline`, so inline scripts, inline styles, and `on*=` handlers do not run. External origins, framing, `<base>`, and cross-origin form posts are refused. `data:` is allowed for images only (the favicon).
- Host validation runs before anything else, static files included. Accepted hosts: `127.0.0.1`, `localhost`, `::1`, the bind address, and `allowed_hosts` from the bootstrap file. When trusted networks are configured, any `*.ts.net` name and any IP literal inside a trusted range also pass. Anything else is `400 invalid Host header`.

Browser pairing:

1. Run `orchestra-next pair` in a terminal. The CLI calls `POST /api/auth/pair` with the operator bearer and prints a 12-character code (`XXXX-XXXX-XXXX`, default TTL 300 s, single use). Case, dashes, and spaces are ignored; `O`→`0`, `I`/`L`→`1`.
2. The pairing screen posts `POST /api/auth/pair/redeem {code, name, cookie: true}`.
3. The reply is `201` with the device record only. The bearer travels in `Set-Cookie: orchestra_device=od_…; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict`. JavaScript never sees the token. The console stores nothing in `localStorage`.

Credential resolution: an `Authorization: Bearer` header wins; otherwise the cookie. CSRF rule for non-GET requests: a cookie-authenticated request must carry `Origin` equal to `http://` + `Host`, exactly. A trusted-network peer that sends an `Origin` must also match; a trusted peer with no `Origin` (curl) passes. Bearer requests skip the check. CLI and services use bearers (`od_` device, `os_` service, `or_` run worker).

Trusted networks (`trust_tailnet`, `trust_loopback`, `trusted_cidrs`; all off by default): a peer inside a trusted range with no valid credential gets identity kind `network` with authority `*`. `_operator()` in `api.py` accepts kinds `device` and `network` only; it gates profile and group mutation, merge, pin, storage plans, device and token listing and revocation, pairing creation, and service-token issuance. A scoped service cannot reach those routes.

Logout: the console confirms, then `POST /api/auth/logout`. The server clears the cookie (`Max-Age=0`) and revokes the browser device. If that device is the last active operator device, the revocation is skipped and only the cookie clears. The Diagnostics panel hides the button for `network` identities.

## 3. Runtime model

State is one object, `store` (auth, me, instanceId, boardRevision, snapshotsStale, runs Map, attention, profiles, groups, readiness, route, detail, filters, ui). Feature blocks add fields with a comment (`store.models`, `store.catalog`, `store.identities`).

Rendering: `markDirty(...views)` adds names to a `dirty` set and queues one `render()` per frame with `requestAnimationFrame`; when `document.hidden`, it uses `setTimeout(render, 0)` because rAF does not fire. `render()` shows the pairing screen when `store.auth === "unpaired"`, otherwise shows the routed view and renders only the marked views that match the route.

`keyedList(container, items, keyFn, sigFn, buildFn, updateFn)` reuses DOM rows. It keeps a `container._keyed` Map of key → `{el, sig}`, builds new rows, calls `updateFn` only when the signature changes, reorders with `insertBefore`, and removes unseen keys. Rows keep their identity across polls, so `<details open>`, focus, and scroll position survive. Run rows use `runSignature()`; the feed uses the message status; raw event rows never update.

Envelope handling in `req()`: a `401` sets auth to `unpaired`; a reply without `board_revision` calls `fatal()` and stops polling (except `/api/openapi*`); a changed `instance_id` calls `resetStore()`; a changed `board_revision` sets `snapshotsStale`.

Poll loop (`tick`):

1. If auth is unknown, `GET /api/auth/me`.
2. On the first pass, `GET /api/events?order=desc&limit=1` seeds `store.cursorsEvents`. After that, up to ten pages of `GET /api/events?after=<cursor>` per tick. The rows are discarded; the call advances the cursor and carries `board_revision` in the envelope.
3. If `snapshotsStale`, `refreshSnapshots()` fetches `/api/runs?order=desc&limit=200`, `/api/attention?status=open`, `/api/profiles`, `/api/groups` in parallel, plus the open run and its messages. A `404` on the open run toasts and routes to `#/`.
4. On the run view, `pollRun()` (below).
5. `schedule()` waits 2 s visible, 30 s hidden. Transport errors (`status 0` or `>= 500`) double the wait up to 30 s; from the second failure a banner shows the retry delay. `visibilitychange` and every successful mutation call `schedule(true)` for an immediate tick.

Run detail: on first open, `GET /api/runs/{id}/events?order=desc&limit=500`; `absorbHistory()` reverses the page, sets `eventsAfter` to the newest id, and `historyFloor` to the oldest. Later ticks page forward with `after=<eventsAfter>`. "Load earlier history" fetches `order=desc&limit=500&before=<historyFloor>`, prepends, and restores the scroll offset. `historyDone` is true when a page is short. Changes, Usage, and Artifacts load once per section visit and reload after each snapshot refresh; dependencies and children load once per run.

Hash routes (`parseHash`): `#/` Fleet; `#/runs/{id}` Activity; `#/runs/{id}/{section}` with section in `activity | changes | artifacts | usage | evidence` (unknown falls back to Activity); `#/attention`; `#/config`. Anything else is Fleet. There is no `#/pair/{code}` route. Opening a different run resets `store.detail`, seeds the header from the fleet row when known, and polls at once.

## 4. Rendering rules

- All DOM comes from `el(tag, props, ...children)`. Strings become text nodes; `text:` sets `textContent`; `dataset:` sets `data-*`. `tests/test_ui_document.py` fails on `innerHTML`, `outerHTML`, `insertAdjacentHTML`, or `document.write` anywhere in `app.js`, and on inline `<script>`, `<style>`, `style=`, or `on*=` in `index.html`.
- Confirmations use the native `<dialog id="confirm">` through `confirmDialog({title, body, confirmLabel, danger, input})`, which resolves `false`, `true`, or the typed string. Feature dialogs (`direct`, `reroute`, `continue`, `profile-dialog`, `group-dialog`, `token-dialog`, `token-reveal`) are also `<dialog>`.
- Events are delegated at `document`: `click` on `[data-action]` dispatches to `ACTIONS[name](element, event)`; `submit` on `form[data-form]` dispatches to `FORMS[name](form, event)`; `input`/`change` on `[data-filter]` updates `store.filters` (text debounced 150 ms).
- `ACTIONS` and `FORMS` are plain objects declared in `--- actions ---`. Feature blocks extend them with `Object.assign(ACTIONS, {...})` inside their own anchor block. The conformance test requires every `data-action` in the HTML to appear as a quoted key and every `data-form` as `name:` in `app.js`.
- Sentinel comments `// --- name ---` / `// --- end name ---` must pair in order and never nest; the test compares the start and end lists. `index.html` uses `<!-- sliceN-x dialogs -->` and `app.css` uses `/* sliceN-x */` for the same slices.
- Bounded text: `boundedPre()` clips at 2,000 chars with an Expand button and slices at 50,000 chars behind `<details>`.

CSS (`app.css`) declares layers `tokens, base, components, views, utilities`. Tokens on `:root`:

| Token | Role |
| --- | --- |
| `--bg`, `--panel`, `--soft` | page ground, card surface, inset surface |
| `--line`, `--line-strong` | hairline and input borders |
| `--text`, `--muted`, `--faint` | three text levels |
| `--accent`, `--accent-text`, `--accent-soft` | primary actions, running status |
| `--good`, `--good-soft`, `--warn`, `--warn-soft`, `--bad`, `--bad-soft` | status colour plus tint |
| `--focus-ring` | `color-mix` of accent, used by `:focus-visible` |
| `--diff-add-text`, `--diff-add-bg`, `--diff-del-text`, `--diff-del-bg` | patch lines |
| `--font-ui`, `--font-mono`, `--fs-xs` … `--fs-l` | type |
| `--sp-1` … `--sp-6`, `--radius-s`, `--radius-m`, `--row-pad`, `--clip-max` | spacing and shape |

Dark values are redefined under `@media (prefers-color-scheme: dark)`; `<meta name="color-scheme">` and `html { color-scheme }` follow the system. `prefers-reduced-motion: reduce` removes transitions and animations. The focus ring is a 2 px outline on `:focus-visible`. Below 720 px the grid collapses, the dispatch form goes static, and the feed shortens.

## 5. Views

**Fleet.** Count buttons for running, queued, waiting, failed toggle status filters (`STATUS_GROUPS`: running = starting+running, failed = failed+timed_out). Text, group, and profile filters run client-side over the loaded runs (`filterRuns`). Rows sort by `updated_at`, newest first; waiting and failed rows get a coloured edge, terminal and paused rows go quiet. "Load older runs" pages with `before=<runsFloor>`. The dispatch form (`<details id="new-run">`) sends `POST /api/runs` with a per-form `request_id` from `crypto.randomUUID()`, regenerated after success. Advanced fields: strategy, title, ref, limits, verifier argv and timeout, child ceilings, and `after` dependencies (one `run_id [condition]` per line). `danger-full-access` opens a danger confirm that names the working directory.

**Run.** The header shows the label, status (`paused` overrides), waiting detail, and chips: profile, route, permission mode (red for full access), strategy, goal state and rounds (attempts for ralph), repairs, corrected, active time, resume count, cache epoch. `RUN_ACTION_MATRIX` decides the buttons:

| Status | Actions |
| --- | --- |
| queued | Direct, Reroute, Stop |
| starting, running | Direct, Pause, Reroute, Stop |
| waiting | Resume, Direct, Reroute, Stop |
| terminal (completed, failed, timed_out, stopped, skipped) | Retry, Continue, Merge (only with a branch) |

A waiting run with kind attention, permission, or verification adds an "Answer →" link. Stop takes an optional reason in the confirm. Pause and Resume post with an empty body; `409` on Resume shows "Run is no longer waiting". Reroute fills routes from `/api/models` (5 min cache, "Retry catalog" on failure) and posts provider, model, effort, message. Retry and Continue call `spawnLineage()`, which keeps one `request_id` per run and kind until the POST succeeds, then routes to the new run. Merge is a danger confirm; the result or the refusal becomes a block at the top of Changes.

- *Activity*: a brief strip with the objective, the merged thread of events and messages (`mergeThread`), and a handoff block with the summary or error once terminal. `threadEntryKind` classifies rows; machine rows (quiet lifecycle, goal, unknown types) stay hidden until "Show machine events". `acp.tool_call_update` rows patch their tool row by `toolCallId`. Follow-live tracks the scroll position (72 px tail threshold); scrolling up unchecks it and shows "Return to live". The Direct modal has two submit buttons: "Tell at boundary" (`tell`) and "Interrupt now" (`interrupt`).
- *Changes*: branch, base, head, diff stat, working-tree status, the diff split per file by `parseDiff` (files open by default when three or fewer), and a note when the patch hits the 200 KB API bound.
- *Artifacts*: name, media type, size, digest, preview, download. `previewPlan` allows inline images up to 2 MB and text or JSON up to 256 KB, fetched with the cookie; other types or pruned files get no preview.
- *Usage*: `aggregateUsage` groups raw rows by provider, model, cache epoch, and Ralph descendant, with a totals row. Tokens only.
- *Evidence*: Family (root, parent, retry of, continuation of, dependencies, children), Facts (ids, worktree, cwd, session, resume count, timestamps, verifier argv), request and profile snapshots, and Raw events with a type filter. The raw block persists across renders so the filter keeps focus; it shows the newest 500 matches.

**Attention.** Open items grouped by run, each with kind chip, non-blocking chip, age, expiry countdown (1 s timer, red under 60 s), lease holder, objective, prompt, context key-values, and option buttons that fill the answer box. Alerts acknowledge with "acknowledged" when the box is empty. Answering an item with a live lease opens a danger confirm before overriding.

**Config.** Profiles (table; New/Edit dialog with datalists from `/api/models`, manual entry when the catalog fails; Enable/Disable and Archive via revision-checked PATCH; a `409` keeps the operator's edits and reloads the untouched fields). Groups (table; New dialog; Rename and Set directory through the confirm input; Archive confirm). Devices and service tokens (revoke with confirm; new token shown once in `token-reveal` and cleared on close). Pairing, Storage, and Audit have headings but empty renderers. Diagnostics: identity, instance, board revision, poll health, and `/api/readiness` (schema, DSH, Claude sidecar, models cached).

**Pairing screen.** Shown whenever auth is `unpaired`. Code and device name; success sets auth `ok`, marks snapshots stale, and polls.

## 6. Keyboard map

From the `keydown` handler in `--- boot ---`. Keys are ignored while typing, except Cmd/Ctrl+Enter.

| Key | Where | Effect |
| --- | --- | --- |
| `/` | any view | focus the run filter |
| `Esc` | any view, no dialog open | back to Fleet |
| Cmd/Ctrl+`Enter` | inside a `data-form` field | submit that form |
| `n` | Fleet | open New run, focus objective |
| `j` `k` `↓` `↑` `Home` `End` | Fleet | move focus across run rows |
| `Enter` | Fleet, row focused | open the run |
| `1`–`5` | Run | Activity, Changes, Artifacts, Usage, Evidence |
| `[` `]` | Run | previous / next section |
| `t` | Run | open Direct |
| `End` | Run | follow live |

## 7. Tests

Run `python3 run_tests.py`. It runs one process per module; pass substrings to select modules (`python3 run_tests.py ui`). Node is optional; the JS tests call `self.skipTest` when `node` is absent.

| File | Covers |
| --- | --- |
| `tests/test_ui.py` | Real HTTP on an ephemeral port: static paths, content types, `no-cache`, 404; security headers; Host validation; cookie pairing (HttpOnly, SameSite, token hidden, replay refused); cookie GET; exact-Origin CSRF; bearer skips CSRF; logout; artifact download with cookie and disposition; trusted-network identity, bare-client mutations, host allowlist extension, bootstrap validation. |
| `tests/test_ui_document.py` | Static conformance: files present; `node --check` on `app.js`; no HTML injection; no inline script, style, or handlers; sentinel pairing and required names; every `data-action`/`data-form` has a handler; dark scheme, reduced motion, `scrollbar-gutter`, `color-scheme` meta. |
| `tests/test_ui_js.py` | Slices `constants`+`fmt`+`logic` and runs them under `node -e`: formatters, `mergeThread`, `parseDiff`, `aggregateUsage`, `filterRuns`, `runSignature`, `extractText`. Exports `slice_section` and `run_node`. |
| `tests/test_ui_routing.py` | `routing-logic`: `routeEfforts`, `rerouteBody`. |
| `tests/test_ui_config.py` | `config-logic`: `profileBody`, `profilePatch`, `groupPatch`, `modelEfforts`. |
| `tests/test_ui_evidence.py` | `evidence-logic`: `absorbHistory`, `rawEventRows`, `previewPlan`. |
| `tests/test_api.py` | In-process `API.handle`: vocabulary, revision guards, leases and service authorities, `auth/me` kinds, `/api/models` cache and 503, `/api/runs` filters and paging, device and token management, `board_revision` bumps on every mutation, pause payload, readiness. |
| `tests/test_api_events.py` | `_event_page`: default ascending page, `order=desc` with `limit` and `before`, the global feed, 400 on bad values. |

## 8. How to add a feature

1. Pick the anchor block. `slice3-admin` is empty and reserved for Pairing, Storage, and Audit; `qr` is reserved for the pairing QR encoder. Add a new `// --- sliceN-name ---` pair only for a new feature, and add matching `<!-- sliceN-name dialogs -->` and `/* sliceN-name */` blocks.
2. Put DOM and store code in the feature block: render functions, `Object.assign(ACTIONS, {...})`, `Object.assign(FORMS, {...})`, and any listeners on static elements. Fill a Config stub by replacing its empty `renderConfig*` function.
3. Put pure functions in a separate `// --- name-logic ---` block with no `document`, `store`, or `api` references. Node runs the block on its own.
4. Add `tests/test_ui_<name>.py`: import `slice_section` and `run_node` from `tests.test_ui_js`, concatenate the needed blocks (`constants`, `fmt`, `logic`, and yours), and assert on JSON output. Skip when `node` is missing.
5. Keep the conformance rules: build DOM with `el()`, no inline handlers, dialogs for confirmations, `act()` around every mutation for busy state and duplicate suppression, `formError()` to keep input on failure, `schedule(true)` after success.

A server change is needed when a view cannot be correct or efficient with the current contracts. Patterns to copy in `orchestra/api.py`: `_event_page()` for any id-cursored feed (`after`, `before`, `order`, `limit` 1..500); the `/api/runs` GET branch for filters (build `where`/`params` lists, validate values against `db.RUN_ACTIVE + db.RUN_TERMINAL` or `groups.find`/`profiles.find`, return 400 on unknown values); `_operator()` for operator-only mutations. Every mutation must bump `board_revision`, or the console will not refresh (`test_board_revision_bumps_on_every_mutation`). Document the route in `docs/API.md`.

## 9. Known gaps

- Config Pairing, Storage, and Audit are heading-only stubs; `slice3-admin` and `qr` are empty. `/api/controls`, `/api/callbacks`, `/api/storage*`, and browser-side `POST /api/auth/pair` are unused.
- Fleet filters run in the browser over the loaded window of 200 runs per page. The server `status`, `group`, and `profile` query parameters are unused; there is no strategy filter and no child count on rows.
- The CSRF check compares `Origin` with `http://` + `Host` only. Behind a TLS proxy, every cookie or trusted-peer browser mutation returns 403.
- No UI state is retained across reloads: filters and "Show machine events" reset. The console never uses `localStorage`.
- Evidence shows the verifier argv but not its output or callback records; verifier output appears only on `verification.failed` rows in Activity.
