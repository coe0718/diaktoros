# Operating a loop

Use this guide after [getting started](getting-started.md). A queued turn is not a started turn;
a process exit is not proof that GitHub accepted a write. The operator owns push policy,
incident recovery, and the decision to merge.

## Contents

- [Install](#first-install-setup) and [generated files](#what-init-writes)
- [First run and push policy](#first-run)
- [Everyday commands](#everyday-commands) and [reviewing your own PRs](#reviewing-your-own-prs)
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
- [Conflicts with the base](#conflicts-with-the-base)
- [Publishing stats](#publishing-stats)

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

## Reviewing your own PRs

List your login (or any account whose PRs you fix yourself) as review-only:

```bash
hermes review-loop set --loop "<loop-id>" --review-only "<your-login>"
```

The reviewer then reviews those PRs like a fixer's. A changes-requested verdict comes back
to you, and the observer notice says so ("returned to the author"); the fixer never gets a
turn on them. There is no verdict cap and no adjudication on a review-only PR. Push your
fix and re-request the reviewer on the PR to get the next review: the reviewer gate
accepts that request from the PR's own review-only author. `explain` shows `next: author-push`
while a verdict waits for you, and the watchdog never reports a fixer stall on these PRs.
`set --no-review-only` clears the list. A login can't be both review-only and a fixer.

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

**CI.** The reviewer and fixer prompts carry the head's CI as the reader saw it just before
the turn: failed checks, checks still running, and how many passed. A failed check is a blocking
finding. At the write, the broker re-reads CI and refuses an APPROVE while any check run or
commit status at the head has failed (a re-run that passed replaces its failure), was cancelled,
or while CI cannot be read. A **cancelled** check (GitHub cancelled the run, for example when no
hosted runner could be acquired) is not the change's fault: the reviewer is told not to list it
as a finding, and the review itself waits for the re-run, whether or not `review_after_ci` is
on, sending one notice that the checks need re-running. The loop cannot re-run CI itself.
With `required_checks` set, all of the above applies to those checks only: an optional check
(a bot, a coverage upload) is shown to the reviewer but never blocks, holds or wakes anyone. The refusal spends nothing, so the reviewer's REQUEST_CHANGES in the same turn
goes through. Checks still running don't block an approval. With `review_after_ci` on, a review
doesn't start while the head's checks are running: it waits (up to an hour), so it sees them
finish.

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
| Read/control | Read repository contents, PRs, reviews, issues, hook state, check runs and commit statuses |
| Hook administrator | Inspect/create/activate/delete repository webhooks |
| Reviewer | Submit PR reviews; read contents |
| Fixer | Write contents, PR comments and review requests; open PRs; issues write for issue comments |
| Triage | Issues write for labels and optional comments |
| Adjudicator with comment identity | Post PR conversation comments |

Fine-grained permissions commonly include Contents, Pull requests, Issues, Webhooks, Checks and Commit statuses
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

## The loop stops with RealHomeError or RealNetworkError

These are **test-harness tripwires**, not production sandbox breach reports.
They arm only when `REVIEW_LOOP_TEST_HOME_GUARD=1` **and**
`REVIEW_LOOP_TEST_GUARD_SENTINEL` names an existing harness sentinel file
(`review_loop/config.py:test_guard_active`). The variable alone does not arm them.

In a real gateway, inspect its launch environment/service configuration for leaked test
settings, unset `REVIEW_LOOP_TEST_HOME_GUARD` there, and restart the gateway through your
normal service procedure. Clearing only the CLI shell does not fix the gateway's environment.
In a real test run, keep the guards enabled and fix the escaped fixture/home/network access;
never disable test guards to obtain a passing test or permit publication.

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
budget, and requests worker recovery. It only re-arms `failed`/`waiting` pre-write runs or the supported fixer push-policy
cancellations; superseded cancellations are refused. Any review receipt, push intent or
confirmation, ruling, triage result or issue-fix record prohibits retry, as does current
uncertainty or an operator-reconciliation/post-write quarantine history. It is not webhook replay.
Even a broker record marked denied or nothing is a record, not permission to re-arm.

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
`status` prints JSON for at most 100 problem rows, oldest first: failed, waiting,
uncertain and supported push-policy cancellations, not every run. `<ledger-path>` is the SQLite file, not a directory;
the next positional argument is the exact run ID. `--reason` stores your investigation (nonempty, at most 512 characters);
`--acknowledge-no-live-worker` is mandatory and cannot replace checking the child.
A missing launch intent also refuses reconciliation: worker identity cannot be established.
An uncertain run retains its seat slot until reconciled; at concurrency one it can stall
other work on that seat. Reconciliation releases the uncertainty hold but **does not replay the old turn**.
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
skip the loop sweep, including scan/drain, route/shim self-heal, observer retries/digest
flushes and pre-write worker retries. Recorded gate-failure alerts can still be reported
without re-drive. Unreadable hooks warn instead of masquerading as a deliberate pause.
Authentication failures alert immediately; other read failures alert after repeated
sweeps, then on cooldown. A first successful armed sweep snapshots old heads rather
than calling old history newly stalled. New head observations, not commit author dates,
drive reviewer stall clocks; an effective current-head approval ends that check.

The default sweep budget is 600 seconds, per-read cap 20 seconds. Slow reads can stop
remaining work; the next scheduled sweep starts fresh. The cron script may report
failure while exiting zero: monitor output and state, not exit code alone. Never set
`REVIEW_LOOP_TEST` in production: it bypasses pause checks and grace periods.

## Conflicts with the base

When a loop PR stops merging into its base (`main` moved and both changed the same lines), the
watchdog sends one `conflict` notice per head (#303). With unattended fixer pushes on, it also
queues one **resolving fixer turn** per head and base:

1. The host merges the base (pinned at the commit it saw) into the PR head itself, with no
   worktree, and stages the result, conflict markers and all, as the fixer's `/work`. The prompt
   names the conflicted files and shows what each side changed in them.
2. The fixer resolves them, runs the tests, and pushes. The host refuses the push if a marker is
   left or a conflicted file is untouched, and commits it as a merge (both parents) under the
   exact-head lease.
3. The fixer asks for the review, and the merged head is reviewed fresh.

The loop does not resolve, and hands to you instead (the run ends `failed` with
`conflict needs a person: …`):
- a **whole-file** conflict: a modify/delete, a rename, a binary file. Git keeps one side whole,
  so there is nothing to merge line by line;
- a base that **changed workflow files** since the PR branched: GitHub refuses that push from a
  token without the `workflow` scope, which the loop never asks for.

Merge the base into the branch yourself, then `hermes review-loop review --pr N`.

## Publishing stats

[`stats`](commands.md#stats) reports what a loop did: seat turns from the run ledger, and with
`--github` its PRs and reviews. `--html FILE` writes one self-contained page (no scripts or remote
assets) and `--json` the same data. Both hold totals and timings only, never error text, paths or
prompt content, so either can be published. The page shows the seat accounts' and PR authors'
logins, as GitHub does.

The plugin never publishes anything itself: putting the page on the web is your step, with your
own credentials. For GitHub Pages:

1. Once, create a `gh-pages` branch in a repository you control, checked out at a path of your
   choice (`~/review-loop-stats` here), and turn on Pages for that branch under **Settings →
   Pages**. A public repository publishes the page to anyone.
2. Refresh it on a schedule (a cron entry or a systemd timer), as the operator:

   ```bash
   #!/bin/sh
   set -e
   hermes review-loop stats --loop "<loop-id>" --since 30d --github --html ~/review-loop-stats/index.html
   git -C ~/review-loop-stats add index.html
   git -C ~/review-loop-stats commit -qm "stats $(date -u +%F)"
   git -C ~/review-loop-stats push -q
   ```

The ledger's turn timings exist only on the host, which is why the page is built there. A
repository that wants GitHub-side numbers alone can compute them in a scheduled Actions workflow
instead; the ledger's columns are not on GitHub.
