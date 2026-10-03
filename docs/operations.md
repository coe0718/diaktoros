# Operating a loop

Use this guide after [getting started](getting-started.md). A queued turn is not a started turn;
a process exit is not proof that GitHub accepted a write. The operator owns push policy,
incident recovery, and the decision to merge.

## Contents

- [Install](#first-install-setup) and [generated files](#what-init-writes)
- [First run and push policy](#first-run)
- [Everyday commands](#everyday-commands)
- [Signing](#what-the-loop-signs) and [review findings](#how-the-reviewer-grades-findings)
- [Token files](#token-files-one-pat-per-account) and [permissions](#token-scopes-by-role)
- [Doctor](#preflight-doctor) and [selftest](#verifying-the-isolated-setup-selftest)
- [Storage limits](#sandbox-size-caps-the-two-writable-mounts)
- [Explain a PR](#why-isnt-this-pr-moving) and [trace a delivery](#why-did-that-delivery-start-nothing-trace)
- [Pacing](#pacing-usage-windows-and-daily-caps)
- [Issues](#issue-triage)
- [Gate failures](#when-a-gate-crashes-or-runs-out-of-time)
- [Run recovery](#when-an-isolated-run-fails)
- [Bursts and watchdog](#how-it-handles-a-burst)

Replace quoted angle-bracket placeholders, including brackets, with your values.
`--loop` is the saved loop ID, not a repo or profile. `--pr` is the numeric PR number
(or issue number when diagnosing an issue run). Commands run on the host, not inside
a seat. Full argument details are in [commands](commands.md).

## First install: `setup`

```bash
hermes review-loop setup
```

The interactive wizard checks prerequisites, collects configuration/account mappings,
and prepares the runtime and routes. No flags are required. If it stops, preserve the
exact failure and inspect both local state and repository hooks before resuming:
a partial install is not necessarily a rollback. Setup does not grant fixer push permission.

## What `init` writes

`init` is the non-wizard path. It writes loop JSON under `$HERMES_HOME/review-loops.d/`,
role routes in the gateway registry and gate shims; the configured skill name is a
reference, not a promise to generate a new skill. Optional
observer/adjudicator routes depend on supplied settings. The private runtime file is
separate: having a route is not enough to launch an isolated turn. Repository hooks
require explicit installation and are paused by default (`init --arm` explicitly
requests live hooks and requires `--hooks`). A requested `--schedule` installs the shared
watchdog job/shim; a scheduler failure can leave config/routes/hooks installed while
init reports incomplete. Inspect those partial effects before repeating installation.
See [configuration](configuration.md)
for file layouts and [commands](commands.md) for `init`, `apply` and `arm`.

`status` prints the effective loop state directory. Default state is under
`$HERMES_HOME/state/review-loops/`. Keep the gateway and CLI on the same Hermes home.

## First run

1. Run `doctor`; resolve failures and explicitly decide warnings.
2. Run `selftest --no-model`, then a model-enabled test if desired.
3. Confirm hook destination, event coverage and signatures using `arm`.
4. Open a non-draft PR from an allowlisted fixer against the configured base.
5. Inspect the actual review on GitHub. With pushes off, changes requested produce
   a held fixer, not a broken loop. Fix by hand or explicitly opt in below.
6. After a manual fix, request the configured reviewer. A push alone does not start review.

### Choose whether to permit unattended fixer pushes

**Off by default.** The gate records a changes-requested verdict as a queue hold without
launching a model. Policy also gates fixer answers/review-request writes and issue fixes.

```bash
hermes review-loop fixer-push --loop "<loop-id>" --enable --acknowledge-pr-race --dry-run
hermes review-loop fixer-push --loop "<loop-id>" --enable --acknowledge-pr-race
hermes review-loop status --loop "<loop-id>"
```

`--enable` opts this repository into host policy. `--dry-run` validates without writing;
remove it to apply. `--acknowledge-pr-race` accepts the residual race: closing, drafting
or retargeting a PR between the last metadata check and Git's exact-SHA-lease push can
still publish. The lease protects the ref, not PR metadata. This is **host-operator
acknowledgement, not verified GitHub owner/admin consent**. Read [security](security.md).

Enabling is refused while a fixer is active or uncertain. Do not delete state to bypass
that check. Existing push-off queue holds are rechecked by the next armed watchdog
sweep or `drain --seat fixer`. A ledgered fixer cancelled by the push policy for missing push admission
needs explicit `retry` after opt-in; redelivery never upgrades admission. The broker
checks live policy again before writing.

```bash
hermes review-loop fixer-push --loop "<loop-id>" --disable
```

`--disable` revokes future unattended writes; it cannot undo accepted writes or eliminate
an in-flight metadata/ref race. A deliberate pause of webhook intake is likewise not
a kill switch for a worker already running.

## Everyday commands

| Task | Command | Interpretation |
| --- | --- | --- |
| Inspect wiring and holds | `hermes review-loop status --loop "<loop-id>"` | Local state and recent problem rows, not a full audit |
| Check prerequisites | `hermes review-loop doctor --loop "<loop-id>"` | Install checks, not proof every GitHub write will succeed |
| Explain one PR | `hermes review-loop explain --loop "<loop-id>" --pr "<pr-number>"` | Live facts and `next:` recommendation |
| Drain a queue | `hermes review-loop drain --loop "<loop-id>" --seat reviewer` | State-changing scheduling request with eligibility rechecks |
| Enable and verify hooks | `hermes review-loop arm --loop "<loop-id>" --admin-token "<hook-admin-login>"` | Activates loop hooks and verifies signed pings |

For drain, `--seat reviewer` is the default; `--seat fixer` chooses the fixer queue and
still respects push policy. `--admin-token` names a mapped login with hook-management
permission, never a token value. For a deliberate hook pause use the workflow in
[commands](commands.md), confirm GitHub hook state, and inspect existing runs separately.

## What the loop signs

By default the host adds an automation footer to reviews, fixer answers, ruling comments,
triage comments, issue-fix PR descriptions and issue comments. Fixer commits carry an
`Automated-By: hermes-review-loop (…)` trailer. Labels cannot carry a signature. Attribution
is added by the host, not parsed from the model's summary.

```bash
hermes review-loop set --loop "<loop-id>" --attribution off
```

`--attribution off` changes future writes; `on` restores it. `status` and `doctor` report
the effective setting. Keep plugin settings consistent before the next `apply`.

## How the reviewer grades findings

The reviewer prompt grades findings P0–P3 and separates **blocks** from **issue**.
P0/P1, regressions, silent data loss, wrong-seat dispatch, false safety greens, and a
build/test broken by the change block and require REQUEST_CHANGES. Other real findings
(usually P2/P3) belong under **Issues to file**, with suggested issue titles, and need
not consume another round: this heading does not automatically create GitHub issues.
Every finding needs `file:line` and observed verification evidence. Only APPROVE or
REQUEST_CHANGES is a valid review verdict; a comment-only review cannot advance the loop.
Missing offline dependencies or insufficient sandbox build space are environment notes,
not automatically PR defects; the seat must state what it did and did not verify. With
nothing verified it still cannot approve. A partial change view cannot authorize approval.
Fixer answers
are published as a bounded PR comment before a fresh review request; a final model
summary is not a receipt. At the cap, adjudication records `ACCEPT`, `REJECT` or `RESPEC`.
A ruling is not a merge and does not grant permission to blindly resume the old loop.

## Token files: one PAT per account

Each GitHub login maps to its own nonempty private token file readable by the gateway user.
Reviewer/fixer writes use their own identities; read/control identity is separate. A
profile name does not establish GitHub identity: the broker checks the token principal.
See [accounts](accounts.md) for separation, least privilege and rotation.

```bash
hermes review-loop set --loop "<loop-id>" --token "<github-login>=<absolute-token-file>"
```

`--token` maps a login to a path (repeatable), not to literal token contents. Never paste
credentials into commands, chats, issues, profiles or diagnostic reports.

## Token scopes by role

| Role | Needed capabilities |
| --- | --- |
| Read/control | Read repository contents, PRs, reviews, issues and hook state needed by gates |
| Hook administrator | Inspect/create/activate/delete repository webhooks |
| Reviewer | Submit PR reviews; read contents |
| Fixer | Write contents, PR comments and review requests; open PRs; issues write for issue comments |
| Triage | Issues write for labels and optional comments |
| Adjudicator with comment identity | Post PR conversation comments |

Fine-grained permissions commonly include Contents, Pull requests, Issues and Webhooks
at the level appropriate to these operations; classic `repo` tokens bundle broad access.
Use [accounts](accounts.md) for details. File-mode checks cannot establish GitHub scopes,
repository access, expiry, SSO approval or identity. Do not use the read token as triage writer.

## Preflight: `doctor`

```bash
hermes review-loop doctor --loop "<loop-id>"
hermes review-loop doctor --loop "<loop-id>" --repair
```

Inspect the first report before mutating anything. `--repair` permits local route/shim
restoration from recorded installation intent, not general credential, GitHub, or run
recovery. Follow the specific remedy for observer destination mismatches. A push-policy
warning is expected if you intentionally keep pushes off.

## Verifying the isolated setup: `selftest`

```bash
hermes review-loop selftest --loop "<loop-id>" --no-model
hermes review-loop selftest --loop "<loop-id>" --pr "<pr-number>"
hermes review-loop selftest --loop "<loop-id>" --pr "<pr-number>" --live-turn
```

`--no-model` skips the tiny real completion while checking containment/runtime.
`--pr` adds a dry-run reviewer authorization check against that live PR. `--live-turn`
requires a PR and runs a real isolated reviewer conversation, printing rather than
posting its verdict; model/build costs may apply.

The private `$HERMES_HOME/review-loop-runtime.json` must be mode `0600`. It supplies
launcher settings; seat models resolve from the configured Hermes profiles. Selftest
success is not assurance that every build fits the sandbox or later write remains eligible.
See [configuration](configuration.md) for runtime keys and [troubleshooting](troubleshooting.md)
when doctor and selftest disagree.

## Sandbox size caps: the two writable mounts

Checkout and scratch are independently bounded; Rust dependency prefetch also has a
host crate-cache limit. Relevant gateway-environment overrides are
`REVIEW_LOOP_CHECKOUT_SIZE_GIB`, `REVIEW_LOOP_SCRATCH_SIZE_GIB` and
`REVIEW_LOOP_CRATE_CACHE_GIB`. Consult [configuration](configuration.md) for defaults,
units and accepted bounds. Set them in the **gateway launch environment**, not only
the shell running selftest, and restart/reload through your normal service procedure.
Production workers inherit them. Raising a cap increases disk exposure, not network
permission; verify a fresh turn and the actual filesystem failure before increasing it.

## Why isn't this PR moving?

```bash
hermes review-loop explain --loop "<loop-id>" --pr "<pr-number>"
```

Read head/base, effective review, queues/claims, cap marker, problem runs and `next:`
together. Expected holds include draft/wrong author/base, no eligible review request,
a same-head verdict, push policy, pacing, spent cap, stacked-base generation changes,
and post-write quarantine. Unreadable GitHub state is unknown, not permission to run.

Exit 0 means the explanation was produced, not that a turn should run. Exit 2 means
an unknown loop, a loop file the loader refuses, no `--loop` when several loop files
exist, or no loop files at all. Parser errors also exit 2: `the following arguments
are required: --pr`, `invalid int value` for a nonnumeric PR, `expected one argument`
for a flag without its value, or `unrecognized arguments` for unsupported options.
A refused invocation is not an instruction to change or replay the PR.

## Why did that delivery start nothing? `trace`

```bash
hermes review-loop trace --loop "<loop-id>" --delivery "<delivery-id>" --admin-token "<hook-admin-login>"
hermes review-loop trace --loop "<loop-id>" --payload "<payload-json-file>" --event pull_request_review --route "<fixer-route>"
```

The two sources are alternatives. `--delivery` selects a numeric hook-delivery ID or
`X-GitHub-Delivery` GUID; `--admin-token` chooses a mapped login able to read deliveries.
`--payload` reads a local JSON object; `--event` supplies `pull_request` (default) or
`pull_request_review`; `--route` selects the destination. Trace runs the actual gate
on a copied home without production writes or model launch. It uses **current** facts,
not a historical reconstruction. `issues` deliveries are unsupported.

## Pacing: usage windows and daily caps

`waiting` may mean provider usage-window/429 hold or daily cap, not failed work. Read
`retry_at` and the reason. Pacing does not consume the pre-write failure-retry budget.
Daily caps reset at local midnight; zero removes the cap. An armed scheduling path must
still resume work. Status shows reviewer/fixer pacing; `triage --loop` shows triage settings.
Issue fixes have concurrency one, no daily cap, and the loop-wide turn budget. Raising
concurrency increases aggregate model/storage use, not individual turn speed.

## Issue triage

Opt-in triage handles only newly opened issues by allowlisted authors. Its sandbox has
**no code checkout** and chooses labels from a list; it is not repository-wide duplicate
search or an ongoing issue discussion agent. See [issues](issues.md).

### Issue fixes: a maintainer hands an issue to the fixer

A configured maintainer applies a separate fix label. With triage and push policy on,
an eligible issue gets one fixer turn from the base commit, which may open one PR or
post one cannot-fix comment. It never merges. A partial branch/PR sequence requires
inspection, not replay. [Issues](issues.md) covers setup and truthful limitations.

## When a gate crashes or runs out of time

Gate failures are separate from model failures. The watchdog records/reports crashes,
budget exhaustion and failed-read deliveries and may safely re-drive eligible failures.
Paused loops are not re-driven; unknown hook state fails closed. Resolve the actual
read/runtime/route error first; inspect gateway logs and delivery responses before
redelivery, and check for ambiguous runs/writes. See
[gate troubleshooting](troubleshooting.md#a-gate-crashed-or-timed-out).

## When an isolated run fails

The host commits durable work and launch intent before spawning. Unique
repo/number/head/seat/turn keys deduplicate deliveries. The supervisor SQLite ledger is
`$HERMES_HOME/state/review-loop-runs.sqlite`; **it is safety state, not disposable cache**.
Worker stderr is beside it in `.workers.log`, with bounded rotation.

| State | Meaning | Action |
| --- | --- | --- |
| `pending` | Durable, not yet claimed | Check runtime and scheduling |
| `claimed`, `launching`, `running` | In-flight state | Wait within budget; investigate stale children |
| `waiting` | Backoff/pacing | Read deadline and reason |
| `succeeded` | Worker completed successfully | Verify broker receipts and actual GitHub result |
| `failed` | Error recorded | Retry only proven pre-write work |
| `cancelled` | Superseded or policy refused | New head gets work; policy cancellations can be explicitly re-admitted |
| `uncertain` | Launch/worker/write result unknown | Inspect and reconcile; never blindly retry |

Pre-write transient failures wait two, four and eight minutes between attempts;
the fourth failure exhausts the automatic retry chain. Due work resumes through
worker-enabled recovery (events, finishing work or an armed watchdog sweep), not an
independent timer. A turn-budget kill fails immediately rather than automatically
repeating the same over-budget turn.
Write-ahead evidence, not exit code, decides replay safety. Review claims, push intents,
rulings, answers, triage results and issue-fix records may prohibit replay even if a
row is `failed`.

```bash
hermes review-loop retry --loop "<loop-id>" --pr "<pr-number>" --seat reviewer
```

`--seat` restricts candidates; omitted, it considers eligible problem runs at the newest
offerable ledgered head. Retry resets automatic retry accounting, uses the current seat
budget, and requests worker recovery. It refuses potentially written, uncertain and
reconciled runs. It is not webhook replay.

### Reconcile an uncertain run only after inspection

1. Preserve run ID, head/base, error, PID and write receipts.
2. Inspect reviews, comments, remote branch refs, PRs and review requests for that exact
   run. A lost response is not proof of a missing write.
3. Establish no worker remains. A live or inaccessible recorded PID prevents release.
4. Record your external-state findings and release the hold using the module command.

```bash
python -m review_loop.run_supervisor status "<ledger-path>"
python -m review_loop.run_supervisor reconcile "<ledger-path>" "<run-id>" --reason "<inspection-summary>" --acknowledge-no-live-worker
```

Use an environment where the module is installed, or run from the plugin checkout.
`status` prints all run rows as JSON. `<ledger-path>` is the SQLite file, not a directory;
the next positional argument is the exact run ID. `--reason` stores your investigation;
`--acknowledge-no-live-worker` is mandatory and cannot replace checking the child.
Reconciliation releases the uncertainty hold but **does not replay the old turn**.
It records an operator-reconciliation reason that still prohibits retry; a genuinely
new head gets fresh work. Never delete receipts, launch intent, locks or the database
to make retry pass. Host recreation after ledger loss reports missing history when
presence markers remain; investigate that loss before allowing new writes.

## How it handles a burst

Reviewer/fixer concurrency defaults to one. Excess work queues; live GitHub checks drop
closed, retargeted and superseded queued heads before launch. JSON seat marks and the
supervisor cooperate; a mark alone is not proof of a live process. Long budgets extend
stale-lock thresholds so a healthy long turn is not prematurely declared dead.

The shared watchdog requires no model conversation. It drains eligible queues, resumes
due pre-write retries, restores recorded routes/shims, reports stalls/read failures,
retries safe observer failures, flushes digests, and emits supervisor operator notices.
The operator outbox is separate from the [observer](observer.md). Known-paused hooks
suppress scan/drain; unreadable hooks warn instead of masquerading as a deliberate pause.
Authentication failures alert immediately; other read failures alert after repeated
sweeps, then on cooldown. A first successful armed sweep snapshots old heads rather
than calling old history newly stalled. New head observations, not commit author dates,
drive reviewer stall clocks; an effective current-head approval ends that check.

The default sweep budget is 600 seconds, per-read cap 20 seconds. Slow reads can stop
remaining work; the next scheduled sweep starts fresh. The cron script may report
failure while exiting zero: monitor output and state, not exit code alone. Never set
`REVIEW_LOOP_TEST` in production: it bypasses pause checks and grace periods.
