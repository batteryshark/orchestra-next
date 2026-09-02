# Antigravity CLI delegation spike

Status: implemented as the `orchestra-next delegate` bridge command (see docs/API.md); not a DSH provider

Date: 2026-09-02

## Decision

Use Google's official `agy` CLI as an optional, coarse-grained delegation tool
owned by a DSH run. Do not install a DSH plugin that imports Google OAuth
credentials or calls Antigravity's internal Cloud Code Assist transport.

```text
Orchestra run → resident DSH worker → bounded delegation → official agy CLI
```

Antigravity is another agent harness with its own context, tools, permissions,
conversation state, and fixed prompt overhead. It is not a transparent
foundation-model endpoint. Treating it as an ordinary DSH model would hide
those semantics or require a brittle tool-call translation layer.

## Policy boundary

Google documents `agy` headless mode for scripts, CI pipelines, resident stdin
sessions, model selection, streaming events, and usage reporting. The CLI owns
the account session in the operating-system keyring. Orchestra should invoke
that documented binary and never read, copy, refresh, or export its OAuth
credentials.

Google separately states that using third-party software to access the services
behind Antigravity or Gemini CLI with their OAuth credentials violates its
terms and may result in account suspension. This integration must not:

- call Antigravity or Cloud Code Assist internal endpoints;
- import OAuth client configuration, access tokens, or refresh tokens;
- install a direct OAuth DSH provider on the user's account;
- rotate accounts, synthesize device fingerprints, or bypass quotas;
- silently fall back to an API key or usage-based billing.

References:

