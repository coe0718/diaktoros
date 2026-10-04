# Troubleshooting

Diagnose before re-driving. Preserve the loop ID, repository, PR/issue number, head/base,
delivery ID, exact error, run ID and worker PID. Keep secrets, private diffs and review
bodies out of shared incident reports. **A lost response or failed process is not proof
that no GitHub write happened.** Never delete safety state to bypass a refusal.

## Contents

- [First checks](#first-three-commands)
- [Not a hermes command](#review-loop-is-not-a-hermes-command)
- [Setup stopped](#setup-stopped-before-the-loop-was-live)
- [PR did not start](#i-opened-a-pr-or-requested-a-review-and-nothing-happened)
- [Fixer did not start](#changes-were-requested-but-the-fixer-never-ran)
- [Cap without ruling](#the-cap-was-spent-and-no-ruling-came)
- [Runtime](#runtime-file-problems)
- [Doctor versus selftest](#doctor-is-all-green-but-selftest-fails)
- [Failed/waiting turns](#a-turn-failed-is-waiting-or-was-retried)
- [Uncertain runs](#a-run-says-uncertain)
- [Duplicate notices](#duplicate-telegram-messages-or-every-stall-twice)
- [Resolver errors](#claude-code-is-not-installed-in-resolver-output)
- [Identity refusals](#token-and-identity-refusals)
- [Gate failures](#a-gate-crashed-or-timed-out)
- [Routes changed](#routes-were-overwritten-or-changed-under-you)
- [Disk](#disk-usage-keeps-growing) and [uninstall](#after-uninstall-things-are-left-behind)
- [Triage](#an-issue-opened-and-nothing-was-labelled) and [issue fixes](#a-fix-label-was-applied-and-no-pr-came)
- [Attribution](#the-loops-posts-carry-a-footer-or-trailer-you-did-not-expect)
- [Escalation evidence](#still-stuck)

Replace quoted angle-bracket placeholders, including brackets, with your values.
`--loop` selects the saved loop ID; `--pr` selects a numeric PR number (issue number for
issue-run retry). `--admin-token` names an already mapped login, never a secret.

## First, three commands

```bash
hermes review-loop doctor --loop "<loop-id>"
hermes review-loop explain --loop "<loop-id>" --pr "<pr-number>"
hermes review-loop trace --loop "<loop-id>" --delivery "<delivery-id>" --admin-token "<hook-admin-login>"
```

| Check | Question answered | Limit |
| --- | --- | --- |
| Doctor | Is the install wired correctly? | Credential-file checks do not prove token scopes |
| Explain | Why is this PR not moving now? | PRs only; depends on readable live state |
| Trace | What would this delivery do now? | Copied-home gate dry run; not original-time reconstruction; no issues events |

Trace's `--delivery` takes numeric delivery ID or `X-GitHub-Delivery` GUID; hook-admin
permission may be needed to retrieve it. A local payload is an alternative:

```bash
hermes review-loop trace --loop "<loop-id>" --payload "<payload-json-file>" --event pull_request --route "<reviewer-route>"
```

`--payload` is a JSON-object file, `--event` is `pull_request` (default) or
`pull_request_review`, and `--route` selects the intended route. None of these trace
examples starts a production model or writes GitHub. Use status for local queues/runs:

```bash
hermes review-loop status --loop "<loop-id>"
```

## "'review-loop' is not a `hermes` command"

**Symptom:** `hermes review-loop …` says it is not a command, although `hermes plugins list`
shows the plugin enabled and `hermes review-loop --help` works.
**Cause:** the command line contains `-p NAME` or `--profile NAME`. `hermes` takes those from
anywhere on its command line, after the subcommand too, and runs in that profile's Hermes home,
where the plugin may not be enabled (or, worse, a different loop config is). No review-loop
command uses those spellings: the triage seat's profile is `--triage-profile`, and
`models` takes `--profile-name`.
**Action:** remove the `-p`/`--profile` pair and use the command's own flag.

## `setup` stopped before the loop was live

**Symptom:** wizard stops at a prerequisite/check/hook/scheduler step.
**Cause:** setup is staged; earlier config/routes/hooks may already exist while later
runtime validation, scheduling or arming failed.
**Action:** keep the named failed step and inspect status, doctor and repository hooks.
Fix that prerequisite, then resume setup or the named CLI step. Do not recreate hooks
or another cron job just because the wizard exited nonzero. Verify hook readback and
signed pings, not only the presence of a route.

## I opened a PR or requested a review, and nothing happened

| Symptom/cause | How to confirm | Action |
| --- | --- | --- |
| Hooks paused/absent | Doctor and repository hook listing | Install missing hooks, then arm with mapped hook-admin account |
| Draft, wrong base or untrusted author | Explain live eligibility | Make intended PR eligible; don't broaden author policy to troubleshoot |
| Request came from an unaccepted account | Trace gate outcome | Use an allowlisted sender and configured reviewer |
| Pushed a commit but no review request | GitHub requested-reviewers and delivery action | Explicitly request the configured reviewer |
| Missing/invalid private runtime | Doctor/selftest; gate error | Fix runtime, inspect durable work, then drain the reviewer queue; redeliver only if no work was admitted |
| Same-head verdict/run already exists | Explain and ledger | Wait, answer current verdict, or inspect uncertain run; don't create duplicates |
| Event never reached gateway | GitHub Recent Deliveries response, gateway log | Fix URL/profile/origin/signature; route name alone is insufficient |
| Base situation changed | Explain stacked/base transition | Follow fresh-review requirement; old same-head verdict may not count |

```bash
hermes review-loop apply --loop "<loop-id>" --hooks --admin-token "<hook-admin-login>"
hermes review-loop arm --loop "<loop-id>" --admin-token "<hook-admin-login>"
```

`--hooks` ensures repository hooks but apply can also apply configured settings; review
those settings first. Arm enables all loop hooks and verifies signed pings. A successful
HTTP webhook response can still mean the gate deliberately ignored the event.

## Changes were requested but the fixer never ran

**First cause to check:** unattended fixer pushes are off (default). Explain/status
report a queue hold without a model run. This is expected policy, not an outage.

Choose a manual fix plus a fresh review request, or read the residual race and opt in:

```bash
hermes review-loop fixer-push --loop "<loop-id>" --enable --acknowledge-pr-race
hermes review-loop drain --loop "<loop-id>" --seat fixer
```

`--enable` authorizes host policy; `--acknowledge-pr-race` accepts non-atomic PR metadata
versus ref publication, **not GitHub owner consent**. `--seat fixer` selects that queue;
drain rechecks live eligibility and cannot override quarantine or cap. Existing queue
holds can start after opt-in; a ledgered policy-cancelled run needs operator retry after
opt-in, not redelivery. See [push policy](operations.md#first-run).

If enabled, inspect whether the review is from the accepted reviewer, is the effective
latest changes-requested verdict at this head, and whether the cap is already spent.
Then inspect failed/waiting/uncertain runs. A verdict on an older head is not current
permission. Do not treat a failed broker push as a harmless retryable model failure.

## The cap was spent and no ruling came

**Cause/action:**

- No adjudicator route: the marker intentionally leaves the decision to you. To add one
  later, follow the loop-file block and `apply --recreate-routes` instructions in
  [setup](commands.md#setup); do not reset the cap marker.
- Delivery pending: watchdog retries eligible adjudicator enqueue; a marker is not
  confirmation that the model started.
- Adjudicator running: allow its own turn budget, not the reviewer's shorter budget.
- Ruling already recorded: inspect the operator outbox and ledger `rulings`; optional
  PR comment needs an adjudicator write identity. Observer ruling notices omit reason text.
- Draft/closed/moved/retargeted PR or new effective approval: live eligibility can
  supersede a queued adjudication.
- Failed/uncertain turn: follow run recovery below. Do not reset the marker to force
  another ruling. ACCEPT/REJECT/RESPEC never automatically merges the PR.

## Runtime file problems

The production file is `$HERMES_HOME/review-loop-runtime.json`, private mode `0600`.
Both gateway and CLI must address the same home. Never place credentials in this file.

| Symptom | Cause | Action |
| --- | --- | --- |
| No file, gate says isolated worker unavailable | Setup incomplete/wrong Hermes home | Run setup for the intended install; inspect existing ledger before redelivery |
| Mode rejected | File too broadly accessible | Restrict permissions using normal file administration; rerun selftest |
| Runtime/source/venv path rejected | Moved checkout or changed bundled Python generation | Rerun setup and inspect detected runtime against selected venv |
| Models resolve in host shell, not worker | Seat profile missing/unresolvable or inconsistent runtime | Inspect configured seat profile/provider/model; don't invent runtime model keys |
| `ContainmentUnavailable: bubblewrap (bwrap) is not installed` | The worker found no `bwrap` in `/usr/sbin:/usr/bin:/bin`; the turn was refused before anything started, never run unsandboxed | Install the bubblewrap package, confirm `selftest` passes `bwrap:installed`, then `retry` the run. It is not retried automatically |
| `RealHomeError` or `RealNetworkError` | Harness-only test guard is armed (variable plus sentinel) | In a real gateway, find/unset leaked test settings and restart; in tests, fix the fixture without disabling guards. See [tripwire recovery](operations.md#the-loop-stops-with-realhomeerror-or-realnetworkerror) |

```bash
hermes review-loop selftest --loop "<loop-id>" --no-model
```

`--no-model` skips the tiny paid completion but still exercises containment/runtime.
A missing runtime can prevent enqueue entirely; a worker spawn failure after commit
can instead leave a pending row. Determine which before asking GitHub to redeliver.
After fixing the runtime, request scheduling for already queued reviewer work:

```bash
hermes review-loop drain --loop "<loop-id>" --seat reviewer
```

Drain rechecks eligibility; it does not bypass an uncertain run or a queue hold.

## doctor is all green but selftest fails

**Cause:** doctor validates wiring and prerequisites; selftest exercises more of the
isolated launcher/inference/broker boundary. The invoking shell can also differ from
the gateway in paths, permissions, provider auth or size-limit overrides.
**Action:** preserve the exact selftest stage, align home/runtime and launch environment,
and resolve model identity/backend requirements. Test a PR authorization check:

```bash
hermes review-loop selftest --loop "<loop-id>" --pr "<pr-number>"
hermes review-loop selftest --loop "<loop-id>" --pr "<pr-number>" --live-turn
```

`--pr` adds live reviewer-authorization dry run. `--live-turn` runs a real isolated
reviewer conversation whose verdict is printed, never posted; model/build costs apply.
No green local check overrides later live PR eligibility.

## A turn failed, is waiting, or was retried

Read the reason and `retry_at`, not just state. Supervisor details include bounded output
tail; worker stderr is beside the ledger in `.workers.log` with rotation.

| Reason | Meaning | Action |
| --- | --- | --- |
| Transient pre-write failure | Safe bounded retry/backoff | Fix service/read failure; allow next armed sweep |
| Retry limit reached | Automatic budget exhausted | Fix cause, then operator retry if write-free |
| Killed at turn budget | Whole turn exceeded recorded budget | Diagnose build/model time; raise appropriate seat budget before safe retry |
| `turn exited with status 1`, output tail ends in a plan or summary with "no write" | The agent used its step or model-call cap before writing, or ran long tests until the budget; each retry starts from scratch | Read the tail. A task too large for one turn needs a narrower issue or finding; otherwise raise the seat's `--fixer-max-steps` or turn budget, then `retry` |
| 429 with reset time | Usage window closed | Wait until named reset; no retry spent |
| Bare 429 without reset | No reliable window information | Ordinary failure/backoff, not guaranteed reset scheduling |
| Daily cap reached | Starts delayed to local midnight | Wait or explicitly change cap; don't erase pacing ledger |
| Policy cancellation | Push admission absent/revoked | Opt in only if intended, then explicit retry |
| Superseded cancellation | PR/head no longer eligible | Fresh eligible head/event, not old-run replay |

```bash
hermes review-loop retry --loop "<loop-id>" --pr "<pr-number>" --seat reviewer
```

`--seat` restricts the candidate seat; omit to consider eligible problem runs at the
newest offerable head. Retry resets automatic retry accounting and uses current budget.
A `failed` state alone does not establish replay safety: host write evidence prohibits
retry even when the process exited unsuccessfully. See [operations recovery](operations.md#when-an-isolated-run-fails).

## A run says "uncertain"

**Symptom:** lease/launch outcome unknown, ambiguous review claim, push intent or
post-write quarantine. Later review/merge hints may be held even if a branch looks right.
An uncertain run keeps its seat slot until reconciled; at concurrency one it can block
other work on that seat.
**Cause:** a worker may still be active, or an external write may have landed before
its response/receipt was lost. Known accepted pushes can still require post-write checks.
**Action:** preserve evidence, inspect exact external state, establish no worker remains,
then reconcile. Never retry, redeliver repeatedly, delete locks or reset the ledger.

```bash
python -m review_loop.run_supervisor status "<ledger-path>"
python -m review_loop.run_supervisor reconcile "<ledger-path>" "<run-id>" --reason "<inspection-summary>" --acknowledge-no-live-worker
```

Run in the installed module environment or plugin checkout. `<ledger-path>` is the SQLite
file; `<run-id>` is exact, not PR number. `--reason` records inspection of reviews,
comments, remote refs, PR creation and review requests (nonempty, at most 512 characters);
the explicit acknowledgement is mandatory. A live/inaccessible PID or missing launch
intent blocks release. Reconcile releases the uncertainty
hold but does not replay the old turn or make it retryable. A new head gets fresh work.
Observer uncertainty is different and has no general replay CLI; see [observer](observer.md).

## Duplicate Telegram messages, or every stall twice

**Cause:** multiple cron jobs, operator outbox plus observer notices, repeated cooldown
stall warnings, or genuinely different heads/events. Observer keys deduplicate the
same transition; ordinary recurring stalls can be new notices, while push-off holds
are one observer notice per head.
**Action:** inspect scheduled jobs and exact event/receipt identity. Keep one shared
watchdog for all loops rather than a second job per loop. Do not delete observations
or safety state to test deduplication. Two independent delivery paths are not necessarily
a defect. To keep operator stall warnings but omit the observer's duplicate stall path,
set an explicit event list without `stall` (a blank list means all events):

```bash
hermes review-loop set --loop "<loop-id>" --observer-events opened,handoff,verdict,approved,escalation,ruling,closed
```

An uncertain send is never permission to manually replay it blindly.

## "Claude Code is not installed" in resolver output

**Cause:** the resolver starts with sanitized `PATH=/usr/bin:/bin`; a Hermes CLI probe
can print this to stderr even when ordinary API-key or supported OAuth model resolution
succeeds. The message alone is not a seat failure and does not require installing Claude Code.
**Action:** inspect the actual resolver result and configured provider/model, then run
selftest. Only the `claude-subscription-directsdk-experimental` backend here truly needs
the native executable; its resolver extends PATH to `/usr/local/bin` and the user's
`.local/bin`. If that backend fails, check its supported CLI/package in the host runtime.
Do not replace real CLI/module/provider names with guessed aliases. DirectSDK usage-limit
failures do not provide the same HTTP reset information as the inference-proxy path,
so a subscription limit may appear as ordinary failure rather than a timed window hold.

## Token and identity refusals

| Symptom | Likely cause | Action |
| --- | --- | --- |
| File missing/empty/mode refusal | Mapping or filesystem problem | Correct mapped private path; don't paste token into CLI |
| Principal mismatch | Token belongs to another login | Replace mapping with that account's own credential |
| Reader used as writer | Identity boundary violated | Restore separate write principal |
| 401/403 | Expired token, insufficient scope/access, SSO or hook-admin permission | Inspect actual GitHub refusal and rotate/grant only needed permissions |
| Missing hook admin mapping | `--admin-token` names unmapped account | Map a private file first, then retry administrative operation |

Doctor file checks alone cannot distinguish scopes, repository selection or SSO.
See [accounts](accounts.md). The watchdog alerts 401/403 immediately; other outages
need repeated failed sweeps and re-alert on cooldown. Read failure means unknown state,
never an empty review list or permission to approve.

## A gate crashed or timed out

**Symptom:** GitHub delivery received an ignored/200 response but no work, or watchdog
reports a gate crash/read failure/budget exhaustion.
**Cause:** a webhook transport response does not encode every script failure. Gate
budget is bounded and must fit the gateway's script timeout. Missing/bad payload,
failed read or busy state can stop admission before any model runs.
**Action:** inspect gateway log, gate-failure ledger and stored payload. Loop failures
live in `<state_dir>/gate-failures.json`; failures without a resolvable loop can use
`$HERMES_HOME/state/review-loop-gate-failures/`. Preserve corrupt-copy evidence rather
than discarding it. If the payload was too large/not retained, use GitHub Recent Deliveries.

Only reviewer/fixer failures are automatically re-driven, at most three times, while
eligible and armed. Adjudicator/triage failures are reported but not re-driven that way.
A paused loop does not re-drive. A missing payload/script or spent retry allowance
requires operator investigation. Before redelivery check for durable/uncertain run
rows, because a crash can occur after enqueue. The watchdog itself is budgeted; zero
cron exit status does not establish a successful sweep.

## Routes were overwritten or changed under you

**Cause:** native gateway subscription edits may not take the plugin's lock. Optimistic
writes and recorded intent mitigate but cannot eliminate every last-syscall race.
**Action:** inspect doctor route contract/intent and use its repair recommendation:

```bash
hermes review-loop doctor --loop "<loop-id>" --repair
```

`--repair` restores eligible locally recorded intent; it is not authority to overwrite
another install's route. Armed watchdog sweeps self-heal recorded routes/shims with
the same secret. Use supported set/apply/uninstall workflows for deliberate edits,
or self-heal may restore your manual change. A durability error after publication
means the new registry may be visible but not crash-durable: read actual state before
retry. For observer destination mismatches follow [observer binding rules](observer.md#privacy-and-destination-binding).

## Disk usage keeps growing

**Cause:** concurrent isolated builds, checkout/scratch/cache limits, incomplete closed-PR
cleanup, retained artifacts, or operator uncertainty that intentionally prevents release.
**Action:** inspect which directory/filesystem grows, active worker state and cleanup
eligibility before changing caps or deleting anything.

```bash
hermes review-loop cleanup --loop "<loop-id>" --pr "<pr-number>" --dry-run
```

`--pr` limits cleanup to one closed PR; omit to inspect all known closed PRs. `--dry-run`
prints planned cleanup without deleting. Review allowed roots and artifacts before
running without it. A `closed` observer notice only says cleanup was attempted.
Never clean an active work directory or supervisor safety ledger to free space.
Overrides belong in the gateway environment, not merely selftest's shell; see
[storage limits](operations.md#sandbox-size-caps-the-two-writable-mounts).

## After `uninstall`, things are left behind

**Cause:** uninstall does not reverse GitHub comments/reviews/commits/PRs, delete credential
files, erase shared resources owned by another loop, or necessarily purge custom state.
Administrative hook deletion can fail before local removal.
**Action:** inspect the exact result and remaining hooks/routes/jobs. Confirm remote
hook deletion instead of assuming a success-looking local message covers it. Use the
uninstall options in [commands](commands.md); inspect purge scope before opting in.
Retain safety history needed to reconcile external writes. Do not purge another active
loop's shared ledger or home.

## An issue opened and nothing was labelled

Read [issue setup](issues.md#turn-on-issue-triage) and inspect settings:

```bash
hermes review-loop triage --loop "<loop-id>"
```

| Cause | Confirmation | Action |
| --- | --- | --- |
| Triage off or issues hook paused/missing | Settings, doctor, Recent Deliveries | Enable/install/arm explicitly |
| Author not allowlisted | Payload author and `triage.authors` | Keep rejection or deliberately replace author list |
| Not action `opened` | Delivery action | Edits/reopens/relabels never start triage |
| Already has configured label | Live labels | Expected skip; triage only adds |
| Closed/PR/changed author before execution | Live issue and ledger error | Expected eligibility refusal |
| Runtime/enqueue failed | Gate log; run may be absent or pending | Fix cause; inspect durable state before redelivery |
| Daily cap/model failure | Run error/deadline | Wait or repair proven pre-write failure |
| Model selected no labels and no comment | `triage_results` state `nothing` | Valid no-write outcome; comment-only triage is not `nothing` |
| Write denied/uncertain | Result error/stage | Fix authorization or reconcile; never blindly replay |

Use the read-only queries in [issues](issues.md#inspect-results). There is no issues trace
or issue explain. A safe failed pre-write triage run can use:

```bash
hermes review-loop retry --loop "<loop-id>" --pr "<issue-number>" --seat triage
```

Here `--pr` is the issue number; `--seat triage` restricts the retry. Recorded write
results and uncertain outcomes cannot be re-armed. Triage has no observer notices.

## A fix label was applied and no PR came

| Cause | Confirmation | Action |
| --- | --- | --- |
| Wrong label/sender | Compare event with fix label and maintainers | Trigger only from an authorized sender |
| Pushes off | Settings/status/doctor | Choose manual work or explicitly accept policy risk |
| Issue author/state/label no longer qualifies | Live issue | Correct intended eligibility; don't widen boundary just to run |
| Base unreadable/no runtime | Gate error and ledger | Fix read/runtime; check whether any work committed |
| Same issue/base already admitted | Run key `issue-fix` | Label toggling cannot create duplicate work |
| Existing `review-loop/issue-N` branch | Remote ref | No overwrite allowed; inspect ownership/prior outcome, not automatic deletion |
| Fix not possible | Issue comment / `issue_fixes` | A cannot-fix comment is supported instead of PR |
| Push/open/request partially completed | Result stage and remote branch/PR/request | Preserve evidence and reconcile; never repeat sequence blindly |

Use [issue result queries](issues.md#inspect-results). A safe failed pre-write turn can
use `retry --loop "<loop-id>" --pr "<issue-number>" --seat issue_fixer`; each option has
the same meaning as the triage retry above. An uncertain branch push is not fixed by
deleting the branch and reapplying the label. If the event was rejected before enqueue,
only after fixing the prerequisite and confirming no durable/possible write should
an authorized maintainer reapply the label or redeliver the event. For an intentional
new attempt blocked by an existing `review-loop/issue-N` branch, first inspect the
prior run, branch, PR, comments and review request. Only when that inspection establishes
a safe new attempt (not an ambiguous or already published result), deliberately remove
the inspected remote branch so the required absent-ref lease can pass, then reapply the
label for eligible fresh work or retry an eligible pre-write run. Do not blindly delete
remote branches: deletion neither erases broker records nor makes recorded work retryable.
See [issue recovery](issues.md#when-it-doesnt-work).

## The loop's posts carry a footer or trailer you did not expect

**Cause:** host attribution is on by default and is not controlled by model prose.
**Action:** keep it for transparency or change future writes explicitly:

```bash
hermes review-loop set --loop "<loop-id>" --attribution off
```

`--attribution off` disables future footer/trailer addition; `on` restores it. Existing
posts/commits remain. Keep plugin settings consistent before apply. See
[signing](operations.md#what-the-loop-signs).

## Still stuck?

Provide a redacted evidence packet: command/stage, loop ID, repo/number, exact head/base,
delivery ID and response, doctor/selftest error, explain `next:`, relevant run ID/state/error,
broker/result stage, worker PID state and last watchdog sweep. Say which actual GitHub
writes you checked. Exclude token contents, HMAC secrets, runtime credentials and private
review/diff text. Separate confirmed facts from unknown outcomes. See
[architecture](architecture.md) for boundaries and [operations](operations.md) for recovery.
