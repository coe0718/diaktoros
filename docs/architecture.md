# Architecture

> **Current status:** gates never dispatch to a gateway agent — they queue an eligible PR
> event as an isolated turn in the host run ledger and return `[SILENT]`. A worker runs that
> turn credentialless in a bubblewrap sandbox, and its only way out is the host broker
> (`review_loop/broker.py`, `review_loop/broker_ipc.py`). Nothing runs until the private
> runtime file exists (without it a turn is held with its reason) and the repo hooks are
> armed; with both, a reviewer turn posts a real review, and on a loop with an adjudicator
> route a spent cap runs the isolated adjudicator. Unattended fixer pushes stay off per loop
> until `fixer-push --enable --acknowledge-pr-race` — that is not atomic PR authorization.

One rule runs through every part of the loop: **the control plane never guesses.** A fact it
cannot read is unknown, never assumed, and a step whose outcome is unknown is never replayed.

The moving parts are a handful of processes:

- the **gate scripts** (`gate_reviewer.py`, `gate_fixer.py`, `gate_triage.py`), which the Hermes
  gateway runs for each GitHub webhook;
- the **supervisor worker** (`review_loop/run_supervisor.py`), a detached process that claims
  queued turns from the run ledger and launches them;
- per turn, the **inference proxy** and the **broker**, host-side threads of that worker, and the
  **bubblewrap sandbox** the agent runs in;
- the **watchdog**, run by the gateway's cron every 15 minutes;
- the operator's **CLI** (`hermes review-loop …`).

