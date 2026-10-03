# How the review loop works, in plain words

This page explains the ideas behind the loop: what it is for, the words you will meet in its
output, what happens to one pull request from start to finish, which switches must be on, and
where its files live. It does not list every flag. For that, see [commands.md](commands.md). For
setting up the GitHub accounts and tokens, see [accounts.md](accounts.md). When something is
stuck, see [troubleshooting.md](troubleshooting.md).

The examples use the same names as the rest of the docs: the repository `owner/name`, the loop id
`name`, the fixer login `dev-account`, the reviewer login `rev-bot`, the reader login
`reader-bot`, and the Hermes profiles `coder` (fixer), `critic` (reviewer) and `arbiter` (adjudicator and
observer).

## What the loop is for

Two AI agents review each other's work on GitHub. One agent writes code and opens a pull request.
A second agent, running a different model under a different GitHub account, reviews it and
either approves it or asks for changes. If it asks for changes, the first agent fixes the code and
asks for another review. This repeats until the reviewer approves, or until a fixed number of
rounds is used up and a person is asked to decide.

The agents do the creative work. Everything else is plain Python: who runs, when they may run, how
many rounds are left, and whether the loop has quietly stopped. That part must not guess, so no
model is involved in it.

The real problem the loop solves is that unattended agent loops fail **quietly**. A loop that
stopped working looks exactly like a loop with nothing to do. GitHub clears a review request as
soon as a review is posted, so a fixer that pushes without asking again ends the loop and nobody
notices for days. Two runs that share one folder can corrupt each other and post wrong reviews. A
loop that keeps asking for changes can burn a night of model budget. This plugin makes each of
those failures either impossible or loud: it counts rounds from GitHub itself, gives every run its
own workspace, and runs a watchdog that reports a PR that has stopped moving.

## Glossary

Each term below appears in the CLI output or in the other pages. The second half of each entry
says what it means for you.

### People and accounts

**Loop.** One configured repository: which GitHub accounts play which part, which Hermes profiles
run them, the round limit, and so on. It has an id (by default the repository name, here `name`)
and one config file. One Hermes install can run several loops, one per repository. Almost every
command takes `--loop name`.

**Seat.** A role in the loop. There are three core seats, and two more for issues that are off
until you turn them on:

| seat | what it does | what it can write |
|---|---|---|
| reviewer | reads the PR, builds and tests it, posts one verdict | one review: approve or request changes |
| fixer | answers a changes-requested verdict with a fix | one push, then one review request (with an answers comment) |
| adjudicator | rules when the round limit is spent | one ruling: ACCEPT, REJECT or RESPEC, with a reason |
| triage (opt-in) | reads a new issue and labels it from a fixed list | labels from that list, and one short comment only if you allow it |
| issue fixer (opt-in) | the fixer seat, handed an issue a maintainer labelled for it | either one PR from a new branch `review-loop/issue-N`, or one comment on the issue saying why it could not fix it |

A seat is a role, not a particular agent. You decide who sits in each seat. Triage runs as its own
profile and labels as the reviewer's account unless you name another. The issue fixer is not a
separate account or profile: it is the fixer seat working from an issue instead of a verdict.
Both are turned on with `triage`; the step-by-step guide is [issues.md](issues.md).

**Login, profile and agent name.** Each seat has three names, and they mean different things:

- The **login** is the GitHub account the seat acts as, such as `rev-bot`. Reviews, pushes and
  comments appear on GitHub under this account.
- The **profile** is the Hermes profile the turn runs as, such as `critic`. The profile decides which
  model and which model credentials are used. Change a seat's model with `hermes -p critic model`.
- The **agent name** (`seats.<seat>.agent`) is only a display name used in prompts and in the
  signature on what the loop posts. It defaults to the profile name.

The reviewer and the fixer must differ in all three: profile, login and token file. One account
reviewing its own work is not a review.

**Allowlists (`fixers` and `reviewers`).** Two lists of GitHub logins in the loop config. They
decide which events the loop pays attention to:

- `fixers` lists the logins whose PRs the loop works on. A PR opened by anyone else is ignored. The
  fixer seat's login must be in this list.
- `reviewers` lists the logins whose verdicts count toward the round limit. The reviewer seat's
  login must be in this list.

**The reader (`read_token`).** A separate GitHub account the loop uses only to *read*: PRs, reviews
and the repository's hooks. Here it is `reader-bot`. It is usually the repository owner, because
the owner can hold a narrow, read-only token. `init` requires `--read-token`.

