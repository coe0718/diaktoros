# Operating a loop

Use this guide after [getting started](getting-started.md). A queued turn is not a started turn;
a process exit is not proof that GitHub accepted a write. The operator owns push policy,
incident recovery, and the decision to merge.

## Contents

- [Install](#first-install-setup) and [generated files](#what-init-writes)
- [First run and push policy](#first-run)
- [Upgrading from hermes-review-loop](#upgrading-from-hermes-review-loop)
- [Everyday commands](#everyday-commands) and [reviewing your own PRs](#reviewing-your-own-prs)
- [Signing](#what-the-loop-signs) and [review findings](#how-the-reviewer-grades-findings)
- [Automatic CI repair](#automatic-repair-for-failed-required-checks)
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
- [Moving to a renamed plugin or repository](#moving-to-a-renamed-plugin-or-repository)
- [The Desktop page](#the-desktop-page)

Replace quoted angle-bracket placeholders, including brackets, with your values.
`--loop` is the saved loop ID, not a repo or profile. `--pr` is the numeric PR number
(or issue number when diagnosing an issue run). Commands run on the host, not inside
a seat. Full argument details are in [commands](commands.md).

## First install: `setup`

```bash
hermes dk setup
```

The interactive wizard checks prerequisites, collects configuration/account mappings,
and prepares the runtime and routes. No flags are required. If it stops, preserve the
exact failure and inspect both local state and repository hooks before resuming:
a partial install is not necessarily a rollback. Setup does not grant fixer push permission.

## What `init` writes

`init` is the non-wizard path. It writes loop JSON under `$HERMES_HOME/diaktoros.d/`,
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
`$HERMES_HOME/state/diaktoros/`. Keep the gateway and CLI on the same Hermes home.

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
hermes dk fixer-push --loop "<loop-id>" --enable --acknowledge-pr-race --dry-run
hermes dk fixer-push --loop "<loop-id>" --enable --acknowledge-pr-race
hermes dk status --loop "<loop-id>"
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
hermes dk fixer-push --loop "<loop-id>" --disable
```

`--disable` revokes future unattended writes; it cannot undo accepted writes or eliminate
an in-flight metadata/ref race. A deliberate pause of webhook intake is likewise not
a kill switch for a worker already running.

## Everyday commands

| Task | Command | Interpretation |
| --- | --- | --- |
| Inspect wiring and holds | `hermes dk status --loop "<loop-id>"` | Local state and recent problem rows, not a full audit |
| Check prerequisites | `hermes dk doctor --loop "<loop-id>"` | Install checks, not proof every GitHub write will succeed |
| Explain one PR | `hermes dk explain --loop "<loop-id>" --pr "<pr-number>"` | Live facts and `next:` recommendation |
| Drain a queue | `hermes dk drain --loop "<loop-id>" --seat reviewer` | State-changing scheduling request with eligibility rechecks |
| Enable and verify hooks | `hermes dk arm --loop "<loop-id>" --admin-token "<hook-admin-login>"` | Activates loop hooks and verifies signed pings |

For drain, `--seat reviewer` is the default; `--seat fixer` chooses the fixer queue and
still respects push policy. `--admin-token` names a mapped login with hook-management
permission, never a token value. For a deliberate hook pause use the workflow in
[commands](commands.md), confirm GitHub hook state, and inspect existing runs separately.

## Reviewing your own PRs

List your login (or any account whose PRs you fix yourself) as review-only:

```bash
hermes dk set --loop "<loop-id>" --review-only "<your-login>"
```

The reviewer then reviews those PRs like a fixer's. A changes-requested verdict comes back
to you, and the observer notice says so ("returned to the author"); the fixer never gets a
turn on them. There is no adjudication on a review-only PR, but there is a verdict cap:
`review_only_cap` (default: the loop's `cap`). At the cap the reviewer declines further
reviews of that PR with nothing spent, says so once per head, and `explain` shows
`review cap reached`. A maintainer or the operator allows exactly one more verdict on the
current head with `hermes dk review --loop "<loop-id>" --pr N --another-round`.
`review_only_daily` (off by default) caps reviewer turns per local day on review-only PRs;
it is a separate pool from the reviewer's `daily_turns` (neither spends the other; with both
set the reviewer may run up to their sum a day, and the reviewer's token budget stays shared).
Once spent they wait until midnight. Unset, review-only turns count against `daily_turns`. Set both with
`set --review-only-cap N --review-only-daily N` (0 clears), `init`, `setup` or the settings
form. Push your
fix and re-request the reviewer on the PR to get the next review: the reviewer gate
accepts that request from the PR's own review-only author. `explain` shows `next: author-push`
while a verdict waits for you, and the watchdog never reports a fixer stall on these PRs.
`set --no-review-only` clears the list. A login can't be both review-only and a fixer.

## Upgrading from hermes-review-loop

Diaktoros was called hermes-review-loop before v0.2.0. Hermes keys a plugin by its manifest name:
- the install folder;
- the `plugins.enabled` entry;
- the settings form (`plugins.entries.<name>.settings`);
- the skill's namespace.

Installing Diaktoros therefore creates a **new** plugin folder, `~/.hermes/plugins/diaktoros`, beside
`hermes-review-loop`, and that plugin isn't enabled yet. `hermes plugins update hermes-review-loop`
isn't the way: it refuses a pinned install (catalog installs are pinned), and it would keep the old
name.

Between turns:

1. `hermes plugins disable hermes-review-loop`, so the two plugins don't both claim
   `hermes review-loop`. Its gates keep running by path until step 4 repoints them.
2. Install Diaktoros: `hermes plugins install diaktoros` from the catalog, or a pinned commit with
   `hermes plugins install coe0718/diaktoros --force --ref <full sha>`.
3. `hermes plugins enable diaktoros`.
4. `hermes dk migrate --dry-run`, then `hermes dk migrate`. Before anything moves, it points every
   route's gate shim and the watchdog at the new plugin, so nothing runs the old code mid-move. It
   then:
   - copies your settings form;
   - gives the host files and the ledger's side files their new names;
   - moves the watchdog job;
   - follows a renamed repository.

   Add `--rename-loop OLD=NEW --admin-token LOGIN` to give a loop the new name too (see
   [renaming a loop](#renaming-a-loop)). See also
   [moving to a renamed plugin or repository](#moving-to-a-renamed-plugin-or-repository).
5. `hermes dk doctor`. When it's green, `hermes plugins remove hermes-review-loop`.

From step 3, `hermes review-loop` works as an alias for one release. Each use says it was renamed.

## What the loop signs

By default the host adds an automation footer to reviews, fixer answers, ruling comments,
triage comments, issue-fix PR descriptions and issue comments. Fixer commits carry an
`Automated-By: Diaktoros (…)` trailer. Labels cannot carry a signature. Attribution
is added by the host, not parsed from the model's summary.

```bash
hermes dk set --loop "<loop-id>" --attribution off
```

`--attribution off` changes future writes; `on` restores it. `status` and `doctor` report
the effective setting. Keep plugin settings consistent before the next `apply`.
When a later seat reads reviewer history or fixer answers, the host strips only a trailing
footer matching the current Diaktoros signature or its pre-rename signature. The footer is
display attribution, not proof of authorship; receipts and the run ledger establish writes.

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
With `required_checks` set, the gating rules apply to those checks only. Optional checks
(a bot, a coverage upload) are shown to the reviewer but never block, hold or wake anyone.
A required check that has not reported at this head is **missing**, not passing: it blocks
approval and holds the review (up to an hour) whatever `review_after_ci` is, unless another
required check has already failed, in which case the review runs and names the failure.
Verify the configured check name and workflow: a missing check is not itself a defect in the PR. The refusal spends nothing, so a
REQUEST_CHANGES verdict in the same turn can still be submitted. Checks still running do
not block an approval. With `review_after_ci` on, a review waits up to an hour for checks to
finish before starting.
A **failed** required check holds the review, with nothing spent and no time limit, whether or
not `review_after_ci` is on, on a **review-only** PR and on a fixer's PR once its CI fixes are
done (#1110). A verdict that only said "CI failed" would spend a round of the cap and close
that head to re-review. The review starts once the check passes at the same head (re-run it on
GitHub) or a new head is pushed. The `ci_failed` notice says so. Only a fixer's PR with
`fix_ci` off is still reviewed red, because that verdict is how the fixer hears of it.

### What every review body needs

The broker checks the body before spending the review write (#427, #687):

- **`Not verified:` line.** Every review ends with one listing what the reviewer did not check,
  or `Not verified: nothing`. A review without it is refused.
- **A test for every fix.** A fix must come with a test that fails without it and passes with it.
  A fix "verified by reading" with no such test is unfinished, and the next review blocks on it;
  if the defect truly cannot be tested in the repository, the fixer's answers must say why.
- **Tests CI already ran.** A test the reviewer cannot run locally is looked up in the head's
  per-test results. `ok` there counts as executed, and the reviewer cites the check; `skipped` or
  absent goes under `Not verified:` and is not a block unless the requirement is that it runs. A
  block that rests only on "could not run" is refused when CI ran that test `ok` at the head.

### Automatic repair for failed required checks

The watchdog can ask the fixer to repair a red required check without waiting for a
`CHANGES_REQUESTED` review. This is a separate, opt-in feature: set `fix_ci: true` in the
loop configuration **and** enable unattended fixer pushes with
[`fixer-push`](#choose-whether-to-permit-unattended-fixer-pushes). The default is off.

A sweep considers open, non-draft PRs on the configured base by an allowlisted fixer who
is not in `review_only`. It reads required-check results and queues at most one CI-fix
turn per PR head. The host rechecks CI before launch and gives the fixer up to three
failing job logs, including the failing step and bounded log tails, as untrusted data.
GitHub serves each log through a redirect to its log storage; the host follows it over HTTPS
only, and never sends the reader's token to the storage host.
The fixer then asks for the next review. If the reviewer is still requested from before (no
verdict yet), GitHub sends no event for that, so the host delivers the request to the
reviewer gate itself. The watchdog backs this up: a head with a pending review request, no
verdict and no review started gets that delivery once, after one sweep interval.
The fixer may propose a scoped patch through the normal broker; the loop does not rerun
GitHub Actions, modify `.github/` workflow files or merge the PR. A green result on a later
head ends the repair path naturally.

CI fixes have their own budget, `ci_fix_cap` CI-fix turns per PR (default 3, across its
heads), outside the reviewer's verdict cap (#539). A red head goes to the fixer first,
whether or not `review_after_ci` is on (#579): the reviewer's turn is held and its CI-fix turn queued at once, so no
review verdict is spent on code that does not build. Once the budget is spent, or the head's
CI-fix turn ended without a new head, the review waits for you (#1110): it starts once the
checks pass at that head (a re-run) or a new head is pushed, and the `ci_failed` notice says
so. While the fixer has a red head, the watchdog does not call the reviewer's silence a stall. The first review after any CI-fix commits
is shown them, with the test files each changed, and blocks one that weakened, skipped or
removed a test. `explain` shows `CI fixes N/CAP`. Normal fixer capacity, pacing, checks and
push safety still apply. See [configuration](configuration.md#write-policy-and-attribution)
for `fix_ci` and [observer](observer.md#events-and-meaning) for the `ci_failed` notice.

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
hermes dk set --loop "<loop-id>" --token "<github-login>=<absolute-token-file>"
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
hermes dk doctor --loop "<loop-id>"
hermes dk doctor --loop "<loop-id>" --repair
```

Inspect the first report before mutating anything. `--repair` permits local route/shim
restoration from recorded installation intent, not general credential, GitHub, or run
recovery. Follow the specific remedy for observer destination mismatches. A push-policy
warning is expected if you intentionally keep pushes off.

## Verifying the isolated setup: `selftest`

```bash
hermes dk selftest --loop "<loop-id>" --no-model
hermes dk selftest --loop "<loop-id>" --pr "<pr-number>"
hermes dk selftest --loop "<loop-id>" --pr "<pr-number>" --live-turn
```

`--no-model` skips the tiny real completion while checking containment/runtime.
`--pr` adds a dry-run reviewer authorization check against that live PR. `--live-turn`
requires a PR and runs a real isolated reviewer conversation, printing rather than
posting its verdict; model/build costs may apply.

The private `$HERMES_HOME/diaktoros-runtime.json` must be mode `0600`. It supplies
launcher settings; seat models resolve from the configured Hermes profiles. Selftest
success is not assurance that every build fits the sandbox or later write remains eligible.
See [configuration](configuration.md) for runtime keys and [troubleshooting](troubleshooting.md)
when doctor and selftest disagree.

## Sandbox size caps: the two writable mounts

Checkout and scratch are independently bounded; Rust dependency prefetch also has a
host crate-cache limit. Relevant gateway-environment overrides are
`DIAKTOROS_CHECKOUT_SIZE_GIB`, `DIAKTOROS_SCRATCH_SIZE_GIB` and
`DIAKTOROS_CRATE_CACHE_GIB`. Consult [configuration](configuration.md) for defaults,
units and accepted bounds. Set them in the **gateway launch environment**, not only
the shell running selftest, and restart/reload through your normal service procedure.
Production workers inherit them. Raising a cap increases disk exposure, not network
permission; verify a fresh turn and the actual filesystem failure before increasing it.

## The loop stops with RealHomeError or RealNetworkError

These are **test-harness tripwires**, not production sandbox breach reports.
They arm only when `DIAKTOROS_TEST_HOME_GUARD=1` **and**
`DIAKTOROS_TEST_GUARD_SENTINEL` names an existing harness sentinel file
(`diaktoros/config/host_guards.py:test_guard_active`). The variable alone does not arm them.

In a real gateway, inspect its launch environment/service configuration for leaked test
settings, unset `DIAKTOROS_TEST_HOME_GUARD` there, and restart the gateway through your
normal service procedure. Clearing only the CLI shell does not fix the gateway's environment.
In a real test run, keep the guards enabled and fix the escaped fixture/home/network access;
never disable test guards to obtain a passing test or permit publication.

## Why isn't this PR moving?

```bash
hermes dk explain --loop "<loop-id>" --pr "<pr-number>"
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
hermes dk trace --loop "<loop-id>" --delivery "<delivery-id>" --admin-token "<hook-admin-login>"
hermes dk trace --loop "<loop-id>" --payload "<payload-json-file>" --event pull_request_review --route "<fixer-route>"
```

The two sources are alternatives. `--delivery` selects a numeric hook-delivery ID or
`X-GitHub-Delivery` GUID; `--admin-token` chooses a mapped login able to read deliveries.
`--payload` reads a local JSON object; `--event` names the event it was (`pull_request`,
`pull_request_review` or `issues`; by default it is read from the payload); `--route` selects the destination. Trace runs the actual gate
on a copied home without production writes or model launch. It uses **current** facts,
not a historical reconstruction. An `issues` delivery replays through the triage gate.

## Pacing: usage windows and daily caps

`waiting` may mean provider usage-window/429 hold or daily cap, not failed work. Read
`retry_at` and the reason. Pacing does not consume the pre-write failure-retry budget.
Daily caps reset at local midnight; zero removes the cap. An armed scheduling path must
still resume work. Status shows reviewer/fixer daily counts and every closed usage window held for the reviewer, fixer, adjudicator or triage profile (a triage seat's window is listed too, though triage has no daily counts there); `triage --loop` shows triage settings.
Issue fixes have concurrency one, their own daily cap (`triage.fix_daily_turns`, default 10, and issue fixes are always capped), and the loop-wide turn budget. Raising
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

## Holding one PR

A maintainer (`triage.maintainers`) holds a PR by applying the `diaktoros:hold` label or
commenting `/loop hold`. `/loop release` lifts either; removing the label lifts only a label
hold, never a `/loop hold` comment. The latest
event from a maintainer wins; anyone else's label or comment is ignored and logged.

While held, no review, adjudication, fix, CI-fix or conflict turn is claimed: queued turns stay pending
and start after release (the next watchdog sweep re-arms the worker). A turn already
running is not interrupted. `explain` and `status` show who held it and when, and the
watchdog reports no stall for it. A hold that cannot be read is treated as held.

It cannot fire on a loop without `triage.maintainers` (they need `triage.fix_label`):
nobody there is allowed to hold. The hold is read at claim time and by the watchdog sweep,
so it needs the read token to see issue events and comments.

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
`$HERMES_HOME/state/diaktoros-runs.sqlite`; **it is safety state, not disposable cache**.
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
independent timer. A turn-budget kill fails immediately rather than automatically repeating
the same over-budget turn. A separate case is a clean agent exit without any broker call.
The host classifies it as unpublished (no broker write was attempted) and retries it once
with a nudge to publish. If that attempt also exits without calling the broker, it fails
for operator review. This is not a retry path for a denied or uncertain write.

Write-ahead evidence, not exit code, decides replay safety. Review claims, push intents,
rulings, answers, triage results and issue-fix records may prohibit replay even if a
row is `failed`.

```bash
hermes dk retry --loop "<loop-id>" --pr "<pr-number>" --seat reviewer
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
python -m diaktoros.run_supervisor status "<ledger-path>"
python -m diaktoros.run_supervisor reconcile "<ledger-path>" "<run-id>" --reason "<inspection-summary>" --acknowledge-no-live-worker
```

Use an environment where the module is installed, or run from the plugin checkout.
`status`, `rulings` and `reconcile` open an existing ledger only: a missing path exits 2 and
creates nothing (#798). `status` prints JSON for at most 100 problem rows, oldest first: failed, waiting,
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

Queued turns are claimed by class, not arrival: a CI-fix or conflict turn first, then a fix
round, then a review, then a new issue fix. A waiting row climbs one class every 15 minutes
(900 s), so the lowest class starts within 45 minutes even while higher classes keep arriving.
`status` prints the queue in claim order and says when a row was aged up (#481).

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
`DIAKTOROS_TEST` in production: it bypasses pause checks and grace periods.

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

**When it is noticed.** A merge into the base is what usually causes a conflict, so the
reviewer gate, on a PR's merged `closed` event, asks for a conflict-only check about 45 seconds
later (`watchdog.py --loop <id> --conflicts`, started detached). A run of merges collapses into one
check after the last of them. The check does only the sweep's conflict step: the same PRs, the same
notice, the same turn, deduplicated the same way. GitHub works out mergeability lazily, so a read
that answers "unknown" is read again once after a few seconds, in the sweep too. A push straight to
the base, or a check that never ran, is still caught by the regular sweep. One checker runs at a
time and stops after about five minutes (half the watchdog's GitHub budget); a merge after that
starts another.

The loop does not resolve, and hands to you instead (the run ends `failed` with
`conflict needs a person: …`):
- a **whole-file** conflict: a modify/delete, a rename, a binary file. Git keeps one side whole,
  so there is nothing to merge line by line;
- a base that **changed workflow files** since the PR branched: GitHub refuses that push from a
  token without the `workflow` scope, which the loop never asks for.

Merge the base into the branch yourself, then `hermes dk review --pr N`.

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
   hermes dk stats --loop "<loop-id>" --since 30d --github --html ~/review-loop-stats/index.html
   git -C ~/review-loop-stats add index.html
   git -C ~/review-loop-stats commit -qm "stats $(date -u +%F)"
   git -C ~/review-loop-stats push -q
   ```

The ledger's turn timings exist only on the host, which is why the page is built there. A
repository that wants GitHub-side numbers alone can compute them in a scheduled Actions workflow
instead; the ledger's columns are not on GitHub.

## Moving to a renamed plugin or repository

Hermes knows a plugin by its manifest name. A plugin installed under a new name therefore starts
with a blank settings form, and the gate and watchdog shims still run the old folder's scripts.
A GitHub repository rename moves the repository under the loop: its webhooks then name a repository
no loop file matches, and its fetches are refused. `migrate` closes both gaps in one step.

Run it between turns, once the new plugin is installed and enabled. The old plugin should be
disabled but still installed, so its settings and scripts stay readable:

```bash
hermes dk migrate --dry-run   # every step, nothing written
hermes dk migrate
```

It runs these steps:

1. It copies each settings-form value the old plugin holds and the new one does not. A value
   already set on the new plugin is kept.
2. **Before anything moves,** it points every gate shim and the watchdog shim at this plugin's
   scripts. From here on, a delivery runs code that honours the pause; the old plugin's code
   doesn't, and would write under the old file names.
3. It moves the shared watchdog job to the new name. A `diaktoros watchdog` job running
   `diaktoros-watchdog.py` is created with the old job's schedule and delivery target, and read
   back. Only then is `review loop watchdog` removed, along with its shim.
4. It gives the host files under `$HERMES_HOME` their new names. It's refused while any run is in
   flight or uncertain. The files:
   - `review-loops.d/` → `diaktoros.d/`, and `review-loop-runtime.json` →
     `diaktoros-runtime.json`;
   - under `state/`: the run ledger (`review-loop-runs.sqlite` → `diaktoros-runs.sqlite`), then
     the ledger's own side files (its `.present` marker, `.workers.log` and any rotation of it,
     with the WAL folded in first), the loops' default state directories (`review-loops/` →
     `diaktoros/`), the gate-failure log, the pacing file and the seat locks.

   A loop whose state directory moved has its loop file updated. Until a file is moved, it keeps
   working where it is; `doctor` names any that still have their old names, side files included.
   If both names exist, the step leaves that file for you to sort out.
5. For each loop whose repository GitHub now reports under another name, it moves the loop's
   records to the new name: the run ledger, the loop's state files and the loop file.
   - It first checks that both names are the same repository.
   - It refuses while a run for that repository is in flight or uncertain.
6. With `--rename-loop`, it renames the loop (see below).
7. It checks the shims once more, then lists every `doctor` check that isn't verified.

Exit 1 means a step was refused or isn't finished; the line says why. Running it again finishes
whatever an interrupted run began. When everything checks out, remove the old plugin with
`hermes plugins remove <old-name>`. Records kept outside the loop's state, such as the
gate-failure log and the broker audit log, keep the old repository name as history.

**The install is paused while `migrate` runs.** It holds a marker,
`$HERMES_HOME/state/migrating.json`, so nothing writes under a name it's moving:
- a gate records its delivery and answers `[SILENT]`, and the watchdog re-drives the review
  and fix gates' deliveries once the migration ends. The re-drive is quiet when it succeeds;
  other gates' deliveries are reported as usual;
- the worker starts no run;
- the watchdog sweeps nothing and says it's paused.

A `migrate` that dies leaves the marker. The install stays paused, because its records may be
half moved, and the watchdog says so until you run `migrate` again, which finishes the job.
A marker that cannot be read or parsed also keeps the install paused; it is not safe to
remove it as stale by guesswork. Inspect the filesystem and migration state, then rerun
`migrate` to recover through the supported workflow. During the pause gates defer deliveries
for later re-drive, workers claim no new runs, and the watchdog skips its sweep.

### Renaming a loop

`--rename-loop OLD=NEW` also gives a loop a new id. A loop's id names its file, its default
state directory, and its routes (`<id>-review`, `<id>-fix` and so on), and through those the
URLs its GitHub hooks post to:

```bash
hermes dk migrate --rename-loop "<old-id>=<new-id>" --admin-token "<hook-admin-login>" --dry-run
hermes dk migrate --rename-loop "<old-id>=<new-id>" --admin-token "<hook-admin-login>"
```

It's refused while one of the loop's runs is in flight or uncertain. Then, in this order:

1. Each route named `<old-id>-…` is copied as `<new-id>-…`, **with the same secret**. A route
   you named some other way keeps its name.
2. Each repo hook is moved to its new route's URL, and GitHub is asked to **ping** it.
   - **Answered:** the route is proven.
   - **Refused:** the hook is put back, and nothing else is moved.
   - **No delivery seen**, for example on a paused hook: the hook stays on the new route, but
     the old route is kept. Run `migrate` again later to prove it and finish.
3. The default state directory, today's turn counts and the route intent record move to the new
   id. The new loop file is written and the old one removed.
4. Each old route is removed once every hook that pointed at it has answered on the new one.

`--admin-token` names the mapped login whose token may edit the repo hooks, as for `apply
--hooks`. Running it again after it finished is a no-op.

## The Desktop page

In Hermes Desktop, the plugin adds a **Diaktoros** page (with a sidebar entry) and a status-bar
summary such as `loop: 1 running · 1 held`.

A passive **Diaktoros · Right now** pane initially docks below the conversation as a 200px strip
(the normal Desktop pane chrome still lets you rearrange it). It mounts independently of the page,
shows the selected loop's live and recent failed runs with their host-written reasons, and scrolls
inside its bounds. Its local loop selector does not navigate or change the page's selection.
Unreadable configurations stay named; a selected loop that disappears is marked not read rather
than silently replaced. The pane has no mutation controls, row links, toasts or auto-navigation.
It shares the page's validated `/now` React Query key and 30-second polling/freshness configuration,
not a separate polling mechanism. A shared authenticated invalidation socket now
accelerates REST refreshes after local ledger/state/config changes; the existing
polling remains the fallback on OAuth, disconnected or unsupported sockets. Events
carry no view data and never turn an unavailable read into verified empty. Last-good timestamps, loading, unavailable,
verified-empty and stale disclosures remain visible; missing ledgers are unavailable, not empty.
Successful reads expire after 60 seconds even if refresh hangs or the wall clock moves backward.
Run reasons describe the recorded state, not new remedies inferred by the pane. Use the page for
pacing detail, drawers and confirmed actions. Diaktoros never merges.

The page shows:
- **Right now:** the live usage-window state first — any seat account held for its reset, and
  today's turns (and tokens, where a budget is set) per seat against their caps, from the same
  read [`status`](commands.md#status) prints. Then every queued, running, held or uncertain run,
  and the last day's failures, each with a short host-written reason (a hold's own line, such as
  `held: waiting for CI …`). A run's held line is a **snapshot** from the moment it was held and
  can name a reset that has already passed, which is why the live strip sits above it. It never
  shows a turn's output. **Click a row** for that PR's drawer: the live window state again, one
  line per stuck run saying what would move it (the host's own text, the same `next_step` line
  `retry` names), the reviewer verdicts and adjudicator rulings this ledger recorded, that PR's
  turns (the newest 50, with the title and a line saying how many older ones are not shown
  when the PR has more; rulings are cut the same way), and the cap marker when a head has
  spent its cap. The live round count comes from
  GitHub's reviews — a person's verdict counts too — so the drawer says what the ledger holds
  instead of guessing at it; `explain --pr N` has the GitHub half. Two more sections answer what
  the ledger cannot: **Findings** lists the reviewer's numbered findings across rounds (#475) with
  their state — open first, resolved dimmed — and **Why the loop did or did not react** is the
  gate's own decision log (#209): the gate, the event and action, who sent it, and its reason
  (`declined: issues labeled maintainer: not the fix label`), which is the answer to "why did
  nothing happen when I labelled that?". **Retry** sits on a row the host says `retry` would
  re-arm — a failed, waiting or push-policy-cancelled run that never wrote. It always asks first,
  naming the loop, PR, seat and head it will re-arm, and then shows the host's own answer and the
  state it read back. A run that may have written (an uncertain run, or one with a receipt, push
  intent or ruling on record) has no button: inspect the PR and reconcile from the CLI. A moved
  head is refused rather than retried, and a fixer run is only offered while unattended fixer
  pushes are on, with the command that turns them on as its hover reason..
- **Local loop records:** bounded local artifacts for the selected loop, served by
  `GET /records?loop=...` with no GitHub calls or mutations:
  - **Unresolved gate failures:** recorded failures to finish (including repository-matched
    fallback records), not quiet gate declines. Safe failure kind, gate, PR/head, attempts,
    re-drives and first/last timestamps are returned; the page shows the last timestamp.
  - **Broker write audit:** role, operation, PR/head, time, phase and outcome. This is not a
    complete authorization/refusal log. An attempt is not proof of publication; reconciled
    `unchanged` remains distinct. Newest appends first from the bounded JSONL tail.
  - **Main CI · local memory:** remembered green and red-notice heads plus configured required
    check names. No current CI conclusions, age or chronology are recorded by this artifact.
  - **Observer · local feed:** event, optional target/head, recorded status, attempts and time.
    Digests may have no single target; report/main notices may use zero. `delivered` is recorded
    delivery memory, not independent proof that a notification arrived. Queued, digesting,
    pending, failed and uncertain remain distinct. Bodies, destinations and delivery IDs are
    withheld; this is not the full observer message history.
  - **Watchdog · local memory:** recorded sweep time, retained PR heads/per-head history with
    first-observed (possibly unknown) and last-seen times, and stall cooldown stamps with safe
    reason categories. These are neither live running/health nor current stall verdicts.
    Cooldown keys contain only seven head characters and are not joined to current heads.
    Queue/lock alert keys, arbitrary branch names and watchdog log prose are omitted.

  Each section distinguishes missing, unreadable and successfully read empty memory. Reads
  admit regular files only, without following any symlink component or blocking on FIFOs;
  artifacts are limited to 256 KiB and returned sections to the newest 50 rows. Observer entries
  and each watchdog map admit at most 1,000 entries. At 50 rows the page warns that older rows
  may be omitted; no total/full-history claim is made. Malformed complete JSONL lines and
  unfinished appends are unreadable, not silently empty. Raw payloads, errors, credentials,
  private prose and host paths are not returned. A successful snapshot is not an atomic
  cross-file read. Loading, unavailable and retained stale data remain explicit; a 60-second
  elapsed deadline expires successful reads. The records query shares the server cache/socket
  invalidation and 30-second polling fallback. The first displayed loop stays selected through
  discovery reorder/removal; without a readable selected identity the page says not read and
  offers no records Retry. No other surface's Retry behavior changes.
- **Seat turns** for the last 24 hours, 7 days or 30 days: the run ledger's half of
  [`stats`](commands.md#stats).
- **Health:** [`doctor`](commands.md#doctor)'s checks for this loop. It opens **offline**: the
  gateway probe, the hooks, token expiry and scopes, and the labels report themselves unprobed
  rather than reaching out. What is broken comes first, then what could not be read — an unprobed
  check is not a passing one — and only then is there nothing to list. **Probe GitHub** runs the
  full set with this loop's own tokens, from this machine only (a forwarded or non-local caller is
  refused); *Re-check* re-runs whichever set is showing, and *Show all N* lists them. Seconds
  either way (7.8s probed vs 5.1s offline on the live install), so it is read on demand.

The status chip raises a toast when the number of runs waiting on you **rises**. A reload is not
news, so the first read of a session only records the baseline. Three palette rows (⌘K) open the
page, refresh its data, and re-run the health checks.

It refreshes every 30–60 seconds. Both halves ship **off**:
1. **The Python half**, which serves the page's data: add `diaktoros` to
   `plugins.enabled` in `config.yaml` and restart the gateway.
2. **The Desktop half:** turn on **Diaktoros** under **Capabilities → Plugins**.

The backend (`dashboard/plugin_api.py`, mounted at `/api/plugins/diaktoros/` behind the
gateway's own auth) has ten GET routes: `loops`, `stats`, `now`, `overview`, `pr`, `health`, `settings`, `hooks`, `actions` and `records`.
They read the loop's config, the run ledger and — for `health` — the installation's own files,
without mutating loop state. `settings` returns the plugin form's schema and effective values plus
each loop's effective seats, budgets and flags, so the Desktop form can render what a loop runs
as without applying anything; a token field is a path and the file is never opened.
Its ledger write is `POST /retry` with `{loop, pr, seat, head}`, the Retry button's confirmation:
it re-reads the ledger, runs the same `retry_action` / `Supervisor.retry` path `hermes dk retry`
runs, answers 409 with the host's own reason for anything that retry refuses (including a head
that moved since the operator confirmed), and never calls GitHub or reads a token.
`GET /hooks?loop=<id>` reads GitHub using `arm`'s default token resolution (the loop's
mapped reader login, which must have hook-admin permission), returning armed, paused or unknown. Missing, mixed or
unreadable hooks never mean paused. Eligibility is direction-specific: a complete, verified
owned all-active set offers **Pause** even with wrong event/content type, URL slash or route
binding; its public reason explicitly warns that wiring needs repair. Here armed describes
observed hook activity, not healthy delivery or wake readiness. A paused miswired set does not
offer **Resume** until its wiring is repaired. Only a fresh, eligible read offers a control.
`POST /hooks` takes `{loop, action, confirmation}` (`action` is `pause` or `resume`). The
confirmation names the loop and consequences; the host re-reads loop identity, route targets
and hook identity/wiring/state, rejecting a stale confirmation with 409. It calls the same
`arm_action.perform` as `hermes dk arm --pause` / `hermes dk arm`, including resume ping,
and reads back the resulting state. A partial action is not rollback or success: the answer
reports the observed state with `ok:false` and withholds controls until a fresh read.
Pausing stops webhook delivery, not turns already running. Sweeps may still recover events;
this is not a global stop or drain. Tokens, paths and raw transport diagnostics never reach
this UI. Both hook routes use gateway authentication; unlike `health?probe=1`, they are not
local-only. No merge control exists.

### Confirmed one-shot actions (#644)

The **One-shot actions** panel looks up a PR independently of the live-run table. Choose
Re-review or Escalate and enter a PR number; Drain queue instead selects the reviewer/fixer
seat (the seats the current drain core supports). Preview is read-only and returns the host's
per-target eligibility; the page never infers it from a run state.

`GET /actions?loop=<id>&action=escalate&pr=<n>` returns an opaque confirmation bound to
loop config, the exact live PR/head, effective verdicts, escalation marker and ledger rows.
`POST /actions` accepts exactly `{loop, action, pr, seat, confirmation}`; `seat` is null for
PR actions, `pr` is null for drain. A stale/unavailable offer returns 409 rather than acting on
whatever is newest. All routes use gateway authentication. Tokens stay on the host.

**Escalate** parks this head against further review/fix and requests an adjudicator ruling,
spending its configured turn budget. A queued review/fix is retired when the ruling worker
claims it. The confirmation names loop, PR and head, these consequences and the equivalent
CLI command. The backend uses the same selector/operation as `hermes dk escalate`, rechecks
the offer under the existing breach reservation lock and reads back the exact operation's
marker. Verified means parked awaiting adjudication, **not** a completed ruling. Delivery
pending is partial; missing/changed/unreadable read-back is unknown. Neither is success, and
the consumed preview offers no follow-up control without another safe read.

**Re-review and Drain are currently refused in Desktop.** A re-review would ask the existing
reviewer gate for a fresh review (not erase a verdict or bypass its cap); its script currently
owns process-global input/signals/context, unsafe inside the threaded Desktop host. A drain
would start queued work for a seat and may lead to GitHub writes; its whole-seat watchdog
entry point cannot bind an exact confirmed queue snapshot. These are disclosed refusals, not
inert buttons or weaker gates. The CLI still calls those core entry points through the shared
host owner; `review --another-round` remains CLI-only. Drain now exits 2 with an unconfirmed
outcome instead of claiming success merely because the watchdog returned. Inspect `explain`
and the queue before asking again. Escalation also refuses corrupt marker stores and unknown
ledger outcomes through either caller. No action grants merge authority.

`health?probe=1` also reads a credential: the loop's own tokens, to read GitHub for
the hook, expiry and scope checks the offline pass leaves unprobed. It is refused unless the caller
is on this machine — a non-loopback peer, or any forwarding header at all (`Forwarded`,
any `X-Forwarded-*` such as `-For`, `-Host` or `-Proto`, and `X-Real-IP`; present is what counts, so an empty one refuses
too) gets 403 — because the dashboard is the surface Caddy exposes off-host. GitHub-side numbers stay with `stats --github`.