State lives in a few places. Per loop, under `state_dir`: `locks.json`, `pending.json`,
`inflight.json`, `breach.json`, `observations.json`, `watchdog.json`, `stack-transitions.json`,
`review-situations.json`, `github-reads.json`, plus `route-intent.json`, `gate-failures.json`
and `broker-audit.jsonl` ([the files](configuration.md#state-files-per-loop-under-state_dir)).
Host-wide, under `$HERMES_HOME/state/`: the run ledger `review-loop-runs.sqlite` (every
isolated turn, shared by all loops) and the pacing file `review-loop-pacing.json`.

```
GitHub ──pull_request ─────────▶ gate_reviewer.py ─┐
       ├─pull_request_review ──▶ gate_fixer.py ────┼─▶ [SILENT] to the gateway, always
       └─issues (opt-in) ──────▶ gate_triage.py ───┘      │
                                                          ├─ nothing to do: no row, no tokens
                                                          ├─ eligible: a row in the run ledger
                                                          ├─ not now: a hold or queue entry
                                                          └─ cap spent: a breach marker, and an
                                                             adjudicator row only with
                                                             --adjudicator-route

PR closed or merged ──▶ gate_reviewer.py ──▶ cleanup.py   (disk only, no turn)

run ledger ──▶ supervisor worker ──▶ re-reads GitHub, stages the exact head
                     │
                     └──▶ bwrap sandbox (no network, no tokens, no keys)
                              ├── model.sock ──▶ inference proxy ──▶ model provider
                              └── broker.sock ─▶ broker ───────────▶ GitHub (the only writes)

cron (15m) ──▶ watchdog.py ──▶ stalls, queue drains, route self-heal, operator notices
operator ───▶ review-loop explain / trace ──▶ reads GitHub + state files, writes neither
```

The [security model](security.md) describes what each process holds and what the sandbox can
reach.

## The seats

A **seat** is a role, not an agent. Every loop has two working seats, `reviewer` and `fixer`.
The run ledger knows three more, each opt-in:

| seat | woken by | opt in with |
|---|---|---|
| `reviewer` | a PR opened, made ready, reopened, or a review request from the fixer | always on |
| `fixer` | a changes-requested verdict | always on; it can push only after `fixer-push --enable --acknowledge-pr-race` |
| `adjudicator` | the round cap spent with no approval | `init --adjudicator-route` |
| `triage` | an issue opened by an allowlisted author | `hermes review-loop triage --enable` ([issues](issues.md)) |
| `issue_fixer` | a maintainer applying the fix label to an issue | triage with a fix label, plus unattended fixer pushes; runs as the fixer seat |

The two working seats are each bound to a Hermes profile (the run happens as that profile), a
GitHub login (attribution and permissions) and a webhook route (the way it is woken). Both are
declared per loop, so the same install can run a different pair of agents on a different
repository with a different budget.

A seat is a **capacity, not a mutex**, and each seat has its own:

| setting | effect |
|---|---|
| `concurrency` (loop) | the default limit for the reviewer and the fixer |
| `seats.reviewer.concurrency` | the reviewer's own limit (critic: two reviews at once) |
| `seats.fixer.concurrency` | the fixer's own limit (coder: one fix at a time) |

`hermes review-loop set --reviewer-concurrency 2 --fixer-concurrency 1` means: *coder works on at
most one PR at a time, critic reviews up to two at once, everything else queues.* A seat-level value
wins over the loop default; `1` (serialized) is the fallback everywhere. The opt-in seats
(adjudicator, triage, issue fixer) never inherit the loop default: they run one turn at a time.

Above 1, the config still requires a `clone` path. That rule predates the per-turn exports
described under [isolation](#isolation-one-export-per-turn), which do not use the clone; it is
checked against the *effective* value per seat, so a seat-level 2 is caught even when the loop
default stays 1.

The run ledger enforces capacity, and it is keyed by **PR**, so the same PR never runs twice even
with a free slot. Its unique repo/PR/head/seat/turn index means the same turn is never queued
twice. A worker that dies before launch loses its claim when the lease expires, and the row is
claimed again. A worker that dies after launch is quarantined as `uncertain`: it may have
written, so it keeps its PR and seat until an operator reconciles it
([security: the run ledger](security.md#the-run-ledger-and-the-notice-outbox)).

### Who serves a seat, and why the route is part of it

The profile and the login are the seat's **identity**, and they are per loop like everything else:
the same install can run one pair of profiles on one repository and a different pair on another. The plugin
settings carry a per-profile *default* for them (`reviewer_profile`, `fixer_profile`,
`reviewer_login`, `fixer_login`, `adjudicator_profile`); a new loop starts from it, and an existing
loop only moves when `apply --loop <id>` pushes it. Blank means *not set here* — never "forget what
this loop uses".

A route is bound to a profile by its URL: `/webhooks/<name>` is the launch profile, and
`/p/<profile>/webhooks/<name>` is every other one. So an identity change is **also a route change**,
and `apply` treats it as one staged operation: validate (the profile exists, the login is in the
allowlist, the two seats share no profile/login/token file, every token reference resolves, the route
is not another loop's and still runs this role's gate script) → write the loop config → rebind
exactly the routes whose profile moved → read the registry back and report. Only the routes this loop
owns are touched, and a rebind keeps the route's own secret, so unrelated routes and their secrets
survive.

Two consequences worth stating plainly:

* **A seat in flight is not rewritten underneath itself.** `apply` refuses an identity change while
  the seat it would move has a live run, and `--while-busy` is the explicit override — the run that
  is already out finishes under the identity it started with, and `status` is where that shows.
* **The config and the route can disagree, so `status` prints both.** `reviewer widgets-review → critic
  (ok)` is the check; `MISMATCH — hermes review-loop apply --loop widgets` is the loop that would
  run as the old agent while every config file claims otherwise.

Existence, allowlist and credential checks run for the roles an operation *writes*, not for every
role in the file: a loop created before the checks existed must not start failing because someone
tuned its `cap`. The combination checks (distinctness, credentials, route ownership) always run,
because the unsafe shape is the combination.

### One PR, one seat — and who frees it

Per-seat capacity answers *how many PRs a seat may hold*. A second, stricter rule sits under it:
**one PR is held by one seat at a time.** A review must never run against a PR the fixer is mid-fix
on, and a fix must not start on a PR under review.

Both rules are **enforced by the isolated run ledger**: a gate only enqueues a turn, and a worker
claims a pending row only while its seat has capacity and no other run occupies the PR. The seat
claim in `locks.json` (and the head's in-flight mark) is the *visible* copy of that occupancy: the
isolated worker writes it when its run launches — with the run's own budget, so its TTL fits the
turn — and removes it when the run ends. A run that ends `uncertain` keeps its claim until an
operator reconciles it; the reconciliation frees it. `explain`, `status`, the queue drain and the watchdog's "that run died"
report read the claim; none of them can start or stop a turn.

The gates' release paths below also free a claim, and are what end the *PR's* turn for the other
seat. The loop sees events, not process exits, so it uses the events that already mean a turn is
over:

| signal | what it ends |
|---|---|
| `review_requested` from the fixer | the fixer's turn — the push-then-ask handoff |
| a verdict at the current head (approve **or** changes-requested) | the reviewer's turn |
| `ttl_min` | a run that died without either. The backstop, not the mechanism. |

That is also why **order matters inside each gate**: the gate frees the other seat *before* it
enqueues its own turn, so a handoff never waits on a claim the handoff itself ends:

```
observe the peer's handoff → free the peer's claim → enqueue own turn (the worker claims at launch)
```

An approval is the case worth naming: the fixer has nothing to do on an approved PR, but the
reviewer's slot is still held, and a slot that leaks for `ttl_min` on a busy repo is the difference
between ten review slots and nine. So the approval path frees the reviewer and drains the queue
without waking anyone.

## Isolation: one export per turn

Two runs must never share a working tree: a review builds, runs tests and may edit files, and two
runs in one checkout would corrupt each other's results and publish **wrong verdicts**.

So every turn gets its own tree, and nothing is shared between turns:

- the worker creates a private temporary root for the turn, `<state_dir>/isolated-runs/turn-*`
  (mode 0700), and removes it when the turn ends;
- it stages an **exact-head export** into it (`trusted_turn.run_turn` → `trusted_fetch.stage`):
  one GitHub tarball of the commit under review, fetched with the read token and bounded
  (at most 10,000 files and 100 MiB). The export carries no `.git`, no remote and no credential;
- the sandbox gets that export at `/work` — inside a size-capped tmpfs for the reviewer and fixer,
  read-only for the adjudicator — so edits and build output never reach the host filesystem;
- the sandbox holds **no credentials**. The fixer cannot push from its tree: it hands the broker a
  manifest of whole files, and the broker publishes them as one commit with an exact-SHA lease
  (`review_loop/safe_push.py`).

Capacity is enforced by the run ledger, not by the workspace: a worker claims a row only while the
seat has a free slot and no other run occupies the PR. A triage turn has no tree at all; its
`/work` is an empty read-only directory. An issue-fix turn exports the base branch's current
commit instead of a PR head.

## The isolated turn

What happens between "a gate wrote a ledger row" and "a review appears on GitHub":

1. **Enqueue.** The gate commits one row to the run ledger (`gate.enqueue_isolated`) with the
   seat's turn budget, then spawns a detached worker and answers `[SILENT]`. A redelivered event
   finds the same row and starts nothing new.
2. **Claim.** The worker claims a pending row only when its seat has capacity and its PR is free.
   It re-reads the facts the turn depends on first (for a review: the PR is open, not a draft,
   still at that head); a PR that closed or moved on cancels the row, a draft waits, and an
   unreadable PR is retried with backoff.
3. **Launch.** The worker writes the visible seat claim (`locks.json`) and the head's in-flight
   mark, reads the PR's change record (title, description, files, diff), stages the export and
   prefetches its dependencies. It then
   starts, for this turn only, an inference proxy holding this seat's model credential and a
   broker holding this seat's GitHub token, each on its own Unix socket.
4. **Sandbox.** The agent runs in bubblewrap with no network, a staged `/etc` (one user and group
   entry, nothing copied from the host's `/etc` except the read-only `/etc/alternatives`
   symlinks), the export, the diff, a throwaway home and the two sockets. Details:
   [security model](security.md).
5. **Write.** The broker accepts only this seat's operations, records each one in the ledger
   before the external call, and checks the live PR again before sending it.
6. **End.** The worker records the outcome, removes the claim and the mark, and queues an
   operator notice for a failed or uncertain run.

**Budget.** Each turn has a wall clock, `turn_budget_s` (default 900 s, per seat with
`seats.<seat>.turn_budget_s`), fixed on the row at enqueue. The sandbox is killed 30 s after it.
Dependency prefetch (up to 300 s) runs before the clock starts. `doctor`'s `turn-budget` line
prints every figure. See [configuration](configuration.md#turn-budget-how-long-one-turn-may-run).

**Retries.** A turn that failed before any GitHub write (decided from the host's write-ahead
records, never from the exit code) is retried after 2 minutes, 4 minutes and 8 minutes, so four
attempts in all, and is then `failed`. A redelivered event re-arms a failed pre-write run up to
eight times (`MAX_REARMS`); past that only `hermes review-loop retry` resets it. A run that may
have written is `uncertain` and is never replayed. When the model provider answers 429 or a
seat's daily cap is reached, the turn waits for the usage window to reopen without spending a
retry ([pacing](operations.md#pacing-usage-windows-and-daily-caps)). More:
[when an isolated run fails](operations.md#when-an-isolated-run-fails).

## Issue triage and issue fixes

**Triage** (opt-in). The triage route listens for the `issues` event. The triage gate drops every
issue except a newly opened one whose author is in `triage.authors`, before any model sees its
text, then re-reads the issue and skips it if it is closed, is a pull request, or already carries
one of the triage labels. An eligible issue becomes one isolated `triage` turn. The agent reads
the issue as data and asks the broker for labels from the loop's list (at most `max_labels`) and,
where the loop allows it, one comment; the host writes them as the triage login.

**Issue fixes** (opt-in). When a maintainer (`triage.maintainers`) applies `triage.fix_label` to
an open issue by an allowlisted author, and the loop's unattended fixer pushes are on, the triage
gate queues one `issue_fixer` turn from the base branch's current commit. It runs as the fixer
seat. The broker lets it do exactly one of two things: push a new branch, open a PR as the fixer
and request review — after which the PR enters the normal review loop — or post one comment on
the issue explaining why it could not fix it. Operator guide: [issues](issues.md).

## Why the reviewer is woken by a request, not by a push

GitHub clears a pending review request the moment a review is submitted. So the loop continues for
exactly one reason: the fixer re-requests review after pushing. That makes the request the only
honest trigger:

- `opened` / `ready_for_review` / `reopened` — a new PR needs a first look (a request will not
  exist yet);
- `review_requested` — the fixer asked, and only when the request names *this* seat and comes from
  the fixer side;
- `synchronize` — **never**. Intermediate pushes cost nothing, and a reviewer that wakes on every
  push reviews half-finished work and burns the budget.

## Why the budget is counted in verdicts

Wall-clock budgets cannot tell "the loop is thinking" from "the loop is stuck". A verdict is a
thing that either exists on the PR or does not, so the count comes from the reviews themselves —
never from a local counter that can drift from reality. `cap = 3` means three verdicts and two fix
turns; the third `changes_requested` escalates.

## How the reviewer grades findings

The reviewer's prompt (`prompts.ISOLATED_REVIEWER`) asks for one verdict, APPROVE or
REQUEST_CHANGES, and for every finding its grade **P0–P3**, evidence (command and observed output)
and `file:line`. Each finding is either:

- **blocks** — a P0 or P1, a regression, silent data loss, the wrong agent woken, a false green on
  a safety check, or a test or build the change breaks. Any blocking finding makes the verdict
  REQUEST_CHANGES;
- **issue** — everything else (usually P2/P3): real, but not worth another round. With only
  issue-tier findings the verdict is APPROVE, and the review lists them under **Issues to file**,
  each with a suggested title, for the operator to file.

A claim the reviewer could not verify is not approved: the verdict is REQUEST_CHANGES naming what
could not be checked. The exception is the environment itself — when the host says dependencies
are unavailable, or a build did not fit the sandbox, that is noted, not held against the PR.

## Escalation

Adjudication is opt-in: it runs only on a loop set up with `init --adjudicator-route`. Without
that route a spent cap writes a breach marker and an observer `escalation` notice naming you as
the next turn, and nothing else; no adjudicator turn is enqueued.

With the route, when the cap is spent, the gate verifies the live PR head, writes a durable
`delivery-pending` breach marker, and — under the breach lock's reservation — enqueues an isolated
`adjudicator` turn in the host run ledger (turn key `breach:<rounds>`). A durable, armed enqueue promotes the marker to
`awaiting-adjudication`; any failure leaves it pending for a later event or watchdog sweep to retry.
One accepted wake per head: repeated events and retries dedup on the ledger's unique
repo/PR/head/seat/turn index, and a late event for an older head cannot replace the current marker.
The adjudicator's route itself runs `gate_adjudicator.py`, which always answers `[SILENT]`: the
turn comes from the ledger, never from the route.

The worker claims the turn only after re-reading GitHub (open, not draft, same base and head, author
a configured fixer, cap still spent, no approval at the head, a matching marker); a moved, closed,
retargeted or approved PR cancels it, an unreadable one waits. Right before launch it re-verifies,
marks the breach `adjudicating`, and runs the agent credentialless with a read-only export of the
head, a host-rendered prompt and the reviewer verdicts and the fixer's published answers as data.

The adjudicator is told to read both positions and rule — ACCEPT, REJECT or RESPEC — with a reason,
and **not** merge, push or review (it has no way to). Its one broker `ruling` is recorded in the run
ledger first; then the host sends an observer `ruling` notice, queues the full ruling in the
operator outbox, and posts it as a PR comment only for a configured, distinct adjudicator identity.
The operator is the veto, not the reviewer — overriding a ruling should cost one message, not a
re-read of the whole thread.

## The watchdog's four shapes

Read from GitHub every 15 minutes. The first successful armed sweep snapshots existing PR heads
as history. Each subsequent SHA change gets a durable first-observed timestamp in `watchdog.json`;
the reviewer grace starts then, not at the commit's authored/committed date. A first-seen PR created
since arming also gets a clock; an old PR first seen later is conservatively baseline-only until its
head changes. Existing `watchdog.json` files without `heads` establish this conservative snapshot on
their next successful sweep. Unreadable PR listings neither advance the snapshot nor drain queues;
unreadable review lists do not produce verdict-dependent alerts. An old PR whose head changed before
the first successful observation cannot be distinguished from an unchanged old PR without an event
record, so it remains baseline-only until the next observed SHA change. An observation survives a
brief omission from the listing, a draft transition, or close/reopen at the same SHA. Absent heads
expire after 30 days since last seen; a reappearing old head after expiry is baseline-only, never
falsely treated as a recent push. Corrupt observation clocks are also treated as unknown. The four
shapes are:

1. reviewer never posted a verdict for a quiet head;
2. fixer never pushed after a verdict (on a loop without unattended fixer pushes this is reported
   as *fixer held*, once per head, with the enable command — no fixer turn starts there);
3. a PR parked awaiting adjudication;
4. the cap is spent at this head with no approval and no escalation marker — i.e. *the gate did not
   act on it*, which is the failure the loop cannot see about itself.

It also retries pending adjudicator enqueues for current heads with a verified spent cap,
reports stuck seats (a lock older than a run could plausibly live, a request waiting past
the grace period), and drains the queue when it can be proven safe: the seat is free, the PR is
still open, the head has not moved, and the verdict has not already landed. A drain re-delivers a
synthetic event to the seat's gate route; the gate re-checks everything, enqueues the turn and
answers `[SILENT]`. A drain that fails these checks drops the entry instead of re-delivering it —
a stale queue entry must die quietly, not start a run against a head that moved on.

The "is this loop armed at all?" question is answered by `gate.hooks_read`, which the watchdog and
`explain` share: both seat routes must exist as active repo hooks. An unreadable hook list is
**not** "paused" — a token without hook read access (classic `repo`, or the narrower
`read:repo_hook`) cannot see hooks that may well be active — so
the watchdog neither drains nor scans there, but alerts with the read token's login and the HTTP
status (a 401/403 at once, a 5xx or no answer after three failed sweeps in a row, re-raised every
`cooldown_h`), and `explain` prints "unknown" rather than guessing in either direction. The
open-PR listing counts the same way: a token that can see the hooks but is refused the pulls (a
403) raises the same alert on the same cadence. The stall scan's per-PR review reads are not silent either:
a PR whose reviews cannot be read is skipped without a guessed verdict, but the sweep names it
in one bounded line (`could not read reviews for N PR(s) — #7: …`). If the failure looks like an
outage (a 401/403, a 5xx, or no answer), it also counts toward the read alert. "GitHub reads work
again" is said only after a sweep in which every read it made succeeded. When the read alert
fires, it names every PR whose failure looks like an outage, and every other failed PR stays in
the per-PR line, so no failed PR goes unmentioned. A gate that cannot read the current PR still
answers `[SILENT]`, and leaves the failed call in `github-reads.json`, which `explain` shows on its
`github:` line. The read is reported once, by one owner: the gate-failure entry the gate recorded
for that event (its alert, and its re-drive), with the `github-reads.json` record marked
`owned_by` so the health check does not report it again. Only a failed call that no gate-failure
entry owns is reported by the health check. A failed write is reported by what is known:
a 4xx means GitHub refused it and nothing changed; no answer or a 5xx leaves the outcome unknown,
and the line names what to check on the PR before re-sending it. GitHub's error bodies arrive as
pretty-printed JSON; every alert and `explain` line folds them into one bounded line.

## Explain — why is this PR not moving?

A loop that stopped being driven looks exactly like a loop with nothing to do, and no single file
answers "why". Half the answer is in GitHub (the head, the verdicts *at that head*, whether an
approval exists) and half is on disk (who holds the PR, what is queued, what is marked in flight,
what the watchdog last saw). `hermes review-loop explain --pr N` reads both and prints one report (example output:
[Operating a loop](operations.md#why-isnt-this-pr-moving)).

It is **the gates' own logic, walked differently**, and that is the design constraint that matters:

| | a gate | `explain` |
|---|---|---|
| input | a webhook payload | GitHub + the state directory |
| guards | the same guards, in the same order | the same guards, in the same order |
| at a guard | stops — `silence()`, and the operator sees only that nothing happened | reports all of them, and names the one that is holding the PR |
| output | always `[SILENT]` | labelled facts, `blocked:` reasons, one `next:` event |
| effect | a run-ledger row, a hold or queue entry, a breach marker, or an observer notice (the seat claim and in-flight mark are written later, by the worker at launch) | none |

The predicates are shared, not copied: `verdicts` (the round count), `reviews_at_head` /
`reviewed_at_head` / `changes_at_head` / `approved_at_head`, `seat_key`, `seat_capacity`,
`breach_delivery_status`, the seat ledgers, the queue and `hooks_read`. Re-deriving any of them would be
the drift this report exists to rule out — an operator who is told "awaiting the fixer" while the
fixer gate would in fact have enqueued a turn has learned nothing.

The guards report in the gates' own order, so the first one that names an action *is* the guard the
loop would stop at:

1. can GitHub be read at all (a failed read is unknown, never "closed");
2. is the PR closed or merged (the loop is over; the closed path reclaims the disk);
3. is the loop armed — a paused loop can be woken by nothing;
4. is this a PR the reviewer gate serves at all (draft, wrong base, author is not a fixer);
5. the budget: an approval ends the loop; a spent cap with pending delivery needs retry before any
   ruling can be expected, while an acknowledged marker means adjudication;
6. who holds the PR right now (one PR, one seat) — only a lock for the live head can imply its next
   verdict or push; an old-head lock must be released or expire;
7. is it queued for this exact head (a stale queued SHA is dropped, never retargeted; a current
   queue waits for capacity); even without a queue entry, locks held by other PRs can fill a seat;
8. is this exact head marked in flight (a run is already out for it);
9. a verdict at this head with no fix run out (retry the fixer gate event, not an absent fixer's push)
   — or, while the loop has not opted in to unattended fixer pushes, an *operator decision*: the
   verdict is held and the `next:` line names `fixer-push --enable`;
10. a non-verdict review at this head (a comment consumes no round and does not suppress a new
    review request in the reviewer gate);
11. nothing at this head: the fixer's request is what wakes the reviewer, and GitHub clears it when
    a verdict lands, so a missing request is the classic silent stall. A pending request *without*
    a run needs its gate event re-delivered, not a verdict from a reviewer who never started.

Every report ends in exactly one `next:` line: a reviewer verdict, a review request, a retry of the
fixer event, a fixer push plus request when a run exists, an operator decision (enable fixer pushes),
a released slot, an adjudication, a re-arm,
a read retry, or nothing at all. Timestamps
carry their source (the read itself, the verdict's `submitted_at`, or the state mark's own epoch),
and anything that could not be read is printed as unknown with the reason.

**Zero mutation is a property, not a promise.** `explain` does not call `st.active()` — which prunes
expired locks *and writes them back* — but `st.live_locks()`, its read-only twin; it reads inflight
marks with `inflight_at`, and never touches the queue except to count it. The suite runs it twice
with an expired lock, a queue entry and a breach marker on disk, and asserts every file (both loops'
configs, every state file, the route registry and the stub world) has the same SHA-256 hash and that
no webhook was sent.

## The observer feed (read-only, never a seat)

The loop is unattended, which is the point — but "nobody is watching" and "nothing is visible" are
different things. A loop may carry one **observer**: a chat destination that receives a short notice
each time the loop changes state, without joining the loop. (Operator guide:
[observer.md](observer.md); keys: [configuration](configuration.md#the-observer-feed).)

A notice is emitted *at* the transition, by whoever made it — the reviewer gate on a handoff, the
fixer gate on a verdict, `breach()` when the cap is spent, the reviewer gate on a close, and the
watchdog when it decides a stall is worth reporting. Nothing is inferred from an agent's summary,
and none of it can influence a seat: the observer path runs after the state change, never in front
of it. Escalation is announced after the durable pending marker but before the adjudicator turn
is enqueued, with the next turn shown as pending rather than complete.

The observer is the one part of the loop that sends a message out through a route: a signed POST
(`X-Hub-Signature-256`, `observer.py` → `routes.fire`) at a route bound to the observer's
profile. There is no adjudicator POST and no POST that wakes a seat: seats are started only from
the run ledger (the watchdog's queue drain re-delivers an event to a gate route, and that gate
only enqueues). The observer route is `deliver_only` and its prompt is the notice itself: the loop wrote
the message before the POST, so no agent is woken and no turn is taken.

The delivery ledger (`observations.json`) is what makes the feed idempotent and honest:

```
entry key = loop : PR : head : event : verdict-or-round identity
status    = delivered | queued | pending | failed | uncertain
```

- A second webhook about the same transition finds the entry and stops, however many times GitHub
  redelivers or the sweep runs — the key is the fact, not the delivery attempt.
- A definite pre-POST failure is retried by the watchdog, up to `MAX_ATTEMPTS`.
  A timeout, 5xx, or stale pending claim may already have reached the receiver: it becomes
  `uncertain` and is never automatically replayed. Older `failed` receipts without explicit
  pre-POST evidence are also quarantined. Reconcile them manually before changing routes.
- With `digest_min` above zero, new entries park as `queued` and the sweep sends one message listing
  them. The batch is itself an entry carrying its members, so a digest and a single notice can never
  both claim the same transition.
- Muting, filtering by event, or having no feed short-circuits before the ledger is touched: a
  disabled observer is indistinguishable from no observer.

The feed holds no lock and carries no secret: the payload is the loop id, PR number and URL, head,
event, outcome, next turn and a one-line summary. A private PR URL reaches only the profile the
operator configured — that is the one boundary the observer is allowed to cross. A loop without
an explicitly configured webhook host cannot send a private link by inheriting the mutable route
registry's host.

## Cleanup

A finished PR gives its disk back: worktrees, build directories, probe logs, plus the loop's own
state (locks, queue, in-flight marks, breach marker). Rails, because this deletes real directories:

- only paths inside non-symlink configured `roots` (or the loop's artifacts directory) are
  considered; a root's PR-like name does not attribute every child to that PR, and a plain root
  child must name this loop's repository as well as the PR (`widgets-pr7-target`), because roots
  are shared between loops. `/`, the home directory and its ancestors are refused as roots;
- detached worktrees must be registered to this clone and inside an allowed root. Other Git
  checkouts, nested repositories, the clone and its contents are protected even if PR-named. The
  one exception is the loop's own `artifacts/<PR>/` under its real (non-symlink) state dir: the
  isolation clones inside it were made by the loop, so their `.git` does not protect them;
- a worktree with a **branch** checked out is never touched — that is somebody's working tree, not
  a review artifact (only detached checkouts are cleaned);
- evidence patterns (`phase3`, `evidence`, `soak`, `release-verification`) are skipped: regenerable
  build output is not the same thing as a receipt;
- cleanup requires a fresh GitHub lookup confirming the matching PR is closed; open, failed, and
  malformed lookups are refused. The standalone cleanup script's explicit `--force` is the
  operator-only override (the webhook and plugin CLI never pass it);
- the clone itself is out of scope by construction.

## Preflight: can this installation run?

`init` writes the config, the routes, the hooks and the cron job; it cannot check itself. So
`hermes review-loop doctor --loop <id>` walks the installation read-only and answers one question:
**can this loop wake a seat and post a verdict?** It is the installation-level counterpart of the
watchdog — the watchdog asks "is this PR stalled?", the preflight asks "is this loop wired at all?"
Example transcripts are in [Operating a loop](operations.md#preflight-doctor).

| check | what it proves |
|---|---|
| `config` | the loop file is there and parses |
| `turn-budget` | prints each seat's turn budget, the whole launch-to-end bound, and the stall and seat-lock clocks derived from them (always verified: it informs, it does not judge) |
| `profile:reviewer` / `profile:fixer` | each seat's Hermes profile home exists (`~/.hermes/profiles/<name>`, or `~/.hermes` itself for `default`); `profile:adjudicator` too when the loop has an adjudicator route |
| `credential:<seat>` | a nonempty token file is mapped for that seat's login through `gh.token_path`; profile `GH_TOKEN` alone is not used by the gates |
| `profile:triage` / `credential:triage` | with triage on: the triage profile exists, and the triage login (its own, else the reviewer's) has a nonempty, private token file |
| `credential:adjudicator` | only when `seats.adjudicator.login` is set: it is a fourth account — not the reader or a seat — with its own private token file |
| `model:<seat>` | the seat's profile names a provider and model the inference proxy can carry — read from the profile's `config.yaml` by the runtime's Hermes interpreter, without resolving any credential |
| `extras:<seat>` | the optional Hermes package that seat's provider needs (the `anthropic` extra for the Messages wire) is importable by the runtime file's `venv` — the interpreter the sandbox mounts; a provider Hermes only *may* move onto that wire is ⚠️ without it, and the line is skipped when `model:<seat>` already fails |
| `fixer-push` | whether unattended fixer pushes are on; off is reported as unknown (the fix leg cannot run; verdicts are held) with the enable command |
| `attribution` | whether what the loop posts is signed "Automated by hermes-review-loop" (either answer is verified) |
| `sandbox:caps` | the two writable tmpfs mounts per turn, times this loop's concurrent turns, against the host's available memory, and any refused size override |
| `token:<login>` | every credential file named in the config exists, is non-empty, and is not readable by group or other users |
| `read_token` | the login the gates read GitHub as is one of those mappings, and is its own account: not a seat, not the adjudicator login, no shared token file (the four-identity rule) |
| `route:<name>` | the gateway's registry holds the route, it wakes *this* seat's profile, it carries a secret and a prompt, it runs the right gate script for the right event, it is not switched off (`enabled: false` makes the gateway answer 403 to every event; `apply` or `doctor --repair` re-enables it), and it resolves to this loop's own gateway origin — and, when the plugin has an intent record for it, still matches that record (a rotated secret looks well-formed but no longer matches GitHub's hook). A loop with an observer gets the same check for its feed route: present, serving `observer.profile`, and exactly the delivery-only contract the feed checks before every notice |
| `gateway-script:<route>` | the route's `script` resolves the way the gateway resolves it — under the **serving profile's** `scripts/` (`~/.hermes/scripts` for `default`, `~/.hermes/profiles/<name>/scripts` otherwise), as a real file inside that directory — and it is the plugin's gate shim pinned to this install. `init`/`apply` write those shims (a symlink would be refused by the gateway); `uninstall` removes the ones no other loop needs; a same-named file the plugin did not write is never touched |
| `route-intent` | the plugin's intent record (`route-intent.json`) can be read and compared with the live registry |
| `scripts` | the plugin's six scripts are on disk: `watchdog.py`, `gate_reviewer.py`, `gate_fixer.py`, `gate_adjudicator.py`, `gate_triage.py` and `cleanup.py` |
| `gate:timeout:<profile>` | for each profile that serves a loop route, the gateway's script timeout, which the gates shrink their own budget to fit |
| `cron:shim` | `~/.hermes/scripts/review-loop-watchdog.py` exists **and is pinned to the plugin install that is here now** — an upgrade that moves the directory leaves the scheduler running an old path |
| `cron:job` | the scheduler's store holds the **one shared** watchdog job (it sweeps every loop once per tick) and it is not paused — extra jobs are named for migration (#60) |
| `watchdog:run` | the watchdog actually ran recently: its `last_run` stamp is newer than twice the cron interval |
| `runtime:file` / `runtime:<key>` | the private runtime file exists (0600) and its `source`, `venv`, `runtime` and `rust` paths exist and look right |
| `clone` | the clone exists, is a git checkout, and is not inside the loop's artifacts root — the cleanup deletes that whole tree |
| `state_dir` / `roots` | the loop can write its locks and queue there; every cleanup root is a directory |
| `gateway` | a TCP connect to the loop's webhook origin is accepted |
| `hook:<route>` | the repo hook posts at the route's URL, subscribes to that seat's event, and is active; and its recent deliveries show how the gateway answered — a latest delivery answered 401 or 403 means the hook's secret does not match the route, so it wakes nothing |

Five states, and the difference between absent/mismatch and unknown is the point:

* ✅ **verified** — checked, and correct;
* ❌ **absent** — not there at all;
* ❌ **mismatch** — there, but not what this loop needs: a route waking another profile, a hook on
  another gateway, a shim pinned to a stale plugin path, a world-readable PAT;
* ⚠️ **unknown** — could not be decided from here: a hooks read the token was not allowed to make
  (reading a repo's hooks needs hook read access: classic `repo`, or the narrower `read:repo_hook`),
  or a probe skipped with `--offline`;
* ➖ **skipped** — not checked, because another line already fails for the same cause:
  `extras:<seat>` while `model:<seat>` is ❌ (no provider to check). Neither a pass nor a second
  warning, and not counted as unknown by `--strict`.

**Unknown is never folded into absent.** "The API refused to tell me" and "there are no hooks" are
different claims, and printing the second when the first is true sends the operator hunting for a
hook that exists. Failures exit 1 and each one carries the single command that fixes it;
`--strict` makes an `unknown` a failure too, for installs that require a fully proved preflight.

Read-only is a hard rule here, twice over. The preflight writes no config, route, state or hook —
a check that repairs what it looks at cannot be trusted to describe what is wrong (the one
exception is opt-in: `doctor --repair` runs the watchdog's route self-heal *before* the checks,
and prints what it restored) — and it never posts to a route, because a synthetic event at a
seat's route would pass its gate and enqueue a real turn with a real budget. The entire network
side of the preflight is a TCP connect to the gateway and, when the token is permitted, a read of
the repo's hooks and of each hook's recent deliveries. To see what a gate decided for a delivery
that already happened, use `hermes review-loop trace`; to prove a hook's secret end to end, use
`selftest --ping`, which asks GitHub to send a harmless signed `ping` that no gate acts on
([security: live verification](security.md#live-verification-hermes-review-loop-selftest)).

## What a plugin can and cannot own

This ships as a general Hermes plugin, which means it can register a CLI command, tools, hooks,
middleware and skills — and it **cannot** own webhook routes, GitHub hooks or cron jobs. That is
why `init` writes those through the operator-visible config surfaces instead of inventing a second
registry. The consequence is good: nothing about the install is hidden, `uninstall` is the inverse
of `init`, and a broken loop can always be inspected with the tools the gateway already has.

## The shared route registry (issue #1)

`webhook_subscriptions.json` has writers the plugin does not control: Hermes's own CLI and
dashboard rewrite it without the plugin's `flock`. A native read-modify-write racing a plugin one
could erase or rewrite the loop's routes — including their HMAC secrets, after which GitHub's hook
can no longer authenticate. Issue #1 is closed as mitigated (by #31). The real fix is upstream:
every writer sharing one locked transaction (NousResearch/hermes-agent#120964, still pending).
Until then the plugin defends its own routes:

| mechanism | where | effect |
|---|---|---|
| optimistic write | `review_loop/routes.py` (`_transact`) | identity (inode, mtime_ns, size, sha256) recorded at read and re-checked just before `os.replace`; a change means re-read and re-apply (`CONFLICT_RETRIES`, 5 attempts, then `RegistryConflictError` and nothing published), so a native write that lands during a plugin edit is preserved |
| intent record | `<state_dir>/route-intent.json` (`review_loop/route_intent.py`) | the plugin's private 0600 copy of every route it owns, secret included; updated by `init`/`apply`/`set`, forgotten by `uninstall` and observer renames |
| self-heal | every armed watchdog sweep (before the PR listing); `doctor --repair` | missing or drifted routes (secret, script, prompt, events, profile, `deliver_only`, host) restored with the same secret, and the cron output says what was restored |
| report | `doctor` (read-only) | `route:<name>` turns ❌ when the live route differs from the record |

1. **Optimistic concurrency** (`routes._transact`). Under the plugin lock, each read records the
   file's identity; immediately before `os.replace` the file is re-read and re-hashed, and on any
   difference the edit is re-applied to the fresh bytes. Edits are pure functions of the parsed
   registry, so a retry never mints a second secret.
2. **Intent record** (`route_intent`). `init`, `apply` and `set` record what they wrote as the last
   step of their transaction (a failure rolls the route back rather than leaving a record the
   registry does not match); `uninstall` and an observer rename forget first, so self-heal never
   fights an operator who changed things through the plugin. Routes a loop installed before the
   record existed are *adopted* on the next sweep if their gate script and prompt prove they are
   this loop's.
3. **Self-heal** (`route_intent.heal`). For each of *this loop's* route names in the record:
   missing → restored; watched fields differ and the live entry still runs a review-loop gate →
   restored with the recorded secret; live entry now runs someone else's script → reported, never
   overwritten. A malformed registry or record → alert, nothing written. Restores go through the
   same optimistic write, so a native change racing the heal survives it.

Never touched: routes not in this loop's config, routes whose name another writer now uses for a
non-review-loop script (reported instead), and a malformed registry (alert only — fail closed).

What remains:

* *The last-check-to-rename gap.* Between the final identity check and `os.replace` there are a
  few syscalls. A native write landing exactly there is overwritten by the plugin's publish.
* *Native writers that read early.* A native writer that read the registry before a plugin
  publish and writes after it overwrites the plugin's edit — optimistic checks on our side cannot
  see its stale read. The plugin's routes come back on the next armed sweep; the native writer's
  own edit is whatever it wrote.
* *Between sweeps.* Until the next armed watchdog sweep (or `doctor --repair`), a damaged route
  can miss deliveries. A paused loop (hooks inactive) does not heal.
* *Intentional native edits are reverted.* Changing this loop's routes in the Hermes dashboard
  is indistinguishable from the race; make route changes through `hermes review-loop set/apply`
  or remove them with `uninstall`.

## Stacked PRs and retargets

A stacked PR — one whose base is another open PR's branch rather than the loop's base — is not
reviewed: the reviewer gate requires the PR's base to equal the loop's `base`. It enters the loop
once it targets the base branch, typically after its parent merges.

### After the parent merges: a fresh review situation

When an observed stacked child is retargeted to the base branch with the same head (typically
because its parent merged), the host records a separate quarantine ledger
(`review_loop/transition.py`, `stack-transitions.json`). Old review IDs (the `old_review_ids`
baseline), rounds, in-flight marks, queued requests and cap markers cannot authorize a new run,
and **nothing from before the boundary carries over** — not an approval, not a rejection, not a
round.

The transition then starts a fresh review situation automatically: it enqueues **one** isolated
reviewer turn at that head (`transition.start_fresh_review` → `gate.enqueue_isolated`) with the
turn key `retarget:<from_base>:<boundary ms>` (`transition.turn_key`), derived only from facts
frozen in the hold. The armed watchdog sweep (`reconcile_stacked`, and `retry_fresh_reviews` on
later sweeps), the `edited` webhook and a queued drain all name the same turn and dedup on the run
ledger's unique repo/PR/head/seat/turn index. Each enqueue re-reads the live PR; a draft waits
until it is ready. A failed enqueue (no private runtime, ledger or spawn error) is kept on the
hold as `fresh_review.state = retry`, reported by the sweep and by `explain`, and retried on the
next sweep. A same-head review request re-drives only that same turn.

In the new situation a review counts for the held head only when `transition.effective_reviews`
accepts it: its ID is **not** in the baseline **and** a confirmed host receipt in the run ledger
(`review_receipts` joined to `runs`) binds that exact review ID, principal and verdict to an
isolated *reviewer* run for this repo/PR/head whose pinned generation names this head and the
configured base. Every decision uses that one helper: the reviewer gate (already reviewed, round
count, cap), the fixer gate (a receipted post-boundary `CHANGES_REQUESTED` is a work order; a
receipted post-boundary `APPROVED` can produce the `you merge` cue, with every other base check
unchanged), the watchdog (stalls, drain, breach retry), the supervisor's claim and launch checks
for fixer and adjudicator turns, and `explain`. A review GitHub merely lists after the boundary —
a human's, or one posted by hand — has no receipt and stays diagnostic only. An unreadable
receipt ledger is unknown, never "no review".

A hold whose baseline could not be read (`old_review_ids` is null) is permanent at that head: old
and new reviews cannot be separated, so no fresh turn is enqueued and no review counts; `explain`
names that reason. A **later** child head pushed while targeting the base branch is an ordinary
PR. An unobserved stacked push followed by a retarget is quarantined at its newly seen head: there
is no proof that the push occurred after the retarget. A first observation *after* an unobserved
retarget cannot prove the old base, so it cannot retrospectively classify same-head reviews from
before it; do not infer their approval from the lack of a quarantine record. Activation on an
existing PR with no earlier stacked observation is a provenance gap: the system cannot tell an
unobserved retarget from a PR that always targeted the base, so baseline it explicitly or wait for
a new head. An already-running turn is not cancelled by this ledger. The fixer approval gate
rechecks the live base ref and SHA at its final PR read before saying `you merge`; that read is
not atomic with the later notice, so a retarget (or a retarget and rollback) entirely between
reads remains possible. GitHub has no atomic conditional review POST; the receipt's pre- and
post-POST generation reads narrow, but cannot close, that window.