- [Antigravity CLI headless mode](https://antigravity.google/docs/cli/headless/)
- [Antigravity models](https://antigravity.google/docs/models/)
- [Antigravity FAQ](https://www.antigravity.google/docs/faq/)
- [Gemini CLI terms and privacy](https://github.com/google-gemini/gemini-cli/blob/main/docs/resources/tos-privacy.md)

Invoking the documented CLI is a materially safer boundary than reusing its
credentials, but it is not a legal determination. Keep the feature private
unless Google clarifies broader product use.

## Environment inspected

- Official CLI: `agy 1.1.24`
- Authentication: existing local keyring session; no credential was read
- Execution: synthetic prompts only, with no repository content transmitted
- Working directory: `/private/tmp` where possible
- Safety: `--sandbox`, no requested tools, no file access or writes

The authenticated account advertised these exact routes:

- `gemini-3.8-flash-{low,medium,high}`
- `gemini-3.7-flash-{low,medium,high}`
- `gemini-3.6-flash-{low,medium,high}`
- `gemini-3.1-pro-{low,high}`
- `claude-sonnet-4-6`
- `claude-opus-4-6-thinking`
- `gpt-oss-120b-medium`

The catalog came from `agy models`; it must remain the source of truth rather
than becoming a checked-in list.

## Results

### Basic Gemini route

A standalone Gemini 3.8 Flash Low request returned the required exact text in
1.62 seconds of provider-reported duration:

```text
input       14,494
output           5
cache read       0
total       14,499
```

Authentication and model selection work. Even a trivial prompt pays a large
fixed Antigravity harness context cost.

### Resident session continuity

One `agy --input-format stream-json --output-format stream-json` process
received two prompts. The second correctly recalled a marker and API endpoint
from the first. Both results used the same conversation ID.

| Counter | Turn 1 | Turn 2 cumulative | Turn 2 delta |
| --- | ---: | ---: | ---: |
| Input | 14,546 | 29,238 | 14,692 |
| Output | 48 | 74 | 26 |
| Cache read | 0 | 0 | 0 |
| Total | 14,594 | 29,312 | 14,718 |

Continuity works, but the second turn received no cache-read credit and incurred
roughly the same input again. Antigravity result usage is cumulative across the
conversation; Orchestra must store raw snapshots and derive deltas against the
prior successful result.

### Opus route

Claude Opus 4.6 Thinking returned a correct, concrete answer to a synthetic
single-writer architecture problem:

```text
input       16,640
output         166
cache read       0
total       16,806
```

The result was useful, but its fixed input floor means Opus should be reserved
for architecture review, difficult diagnosis, or other substantial
delegations—not small inner-loop questions.

### Cancellation

Sending `SIGINT` after the first active streaming event stopped a Gemini turn
in about three seconds. The CLI emitted terminal status `ERROR`, exited `1`, and
reported 14,532 cumulative tokens. It did not remain available for another
stdin turn.

For this integration, an Orchestra-initiated signal is authoritative: `ERROR`
or process exit after that signal should be recorded as an expected
interruption, not a provider failure. The resident process is disposable after
cancellation.

### Cross-process recovery

A fresh `agy` process launched with `--conversation <id>` recovered the earlier
Gemini conversation and returned the remembered marker exactly. The
conversation ID remained stable. Usage advanced to 44,251 cumulative tokens,
again with zero cache reads.

Recovery is possible after process loss, but it is not cheap. The first
implementation should allow one explicit recovery and avoid automatic replay
of an interrupted action.

## Driver requirements discovered

- Read stdout and stderr concurrently. Python text streams may prefetch several
  NDJSON lines, so polling the underlying text pipe with `select()` can stall
  when another line is already buffered. Dedicated bounded line readers are
  simpler and correct.
- The stream begins with one `init`, emits `step_update` rows, and returns one
  `result` per input turn.
- Result usage, turn count, and duration are cumulative; response text belongs
  only to the current turn.
- Bound line size, retained stderr, turn time, process shutdown, and output
  returned to DSH.
- Pin the model on the process command. An unknown model must fail rather than
  fall back.
- Scrub `GEMINI_API_KEY`, `GOOGLE_API_KEY`, and unrelated provider keys so a
  subscription route cannot silently become usage-based API billing.
- Keep `HOME` and the native keyring available; `agy` owns authentication.
- `--mode plan` currently warns that it has no effect when slash expansion is
  disabled. A read-only implementation must not combine those flags and assume
  it is protected. Use the sandbox plus verified permission configuration, then
  test attempted writes explicitly.

## Smallest useful integration

Add one DSH-visible operation named `antigravity_delegate`. It is not a profile,
provider, run strategy, or general-purpose harness abstraction.

Suggested input:

```json
{
  "objective": "Review this design and identify the highest-risk assumption",
  "model": "claude-opus-4-6-thinking",
  "mode": "review"
}
```

Initial constraints:

- `mode` is only `review`;
- the working directory is fixed to the Orchestra run worktree;
- model selection must match the live `agy models` catalog;
- no arbitrary environment, CLI arguments, timeout, or path comes from the
  model;
- one lazily started `agy` process per Orchestra run;
- one delegation at a time;
- no Antigravity subagents or background tasks;
- bounded response and diagnostic output;
- one cross-process recovery using the recorded conversation ID;
- explicit cancellation destroys the process;
- raw usage snapshots and derived deltas are attached to the parent run with a
  distinct `antigravity` source;
- Antigravity may read the worktree only after the operator has authorized that
  external data boundary.

The outer DSH worker remains responsible for the durable goal, deciding what to
delegate, interpreting the result, making changes, running verification, and
reporting completion. This avoids concurrent writers and preserves one visible
Orchestra lifecycle.

## Evaluation before implementation mode

The next paid-provider evaluation should use a disposable child worktree and
explicit authorization to send its contents to Google. Compare Gemini 3.8 Flash
and Opus 4.6 on the same substantial review task and record:

- completion usefulness to the outer DSH goal;
- fixed and incremental tokens;
- wall time;
- tool and permission behavior;
- cancellation and one recovery;
- files read and any attempted writes;
- how much context DSH must repeat when handing work off.

Only add a write-capable mode if that evaluation demonstrates a real advantage.
If write mode is added, give Antigravity a separate child worktree and return
Git evidence to DSH. Never allow DSH and Antigravity to write the same worktree
concurrently.

## Conclusion

The unused subscription capacity is real and both target models work. The
official CLI has a usable resident protocol and recoverable conversation IDs.
Its fixed context cost, absent cache-read reporting, nested-agent semantics, and
destructive cancellation make it a poor transparent provider but a credible
coarse delegation tool.