**The four-identity rule.** The reader, the reviewer, the fixer and (if you set one) the
adjudicator's comment login must be four different GitHub accounts with four different token
files. `init`, `set` and `doctor` refuse a loop that breaks this rule. The broker checks it again
before every write, by asking GitHub who each token belongs to. If two of them were the same
account, a seat could approve its own work. The adjudicator login is optional; without it, rulings
go only to you.

**Token file.** Each login's GitHub personal access token lives in its own file, mode 600. The loop
config holds only the **path** to that file, never the token itself. The convention is
`~/.hermes/keys/<login>-pat`, but any absolute path you own works. Which kind of token each role
needs is in [accounts.md](accounts.md) and
[token scopes by role](operations.md#token-scopes-by-role).

### How events reach the loop

**The gateway.** The Hermes process that receives web requests from GitHub. When GitHub sends an
event, the gateway checks its signature, then runs a small script for it. You give `init` the
gateway's public address with `--host`. There is no shared gateway: it must be your own.

**Route.** One named entry in the gateway's list of webhooks. A route says: at this URL, check
this secret, run this script, as this profile. `init` creates one route per seat: `name-review`
and `name-fix`, plus `name-breach` when you pass `--adjudicator-route`, and `name-observe` when you
turn on the observer feed. `triage --enable` adds `name-triage`, which serves both triage and
issue fixes. A route's URL contains its profile (`/p/<profile>/webhooks/<route>`), so
moving a seat to another profile also moves its route. `status` shows each route next to the seat
it should serve and says `MISMATCH` if they disagree.

**Repo hook.** The webhook on the GitHub repository that sends events to a route. `init --hooks`
creates two of them: one for `pull_request` events (to the reviewer's route) and one for
`pull_request_review` events (to the fixer's route). With triage on, a third one sends `issues`
events to the triage route; `triage --enable --admin-token LOGIN` creates it. A hook is either:

- **paused** (inactive): GitHub sends nothing. This is how `init --hooks` creates them, so that
  nothing happens before you have checked the install.
- **armed** (active): GitHub sends every matching event. `arm` turns them on and `arm --pause`
  turns them off again. The triage hook is created paused too, and `arm` turns it on with the
  others.

A paused loop is fully silent: no events arrive, and the watchdog neither reports stalls nor starts
queued work.

**Gate.** The script a route runs for each event (`scripts/gate_reviewer.py`,
`scripts/gate_fixer.py`, and `scripts/gate_triage.py` with triage on). A gate decides whether the
event should start a turn. It checks the facts again against GitHub (is the PR still open, is this
still the newest commit, who wrote it, how many rounds are spent) and then either queues a turn or
declines. `scripts/gate_adjudicator.py` is legacy: older installs may still have a `name-breach`
route that runs it, and it always declines. An adjudicator turn is queued by the loop itself when
the cap is spent, and only on a loop with an adjudicator route.

**Why gates always answer `[SILENT]`.** Whatever a gate decides, it prints `[SILENT]` and exits 0.
That tells the gateway not to start one of its own agents for the event. This matters: a normal
gateway agent has your credentials, and the loop never lets one handle a PR. The turn the gate
queued runs separately, in the sandbox described below. The side effect is that GitHub's webhook
page shows the same reply, `200 {"status":"ignored","reason":"script"}`, for an event that started
a run and for one that was declined, and Hermes logs nothing for a decline. To see what a gate
decided for one delivery, use `trace` (see [Diagnostic commands](#diagnostic-commands)).

### How a turn runs

**Isolated turn.** One run of one seat on one PR at one commit. The gate does not run the agent
itself. It records the turn in the run ledger, and a separate worker process picks it up. The
worker checks GitHub again, prepares a copy of the code at the exact commit, and runs Hermes in the
sandbox. The turn ends when the agent finishes, or when its turn budget runs out.

**The sandbox (bubblewrap).** The turn runs inside [bubblewrap](https://github.com/containers/bubblewrap)
(`bwrap`), a Linux tool that starts a process with its own private view of the system. Inside, the
agent sees a copy of the PR's code at `/work`, the PR's diff, and two sockets. It has no network,
no GitHub token, no model key and none of your files. `selftest` checks that the sandbox works on
your machine.

**The broker.** A small process on the host that owns the GitHub tokens for one turn. The agent
reaches it through a socket inside the sandbox. It is the only way anything the agent does can
reach GitHub. Before each write it checks the live PR again (same commit, still open, right
author, four distinct accounts) and refuses anything else.

**Run ledger.** A SQLite database on the host, `~/.hermes/state/review-loop-runs.sqlite`, with one
row per isolated turn and its state: pending, running, succeeded, waiting to retry, failed,
cancelled (the PR closed or moved on, or a policy held the turn before it started) or
`uncertain`. It also records what each turn wrote. It is what stops the same turn from running
twice and what limits how many turns run at once. A turn that fails before it wrote anything is
retried on its own after 2, 4 and 8 minutes; if the fourth attempt also fails, the run is marked
failed. (A turn that ran out of its turn budget is the exception: see below.) `uncertain` means a write may have reached GitHub; such a run is never retried on its own
(see [when an isolated run fails](operations.md#when-an-isolated-run-fails)).

**Runtime file.** A private file, `~/.hermes/review-loop-runtime.json` (mode 600), that tells the
worker where things are on your machine: the Hermes source, its Python environment, a Python
runtime and a Rust toolchain. It does not choose the model; each seat's model comes from its Hermes
profile. Without this file, no isolated turn can start: an event that would start one is held with
the reason. The keys are in
[configuration.md](configuration.md#runtime-file-and-seat-models-review-loop-runtimejson).

**Turn budget.** How many seconds one turn may run, building and testing included. The default is
900 (15 minutes), and each seat can have its own. Hermes tells the agent to wrap up at 80% of the
budget, and the sandbox is killed 30 seconds after the budget runs out. A turn killed this way is
not retried automatically, because the same budget would most likely run out again: raise it with
`set --reviewer-turn-budget N` or `--fixer-turn-budget N`, then `retry`. Details:
[turn budget](configuration.md#turn-budget-how-long-one-turn-may-run).

**Concurrency.** How many PRs a seat may work on at once. Each seat has its own number, so "two
reviews and one fix at a time" is a normal setting. Above 1, the loop needs a local clone
(`--clone`), because every parallel run gets its own copy of the code. Everything above the limit
waits in a queue and starts when a slot frees. Separately, **one PR is held by one seat at a
time**: a review never runs while the fixer is working on the same PR.

**Pacing and daily caps.** A seat on a subscription model (such as a ChatGPT or Claude
subscription) shares its usage window with your own use of that account. When the provider answers
429 and says when the window reopens, the turn waits until then **without spending a retry**, and
new turns for that account wait too. `seats.<seat>.daily_turns` caps how many turns a seat may start
per day on this loop, so the loop cannot eat your whole day's quota. Set it with
`set --reviewer-daily-turns N` (0 removes it). See
[pacing](operations.md#pacing-usage-windows-and-daily-caps).

### Rounds and decisions

**Verdict.** A review from a login in `reviewers` that either approves or requests changes. A plain
comment is not a verdict: it neither counts nor wakes anyone. The reviewer seat is not allowed to
post one.

**How the reviewer grades.** The reviewer seat grades every finding P0 to P3 and marks it either
**blocks** (a P0 or P1, a regression, silent data loss, the wrong agent woken, a false green on a
safety check, or a build or test the change breaks) or **issue** (real, but not worth another
round; usually P2 or P3). Any blocking finding makes the verdict "request changes". When every
finding is issue-tier, the verdict is "approve", and the review lists them under **Issues to
file**, each with a suggested title, for you to file.

**The cap.** The round limit (`cap`, default 3). The loop counts changes-requested verdicts from
reviewers, read from GitHub every time, never from a local counter. With `cap` 3 there are at most
three verdicts and two fix turns. The verdict that reaches the cap does **not** start another fix.
It hands the PR to adjudication instead.

**Adjudication.** When the cap is spent without an approval, the loop writes a marker for the PR.
If the loop has an adjudicator route (`init --adjudicator-route name-breach`), it also queues an
adjudicator turn. The adjudicator reads both sides and gives one ruling with a reason: ACCEPT (the
remaining findings do not block), REJECT (the work should not land as it is) or RESPEC (the two
sides disagree about the goal). It cannot merge, push or review. The ruling reaches you through the
watchdog's output and the observer feed, and is posted on the PR only if you configured a separate
adjudicator login. You make the final call. See
[adjudication](configuration.md#adjudication-the-isolated-ruling).

**Unattended fixer push.** Whether the fixer seat may push to a PR branch without you. It is **off
by default** for every loop. While it is off, a changes-requested verdict starts no fixer turn: the
verdict is held for you, and every surface (`explain`, `doctor`, the watchdog, the observer feed)
names the command that turns it on. You turn it on per loop with
`fixer-push --enable --acknowledge-pr-race`. The flag's name is deliberate: GitHub offers no way to
check "this PR is still open and still yours" and push in one step, so a PR can change in the
moment between the last check and the push. Read
[the push policy](security.md#unattended-fixer-push-policy-host-operator-not-github-owner-consent)
before turning it on.

**Attribution (signing).** Everything the loop itself posts is signed "Automated by
hermes-review-loop": a footer on the reviewer's review, the fixer's answers comment and the ruling
comment, and an `Automated-By:` trailer on the fixer's commits. The host adds it at the moment it
sends the write, so a seat cannot remove it. It never touches a PR or review a person writes. Turn
it off with `set --loop name --attribution off`. See [what the loop signs](operations.md#what-the-loop-signs).

### Watching the loop

**The watchdog.** A cron job (`init --schedule 15m`) that runs every 15 minutes, with no agent. One
job serves every loop on the machine. It reads GitHub directly and reports four kinds of stall: a
reviewer that never posted a verdict, a fixer that never pushed (or is held because fixer pushes
are off), a PR parked waiting for adjudication, and a spent cap with no escalation. It also starts
queued turns when a seat is free, retries what can be retried, repairs the loop's routes if another
program erased them, and warns a week before the read token expires. It says nothing for a paused
loop. Its messages go wherever `init --watchdog-deliver` sends cron output (the README example uses
`telegram`). See [the watchdog's four shapes](architecture.md#the-watchdogs-four-shapes).

**The observer feed.** An optional stream of short notices to a chat, one per transition: `opened`,
`handoff`, `verdict`, `approved`, `escalation`, `ruling`, `stall` and `closed`. Turn it on with
`--observer-profile arbiter`; the default destination is Telegram. It is read-only: no agent runs on
it, it holds no seat, and a failed delivery never blocks the loop. See [observer.md](observer.md).

### Diagnostic commands

| command | the question it answers | writes anything? |
|---|---|---|
| `doctor` | Is this install wired correctly? Profiles, models, token files, routes, hooks, cron job, clone. | no (except `--repair`) |
| `models` | Which models does a seat's Hermes profile offer? | no |
| `selftest` | Can an isolated turn actually run here? Runtime file, sandbox, account identities, model, ledger. | never writes to GitHub |
| `status` | What is this loop's shape right now? Seats, routes, live runs, queue, holds. | no |
| `explain` | Why is this one PR not moving, and what single event has to happen next? | no, byte for byte |
| `trace` | What did the gate decide for this one webhook delivery, and why? | no (runs on a temporary copy) |

`doctor` marks each line ✅ (verified), ❌ (absent or wrong), ⚠️ (could not be decided, or a decision
left to you) or ➖ (skipped). Every ❌ comes with the command that fixes it. See
[doctor](operations.md#preflight-doctor), [selftest](operations.md#verifying-the-isolated-setup-selftest),
[explain](operations.md#why-isnt-this-pr-moving) and
[trace](operations.md#why-did-that-delivery-start-nothing-trace).

## One pull request, start to finish

This walks one PR through the loop with every switch on, including unattended fixer pushes and an
adjudicator route. The accounts are `dev-account` (fixer), `rev-bot` (reviewer) and `reader-bot`
(reader). `cap` is 3.

### 1. The PR is opened

- **Who acts:** you, or your own coding agent, using the `dev-account` login. The loop's fixer seat
  never opens PRs; it only answers verdicts.
- **What GitHub sends:** a `pull_request` event with action `opened` to the reviewer's route.
  `ready_for_review` (a draft marked ready) and `reopened` work the same way. A draft PR is ignored
  until it is marked ready.
- **What the gate checks:** the PR targets the loop's base branch (default `main`), its author is
  in `fixers`, it is still open at this commit (read live as `reader-bot`), nobody has already
  posted a verdict at this commit, and the cap is not spent.
- **What happens:** the gate queues a reviewer turn in the run ledger and answers `[SILENT]`.
- **Where you see it:** an `opened` notice on the observer feed ("next: reviewer queued", or
  "held — " and the reason when the turn could not be queued), the run in `status`, and
  `explain --pr N`.

### 2. The reviewer turn runs

- **Who acts:** the `critic` profile's model, in the sandbox. Its review is posted as `rev-bot`.
- **What triggers it:** the worker picks up the queued turn as soon as the reviewer seat has a free
  slot and nobody else holds this PR.
- **What happens:** the worker re-reads the PR. If it closed or moved to a new commit, the turn is
  cancelled. Otherwise the agent reads the diff, builds and tests the code in `/work`, and submits
  one verdict through the broker. The broker checks the live PR again and posts the review on
  GitHub, signed.
- **Where you see it:** the review on the PR, as `rev-bot`. A turn that failed shows its reason in
  `status`, `explain`, and a watchdog notice.

### 3a. The reviewer approves

- **What GitHub sends:** a `pull_request_review` event (`submitted`, state approved) to the fixer's
  route.
- **What happens:** the fixer gate frees the reviewer's slot and starts the next queued review, if
  any. No fixer turn runs. The loop never merges.
- **Where you see it:** an `approved` notice ("next: you merge"). Merging is your step.

### 3b. The reviewer requests changes

- **What GitHub sends:** a `pull_request_review` event (`submitted`, state changes requested) to
  the fixer's route.
- **What the gate checks:** the review's author is in `reviewers`, the PR's author is in `fixers`,
  the review is on the PR's current commit and is still the newest verdict there, and no fix is
  already running for this commit. Then it counts: if this verdict reaches the cap, go to step 6.
- **What happens:** the reviewer's slot is freed. Then one of two things:
  - **Fixer pushes off (the default):** the verdict is **held**. No fixer turn starts. You can turn
    pushes on (the next watchdog sweep then starts the fix for this commit), or fix it by hand,
    push, and request review again.
  - **Fixer pushes on:** a fixer turn is queued.
- **Where you see it:** a `verdict` notice. When held, it says "next: you — fixer held" and names
  the command. `explain` shows `next: operator decision`, `doctor` shows
  `⚠️ fixer-push off`, and the watchdog reports one `fixer held` stall per commit.

### 4. The fixer turn runs

- **Who acts:** the `coder` profile's model, in the sandbox. Its push and comment appear as
  `dev-account`.
- **What happens:** the agent reads the verdict, edits files in `/work`, and asks the broker to
  push. The push is exact-head: if someone else pushed to the branch meanwhile, it is refused rather
  than overwriting their work. The broker refuses changes under `.github/` and a few other
  sensitive paths, which stay a human's job. Then the agent asks the broker to request a review,
  with a short file answering each finding. The broker posts those answers as one PR comment and
  requests a review from `rev-bot`.
- **Where you see it:** a new commit and an answers comment on the PR, and `rev-bot` listed as a
  requested reviewer.

### 5. The next round

- **What GitHub sends:** a `pull_request` event with action `review_requested`. The new commit's
  own `synchronize` event does nothing: a push alone never starts a review, so half-finished work
  is never reviewed. The request is the signal.
- **What the gate checks:** the request names the reviewer seat's login (`rev-bot`), and its
  sender is in `fixers` or `reviewers`. A request from anyone else is ignored.
- **What happens:** the fixer's slot is freed (the request ends the fixer's turn) and a reviewer
  turn is queued for the new commit. The loop returns to step 2.
- **Where you see it:** a `handoff` notice ("fix pushed · review requested · round 2/3").

### 6. The cap is spent

- **What triggers it:** the verdict that brings the count of changes-requested verdicts to the cap
  (the third, with `cap` 3).
- **What happens:** no fix turn starts. The gate writes a breach marker for this PR and commit, and,
  because the loop has an adjudicator route, queues an adjudicator turn.
- **Where you see it:** an `escalation` notice ("3/3 verdicts, no approval"), and the escalation
  line in `explain`.

Without an adjudicator route, the marker is written, nothing else runs, and the escalation notice
says the next step is you.

### 7. The adjudicator rules

- **Who acts:** the adjudicator's profile (here `arbiter`), in the sandbox, with a read-only copy of
  the code.
- **What happens:** the worker checks again that the PR is open, unchanged and still unapproved.
  The agent reads the reviewer's findings and the fixer's answers and submits one ruling through
  the broker. The host records it, sends a `ruling` notice, and puts the full ruling with its reason
  in the watchdog's output. If you configured an adjudicator login, the ruling is also posted on the
  PR as that account.
- **Where you see it:** the `ruling` notice (verdict only), the watchdog's next message (with the
  reason), and the PR comment if configured.

### 8. You decide

The loop has done what it can. Merge, close, push a fix yourself, or raise the cap with
`set --loop name --cap N`. When the PR closes or merges, the reviewer gate cleans up the PR's local
files (checkouts, build folders, the loop's own markers) and sends a `closed` notice.

## The switches

Nothing happens until several things are in place. Each one exists so that a half-finished install
cannot spend money or write to GitHub by accident. When one is missing, the loop does not fail
silently: the event is held or never arrives, and `doctor` or `explain` tells you which.

| switch | what it does | how you turn it on | when it is off |
|---|---|---|---|
| loop config | tells the loop which repository, accounts, profiles and limits to use | `hermes review-loop init` | no gate recognises the repository; nothing runs |
| routes | let the gateway accept GitHub's events and run the gates | written by `init` | GitHub's delivery fails; `doctor` shows the route ❌ and the fix |
| runtime file | lets the worker start isolated turns | `hermes review-loop setup` detects the paths and writes `~/.hermes/review-loop-runtime.json` (or write it yourself) | an eligible event is **held** with the reason; `explain` shows it on its queue line; once the file exists, the next watchdog sweep (or `drain`) sends it to the gate again |
| hooks armed | lets GitHub send events at all | `hermes review-loop arm --loop name` | GitHub sends nothing; the watchdog stays silent; `explain` says the hooks are paused |
| unattended fixer push | lets the fixer answer a verdict by pushing | `fixer-push --enable --acknowledge-pr-race` | changes-requested verdicts are **held** for you; `explain` says `operator decision` |
| issue triage (opt-in) | labels new issues from allowlisted authors | `triage --enable` with `--profile`, `--author` and `--labels` | issues are not read at all |
| issue fixes (opt-in) | a maintainer's label hands an issue to the fixer | `triage --enable --fix-label LABEL --maintainer LOGIN`; needs triage on and unattended fixer pushes on | the label does nothing |

Two more are not strictly required but you almost certainly want them:

- **The watchdog** (`init --schedule 15m`). Without it, nobody reports stalls, and held or
  queued work only moves when another event arrives or you run `drain`.
- **The adjudicator route** (`init --adjudicator-route name-breach`). Without it, a spent cap only
  writes a marker and waits for you. `setup` does not turn adjudication on. To add it to a loop
  that `setup` made, add an `adjudicator` block (with `route` and `profile`) to the loop file, as
  described in [adjudication](configuration.md#adjudication-the-isolated-ruling), then run
  `hermes review-loop apply --loop name --recreate-routes`.

Triage and issue fixes are covered step by step in [issues.md](issues.md).

The recommended order is the one in the README's [first run](../README.md#first-run-in-order):
write the runtime file, run `doctor`, run `selftest`, decide on fixer pushes, and arm last. In
short:

```bash
hermes review-loop doctor   --loop name
hermes review-loop selftest --loop name --no-model
hermes review-loop arm      --loop name
```

To turn on unattended fixer pushes, after reading the push policy:

```bash
hermes review-loop fixer-push --loop name --enable --acknowledge-pr-race
```

To stop the loop without removing anything:

```bash
hermes review-loop arm --loop name --pause
```

## What the agents can and cannot do

The agents run untrusted input: the PR's own code, written by another model. So the loop assumes
an agent may try anything, and limits what "anything" can reach.

| an agent in a turn | |
|---|---|
| holds a GitHub token | ❌ none, ever. Tokens stay with the broker on the host. |
| holds a model key | ❌ the model is reached through a host proxy on a socket; the sandbox sees only a dummy key |
| reaches the network | ❌ none. Rust dependencies pinned in `Cargo.lock` are fetched by the host before the turn and mounted read-only. |
| reads your files | ❌ only a copy of the PR's code, its diff and a throwaway home folder. Even `/etc` is written fresh by the host for each turn, with one user and one group and nothing copied from your machine's `/etc` |
| writes to GitHub | only through the broker, and only what its seat allows |
| writes more than its share | ❌ the reviewer gets one review; the fixer gets one push and then one review request (or, when the host could not show it the whole change, only its answers comment); the adjudicator gets one ruling; triage gets one set of labels (and a comment if allowed); the issue fixer gets one PR or one issue comment |
| pushes to the wrong commit | ❌ the push is refused if the branch moved |
| edits `.github/`, `.gitmodules`, `.gitattributes` or `CODEOWNERS` | ❌ refused by the broker |
| merges | ❌ no seat has a merge operation |
| removes the "Automated by" signature | ❌ the host adds it after the agent is done |

Why this matters to you:

- **A bad turn cannot do much damage.** The worst a misbehaving reviewer can do is post one wrong
  review. The worst a fixer can do is push one bad commit, which the next review sees.
- **Your credentials stay yours.** A PR that contains a malicious build script runs in a sandbox
  with nothing worth stealing.
- **Merging stays a human decision.** The loop can tell you a PR is approved; it cannot ship it.
- **"Unknown" is never treated as "done".** If a write's outcome is unclear (a timeout during a
  push, say), the run is marked `uncertain` and is never replayed. You reconcile it by hand.

## Where things live

All paths below assume the default Hermes home, `~/.hermes` (set `HERMES_HOME` to move it).

| what | where | notes |
|---|---|---|
| loop config | `~/.hermes/review-loops.d/<id>.json` | one per loop; plain JSON. `REVIEW_LOOP_CONFIG_DIR` moves the folder. [Every key](configuration.md) |
| routes | `~/.hermes/webhook_subscriptions.json` | the gateway's own route list, shared with other plugins. `REVIEW_LOOP_SUBS` points elsewhere |
| runtime file | `~/.hermes/review-loop-runtime.json` | you write it; mode 600 |
| per-loop state | `~/.hermes/state/review-loops/<id>/` | default `state_dir`: seat claims, the held/queued list, in-flight marks, breach markers, watchdog memory and the files below. [The files](configuration.md#state-files-per-loop-under-state_dir) |
| isolated runs | `<state_dir>/isolated-runs/` | each isolated turn's own working folder on the host |
| dependency cache | `<state_dir>/deps/` | Rust crates the host fetched for `Cargo.lock`, mounted read-only into the sandbox; mode 700 |
| route intent | `<state_dir>/route-intent.json` | the routes this loop should have, so `doctor --repair` and the watchdog can put back a route another program erased |
| broker audit | `<state_dir>/broker-audit.jsonl` | one line per GitHub write the broker made |
| gate shims | `scripts/` in each serving profile's home (`~/.hermes/scripts/` for `default`, else `~/.hermes/profiles/<name>/scripts/`) | small files named after each gate (`gate_reviewer.py`, …); the gateway only runs scripts from there, and each one runs the plugin's own gate |
| run ledger | `~/.hermes/state/review-loop-runs.sqlite` | every isolated turn, shared by all loops |
| worker log | `~/.hermes/state/review-loop-runs.sqlite.workers.log` | a detached worker's errors |
| pacing | `~/.hermes/state/review-loop-pacing.json` | usage-window holds and daily turn counts; never a credential |
| token files | `~/.hermes/keys/<login>-pat` (convention) | one per login, mode 600; the config holds only the path |
| watchdog shim | `~/.hermes/scripts/review-loop-watchdog.py` | the small script the cron job runs; it calls the plugin's watchdog |
| Hermes profiles | `~/.hermes/profiles/<name>/` (`~/.hermes` itself for `default`) | each seat's model and model credentials |

Inside a loop's state folder, the files you are most likely to meet are `locks.json` (who holds
which PR), `pending.json` (turns held or waiting, with the reason), `inflight.json` (heads a turn was
recently started for, so a burst of events starts it once), `breach.json` (PRs that spent their cap), `observations.json` (the observer
feed's record of what it sent), `github-reads.json` (the last GitHub read that failed) and
`gate-failures.json` (a gate that crashed or timed out). You should not need to edit
any of them: `status` and `explain` read them for you.

To see the run ledger, run this from the plugin's directory:
`python -m review_loop.run_supervisor status ~/.hermes/state/review-loop-runs.sqlite`.

## Where to go next

- Setting up the accounts and tokens, step by step: [accounts.md](accounts.md).
- Every command and flag: [commands.md](commands.md).
- A PR is stuck, or a check fails: [troubleshooting.md](troubleshooting.md).
- Running a loop day to day: [operations.md](operations.md).
- Every config key: [configuration.md](configuration.md).
- The design reasons in depth: [architecture.md](architecture.md).
- Notices on your phone: [observer.md](observer.md).
