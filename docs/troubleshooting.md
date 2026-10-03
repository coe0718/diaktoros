# Troubleshooting

Your loop is not doing what you expected. This page starts from what you **see**, not from how the
loop is built. Each section says what the symptom looks like, the likely causes (the most common
first), how to confirm which one it is, and the exact command that fixes it.

New to the words used here (seat, gate, route, hook, turn, head)? The
[glossary](concepts.md#glossary) explains each one. Every command and flag is in the
[command reference](commands.md).

The examples use the repo `owner/name`, the loop id `name`, the fixer account `dev-account`, the
reviewer account `rev-bot`, the reader account `reader-bot`, and the Hermes profiles `drey`
(fixer), `vex` (reviewer) and `tuck` (adjudicator/observer). Replace them with yours.

## First, three commands

Most problems are answered by one of these three. They only read; none of them changes the loop.

### `doctor`: is the install wired correctly?

```bash
hermes review-loop doctor --loop name
```

`doctor` checks the installation itself: profiles, token files, the runtime file's paths, the
webhook routes, the repo hooks, the gate scripts, the watchdog cron job. It prints one line per
check. ✅ is fine. ❌ is broken, and the next line starts with `fix:` and gives the command. ⚠️ means
"could not decide from here" or "a decision that is still yours" (for example, fixer pushes are
off). It writes nothing and fires nothing.

Run it first whenever anything looks wrong. If it shows any ❌, fix those before you look further.
Details: [Preflight: `doctor`](operations.md#preflight-doctor).

### `explain --pr N`: why is this one PR not moving?

```bash
hermes review-loop explain --loop name --pr 12
```

`explain` looks at one PR the way the gates do and says what is holding it. Read two lines first:

- `blocked:` is every reason the PR is held. There can be several.
- `next:` is always the last line. It names the one thing that has to happen next, and often the
  exact command.

The other lines show the facts it used: `hooks:` (armed or paused), `queue:`, `run:` (failed,
waiting or uncertain turns at this PR), `gates:` (gate crashes), `github:` (failed GitHub reads).
`--loop` can be left out when you have only one loop. Details:
[Why isn't this PR moving?](operations.md#why-isnt-this-pr-moving)

### `trace --delivery`: why did that one webhook start nothing?

When a gate turns an event down, GitHub still shows the delivery as a success. Every delivery the
gate handled looks the same on GitHub's *Recent Deliveries* page:
`200 {"status": "ignored", "reason": "script"}`. That reply comes back for a delivery that queued a
review **and** for one that was refused. Hermes does not log the reason for a refusal (#209).

`trace` answers for one delivery. Copy the delivery's GUID (the `X-GitHub-Delivery` value) or its
numeric id from *Settings → Webhooks → (the hook) → Recent Deliveries* in GitHub, then:

```bash
hermes review-loop trace --loop name --delivery 40ac7f60-be4a-11f1-8969-4f6489738e63
```

It runs the real gate script on that payload, against a temporary copy of the loop's state.
GitHub reads are real; anything that would write (a GitHub write, a route POST, a worker start) is
listed as `would …` instead of done. The last useful line is `outcome:`, which is one of:

- `would start a reviewer run` (or `would queue a … run`): the gate would act on it now.
- `held — <why>`: the gate took it, but something holds the turn.
- `declined — <why>`: the gate turned it down, and `<why>` is the gate's own reason.

Reading deliveries needs hook read access. `trace` reads as the loop's reader account unless you
pass `--admin-token LOGIN`, which must name a login that already has a token file mapped on this
loop. Details: [`trace`](operations.md#why-did-that-delivery-start-nothing-trace).

`selftest` is the fourth tool. It checks the isolated turn path (sandbox, models, identities)
rather than the wiring; see [section 4](#doctor-is-all-green-but-selftest-fails).

## `setup` stopped before the loop was live

`setup` runs five steps (runtime file, loop, watchdog, checks, arm) and stops at the first one that
fails, saying which. **Re-running it is safe**: whatever is already in place is kept, so a second
run only does what is missing. `--dry-run` shows every step and writes nothing:

```bash
hermes review-loop setup --repo owner/name --dry-run
hermes review-loop setup --repo owner/name
```

What it prints, and what to do:

- **`setup asks questions; with no terminal, pass --yes …`.** You ran it from a script. Pass
  `--yes` and give the answers as flags (`--reviewer`, `--fixer`, the profiles, the token files,
  `--read-token`, `--host`, …); the settings form fills in the rest.
- **A `❌` under `1. Runtime paths`** (`❌ venv … — pass --venv PATH`) and
  `not written: every path must check out first`. It could not find or check a path. Pass the one
  it names (`--source`, `--venv`, `--runtime` or `--rust`) and run it again. See
  [runtime file problems](#runtime-file-problems).
- **`setup stopped: init refused the answers`.** The `init` dry run above that line names the
  problem: a profile that does not exist, two roles on one account or token file, a token file that
  is not private. Fix it and run `setup` again.
- **`loop 'name' serves owner/other, not owner/name — pass --id …`.** A loop with that id already
  exists for another repository. Pass `--id` with a new id.
- **`setup stopped before arming: fix each ❌ above`.** `doctor` or `selftest --no-model` (step 4)
  found a problem, or the watchdog job could not be created. Each ❌ line has a `fix:` line under
  it. Fix them, then run `setup` again.
- **`not armed. When ready: hermes review-loop arm --loop name`.** Not an error. `setup` arms the
  hooks only when you say yes (or pass `--arm` with `--yes`). Run the `arm` command it printed
  when you are ready.

`setup` never turns on adjudication or issue triage. See
[the cap was spent and no ruling came](#the-cap-was-spent-and-no-ruling-came) and
[issue triage](issues.md).

## I opened a PR or requested a review, and nothing happened

**What you see.** No review appears. `status` shows no live run. GitHub's *Recent Deliveries* is
either empty, or shows `200 {"status": "ignored", "reason": "script"}`.

Work through these in order. Start with `explain`:

```bash
hermes review-loop explain --loop name --pr 12
```

### The hooks are still paused

`init` creates the two repo hooks **paused**, so nothing fires until you arm them (unless you
passed `--arm`). A paused hook sends nothing, so *Recent Deliveries* stays empty.

**Confirm.** `explain` shows `hooks:      PAUSED — seat route(s) without an active repo hook: …`
and its `next:` line says `re-arm the loop — hermes review-loop arm --loop name …`. `doctor` shows
`PAUSED — nothing fires until …` on a `hook:` line, and ends with
`the repo hooks are paused, so nothing fires yet: run selftest, then …`.

**Fix.**

```bash
hermes review-loop arm --loop name
```

`arm` reads each hook back from GitHub and prints what GitHub now shows. Flipping a hook needs hook
*write* access; if `arm` prints `PATCH failed (HTTP 403 …)`, see
[token and identity refusals](#token-and-identity-refusals). If there are no hooks at all (you ran
`init` without `--hooks`), `hermes review-loop apply --loop name --hooks` creates them, paused,
and then you `arm`.

### The review was requested by an account the gate does not accept

This is the most common surprise. The reviewer gate starts a review on `review_requested` only
when **both** are true:

1. the request names the reviewer seat's login (`rev-bot`), and
2. the account that *sent* the request is in the loop's `fixers` or `reviewers` list.

If you, the repo owner, click "Request review" from your own account, the sender is `owner`. That
account is usually in neither list, so the gate drops the event. GitHub still shows a 200.

**Confirm.** Trace the delivery:

```bash
hermes review-loop trace --loop name --delivery 40ac7f60-be4a-11f1-8969-4f6489738e63
```

```text
  delivery:  pull_request/review_requested · PR #12 · sender owner · author dev-account · head 4f1c2ab · base main · not draft · requested rev-bot
  gate log:
    [review-loop] sender owner is not a fixer
  outcome:   declined — sender owner is not a fixer
```

The message says "not a fixer" even though accounts in `reviewers` are accepted too. A request for
someone other than the reviewer seat shows `review requested from <login> — not this seat`.

**Fix.** Either:

- request the review from the **fixer's** account (`dev-account`), the way the loop itself does, or
- on the PR, click **Convert to draft**, then **Ready for review**. The `ready_for_review` event is
  judged by the PR's **author**, not by who clicked, so it works from any account as long as the
  author is a fixer.

### A push alone never starts a review

The reviewer gate ignores `synchronize` (a push) on purpose. The fixer pushes, *then* asks for a
review; the explicit request is the only signal that means "review me now". Intermediate pushes
cost nothing.

**Confirm.** `trace` on the push's delivery shows `declined — action 'synchronize' is not a review
trigger`.

**Fix.** After the push, request a review of `rev-bot` from the fixer's account (see above). Or, for
a brand-new PR, open it (or mark it ready) after the commits are there.

### The PR is a draft, targets another branch, or was opened by someone else

The reviewer gate only serves PRs that are not drafts, target the loop's base branch (`main` by
default), and were opened by one of the loop's `fixers`.

**Confirm.** `trace` shows one of `declined — draft PR`, `declined — base is not main`, or
`declined — author <login> is not a fixer for this loop`. `explain` shows
`blocked:    draft PR: the reviewer gate stays silent until ready_for_review`, or
`blocked:    the author <login> is not one of this loop's fixers (dev-account)`.

**Fix.** Mark the PR ready for review. A PR from someone outside `fixers` is out of scope by
design: the loop reviews the fixer's work. To change who counts as a fixer, see
[configuration](configuration.md).

### The turn was taken but held: no runtime file

When the gate accepts an event, it puts an isolated turn in the host run ledger. That needs the
private runtime file `~/.hermes/review-loop-runtime.json`. Without it (or with a file that is not
mode 0600) no turn can start, so the gate parks the PR in the seat's queue with the reason, and
GitHub still sees a 200.

**Confirm.** `explain` shows the reason on its `queue:` line and on a `blocked:` line, for
example `isolated worker unavailable: FileNotFoundError: [Errno 2] No such file or directory: …`
or `isolated worker unavailable: ValueError: production config must be a private regular file`.
(The `blocked:` line starts with `no capacity: queued with the reviewer seat`; the reason after
the dash is the real one.) `trace` shows `outcome:   held — #12 @ 4f1c2ab reviewer held: isolated
worker unavailable: …`. `doctor` shows `❌ runtime:file  no runtime file at …`.

**Fix.** Write the file (see [runtime file problems](#runtime-file-problems)), check it with
`selftest`, then start the queued turn now instead of waiting for the next watchdog sweep:

```bash
hermes review-loop selftest --loop name --no-model
hermes review-loop drain --loop name --seat reviewer
```

### The head already has a review, or one is already running

The gate never reviews the same head twice, and never starts a second review for a head that has
one out.

**Confirm.** `trace` shows `declined — head 4f1c2ab already has a reviewer's verdict` or
`declined — a review for head 4f1c2ab is already out`.

**Fix.** Nothing to fix. A new review needs a new head: push a commit, then request a review. To
follow a running review, use `hermes review-loop status --loop name`.

### The event never reached the gate

If *Recent Deliveries* shows something other than a 200, the gate never ran:

- **no HTTP answer, a timeout, connection refused**: the Hermes gateway is not running or not
  reachable at the loop's `host`. `doctor` shows `❌ gateway  configured gateway unreachable`.
  Check that the gateway process is running, and that its public address (your tunnel or reverse
  proxy) reaches it.
- **401 or 403**: the hook signs with a secret the route does not hold, usually a hook left over
  from an earlier install. `doctor` fails that `route:` or `hook:` line and names the fix. To test
  the signature right now instead of waiting for a real event, ask GitHub to ping each loop hook
  (its one GitHub write; the gateway checks the signature and ignores the ping):

  ```bash
  hermes review-loop selftest --loop name --no-model --ping --admin-token reader-bot
  ```

  Each hook gets a line: `signature accepted`, or `HTTP 401 — signature rejected`. See
  [routes changed under you](#routes-were-overwritten-or-changed-under-you).
- **404**: the hook posts to a URL no route serves (another profile, an old gateway, a trailing
  slash). `doctor` reports the hook as a mismatch with the cause.

A 200 whose gate *script* is missing is a quieter variant: the gateway cannot find the gate in the
serving profile's `scripts/` directory. `doctor` shows it as `❌ gateway-script:<route>  the
gateway would drop every event: …`, with the fix `hermes review-loop apply --loop name` (which
writes the gate shims).

## Changes were requested but the fixer never ran

**What you see.** The reviewer posted "changes requested". No fix commit follows.

**Likely cause: unattended fixer pushes are off.** That is the default for a new loop. While they
are off, a changes-requested verdict starts **no** fixer turn, because a turn that cannot publish
would only spend a model conversation. The verdict is held for you instead.

**Confirm.** Any of these say so:

- `explain` ends with `next:       operator decision: unattended fixer pushes are off for this loop,
  so the changes-requested verdict at head 4f1c2ab starts no fixer turn. …`
- `doctor` shows `⚠️ fixer-push  off — the fix leg cannot run: changes-requested verdicts are held
  for you and no fixer turn starts. …`
- the watchdog reports one `fixer held — unattended fixer pushes are off` stall per head, and the
  observer's `verdict` notice says `next: you — fixer held …`.

**Fix.** Read the PR-metadata race this acknowledges first
([README](../README.md) and
[security](security.md#unattended-fixer-push-policy-host-operator-not-github-owner-consent)).
Then:

```bash
hermes review-loop fixer-push --loop name --enable --acknowledge-pr-race
hermes review-loop drain --loop name --seat fixer
```

The verdict that was already held needs no new review. `drain` (or the next watchdog sweep) checks
that it is still the latest verdict at the PR's current head and starts the fix run. If you would
rather keep pushes off, fix the PR by hand, push, and re-request the review.

**Other causes**, which `explain` names on a `blocked:` line:

- **The fixer run was cancelled by the push policy.** A fixer turn enqueued while pushes were off
  is cancelled when it is claimed, and a redelivered webhook never re-admits it. After enabling
  pushes, re-admit it yourself:

  ```bash
  hermes review-loop retry --loop name --pr 12 --seat fixer
  ```

- **The fixer seat is busy.** Capacity is per seat, and fixes run one at a time by default.
  `explain` says `no capacity: …` and names the PRs holding the seat. The fix starts when a slot
  frees.
- **The cap is reached.** After `cap` verdicts without an approval (3 by default) the PR goes to
  adjudication instead of another fix. `trace` shows `declined — cap reached on #12 — handed to
  adjudication instead of a fix`. An adjudicator only rules if the loop has an adjudicator route;
  see [the cap was spent and no ruling came](#the-cap-was-spent-and-no-ruling-came).

## The cap was spent and no ruling came

**What you see.** A PR reached its verdict cap. No fixer turn starts and no adjudicator ruling
arrives. `explain` ends with `next:       human adjudication: this loop has no adjudicator route, so
the breach marker is the only record — rule by hand, then merge or close`.

**Why.** Adjudication is on only when the loop has an adjudicator route (`--adjudicator-route NAME`
at `init`; any name works, `name-breach` by convention). Without one, the loop writes the breach
marker, sends the `escalation` notice (`next: you`), and stops: the decision is yours. `setup` never
turns adjudication on, and `set` has no flag for it.

**Fix.** For the PR that is already parked, decide by hand: merge it or close it. To have an
adjudicator rule on future breaches, add an `adjudicator` block to the loop file
`~/.hermes/review-loops.d/name.json`:

```json
"adjudicator": {"route": "name-breach", "profile": "tuck"}
```

then write the missing route and check it:

```bash
hermes review-loop apply --loop name --recreate-routes --dry-run
hermes review-loop apply --loop name --recreate-routes
hermes review-loop doctor --loop name
```

`doctor` then shows `route:name-breach … adjudication wake` and the `model:adjudicator` line. The
route needs no repo hook: its gate always answers `[SILENT]`, and the ruling runs as an isolated
turn the host enqueues when a PR spends its cap. More:
[adjudication](configuration.md#adjudication-the-isolated-ruling).

## Runtime file problems

The isolated worker reads one private host file, `~/.hermes/review-loop-runtime.json`. It names four
host paths:

```json
{"source": "/path/to/hermes-agent", "venv": "/path/to/hermes-agent/venv",
 "runtime": "/path/to/python-runtime", "rust": "~/.rustup/toolchains/stable-x86_64-unknown-linux-gnu"}
```

The full description is in
[configuration](configuration.md#runtime-file-and-seat-models-review-loop-runtimejson). Check the
file with `selftest`, not only `doctor`:

```bash
hermes review-loop selftest --loop name --no-model
```

### No runtime file

**What you see.** Turns are held (see [above](#the-turn-was-taken-but-held-no-runtime-file)).
`selftest` shows `❌ runtime:file  no file at …` with a `fix:` line that writes a template.

**Fix.** Let `setup` detect the paths and write it (it keeps everything else as it is):

```bash
hermes review-loop setup --repo owner/name
```

Or create it private by hand, then fill in the four paths:

```bash
(umask 077; touch ~/.hermes/review-loop-runtime.json); chmod 600 ~/.hermes/review-loop-runtime.json; $EDITOR ~/.hermes/review-loop-runtime.json
```

### The file is not mode 0600

The worker refuses a runtime file that group or others can read. `selftest` shows
`❌ runtime:file  … is mode 644 (group/other can read it)` and `fix: chmod 600 …`. It also refuses
a symlink (`… is a symlink`) and a file owned by another user. Fix with `chmod 600` on the real
file, as the `fix:` line says.

### The `runtime` value is wrong

`runtime` is the directory of the Python installation the venv was made from. The sandbox mounts
**only** that directory for Python. So it has to contain both the place the venv's interpreter
link points to, *as written*, and the place that link finally resolves to.

**What you see.** `selftest` shows:

```text
  ❌ runtime:runtime        the venv interpreter points to <path>, outside <runtime>
      fix: set runtime to <dir> (the directory holding the interpreter's install): the sandbox mounts runtime at its own path and nothing else, so a link leaving it cannot start
```

**Why it happens.** It is easy to set `runtime` too narrow. With a uv-managed Python, the venv's
`bin/python` points into a directory like `~/.local/share/uv/python/cpython-3.11-linux-x86_64-gnu/`,
which is itself a link to `~/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu/`. If you ran
`readlink -f` on the interpreter and used the directory it printed, you get only the
`cpython-3.11.15-…` directory. The link as written goes through `cpython-3.11-…`, which is outside
it. The right value is the parent that holds **both**: `~/.local/share/uv/python`.

**Fix.** Use the directory the `fix:` line names. It is computed from the real link, so it is the
right one. Edit `runtime` in the file and run `selftest` again.

### `doctor` and `selftest` disagree about the runtime file

`doctor` checks less. For each of the four paths it only asks: does it exist, and does it contain
the expected file (`run_agent.py`, `bin/python`, `bin/cargo`)? It does not follow the venv's
interpreter link. So `doctor` can show `✅ runtime:runtime  runtime = …` while `selftest` shows ❌.

**Trust `selftest`.** It checks what the sandbox actually needs.

## doctor is all green but selftest fails

This is expected, not a contradiction. The two tools answer different questions:

- `doctor` asks "is this installation wired?" It reads each seat profile's model settings without
  resolving any credential, and it never starts the sandbox or calls a model.
- `selftest` asks "can an isolated turn really run?" It resolves each seat's model the way the
  worker does (running Hermes's own resolver in the runtime venv), builds the real bubblewrap
  sandbox, checks each account's token against GitHub, and (without `--no-model`) sends one tiny
  request to each seat's model.

So a problem that only shows when the resolver or the sandbox runs (an interpreter link outside
`runtime`, a missing Python package, an expired OAuth login, unprivileged user namespaces disabled)
is invisible to `doctor`. **For the isolated path, `selftest` is the authority.**

Run its steps in order, each one costing a little more:

```bash
hermes review-loop selftest --loop name --no-model            # runtime, sandbox, identities, ledger: free
hermes review-loop selftest --loop name --pr 12               # + one tiny completion per seat model
hermes review-loop selftest --loop name --pr 12 --live-turn   # + one real reviewer turn, never posted
```

Each ❌ line is followed by a `fix:` line. `selftest` never writes to GitHub, except with `--ping`,
which asks GitHub to ping the loop's hooks. What each step proves
is in [`selftest`](operations.md#verifying-the-isolated-setup-selftest).

## A turn failed, is waiting, or was retried

Every isolated turn is a row in the host run ledger. When one fails, the loop first asks: *could it
have written to GitHub?* It answers from its own write-ahead records, never from an exit code. A
turn that wrote nothing can be tried again safely. One that may have written cannot (see
[uncertain](#a-run-says-uncertain)).

**Confirm.** `explain` prints each failed, waiting or uncertain turn on a `run:` line, with the
reason and the next step, and the last lines of the turn's output under it:

```bash
hermes review-loop explain --loop name --pr 12
hermes review-loop status --loop name
```

### Automatic retries (backoff)

A turn that failed for a reason that may be temporary (a model 429 or 5xx, a network error, a
GitHub read that failed) goes to `waiting` and is tried again by itself. The waits are 2 minutes,
4 minutes, then 8 minutes. The fourth failure makes it `failed`, and the watchdog sends you a
notice with the real reason.

`explain` shows a waiting turn as
`attempt <n> of 4 due in <N>s (starts on the next event or armed watchdog sweep)`. A due retry
starts on the next event for the PR, when another run finishes, or on the next armed watchdog
sweep. A redelivered webhook for the same head also re-arms a failed turn, up to 8 failures.

### Re-arming a failed turn yourself

When a turn has `failed` and its reason says `no external write`, fix the cause if it is not
temporary, then:

```bash
hermes review-loop retry --loop name --pr 12
```

`retry` re-arms the PR's failed and waiting turns at its newest head, resets their retry count and
starts the worker. Add `--seat reviewer`, `--seat fixer`, `--seat adjudicator`, `--seat triage` or
`--seat issue_fixer` to pick one seat (for triage and issue fixes, `--pr` is the issue number). It
prints `re-armed (was failed: …)` for each turn and then `worker started`. It refuses a turn that may have
written and prints the `reconcile` command instead. A turn cancelled because the head moved or the
PR closed is not offered: the new head gets its own turn.

### Killed at the turn budget

Each turn has a wall clock, 900 seconds by default. A turn that runs past it is killed.

**What you see.** The reason reads `isolated turn failed: TimeoutExpired — killed at the <N>s turn
budget (sandbox stopped 30s past it)`, and the next step reads `no external write — raise the
turn budget (now <N>s): …`.

Such a turn is **not** retried automatically, because the same budget would most likely run out
again. Raise the budget for that seat (in seconds, 60 to 14400), then re-arm:

```bash
hermes review-loop set --loop name --reviewer-turn-budget 1800
hermes review-loop retry --loop name --pr 12 --seat reviewer
```

Use `--fixer-turn-budget` for the fixer, or `--turn-budget` for every seat. The re-armed turn runs
on the new budget. If a push finished while the turn was being stopped, the reason says
`after it wrote (…) — final: never replayed; a new head gets a fresh turn`, and `retry` refuses
it. More: [Turn budget](configuration.md#turn-budget-how-long-one-turn-may-run).

### Held for a usage window (429)

A seat on a subscription plan (Codex, a Claude subscription and others) shares its usage window
with your own use of that account. When the provider answers 429 and says when the window
reopens, the turn waits for that time **without spending a retry**, and new turns for the same
account wait too.

**What you see.** The turn is `waiting`, and its reason is
`held: <seat> usage window (<provider>) — resumes <HH:MM>`. The next step reads
`waits for the reset, no retry spent — starts in <N>s on the next event or armed watchdog sweep`.
`status` lists the hold as `held: <provider> as profile <profile> until <HH:MM> (…)`.

**Fix.** Nothing; it resumes by itself. A bare 429 that names no reset time is treated as an
ordinary failure with the ordinary backoff. Details:
[Pacing](operations.md#pacing-usage-windows-and-daily-caps).

### Held by a daily cap

If you set a daily cap, turns past it wait until local midnight, also without spending a retry.
The reason is `held: <seat> daily turn cap (<N>) reached — resumes <date> 00:00`, and `status`
shows today's count, for example `reviewer 20/20 today · fixer 3/no cap today`. To raise or remove
the cap (`0` removes it):

```bash
hermes review-loop set --loop name --reviewer-daily-turns 40
hermes review-loop set --loop name --fixer-daily-turns 0
```

`status` shows the count line only for the reviewer and fixer seats, and only when one of them
has a cap. Issue triage has its own cap, shown by `hermes review-loop triage --loop name`
(`daily cap N`); a triage turn past it waits with
`held: triage daily turn cap (<N>) reached — resumes …`. Change it with:

```bash
hermes review-loop triage --loop name --enable --daily-turns 40
```

Issue-fix turns have no daily cap.

## A run says "uncertain"

**What it means.** The worker may have written to GitHub (posted a review, pushed a commit, posted
a comment), but the loop cannot prove whether that write landed. Typical causes: the worker
process was lost mid-turn, a push was started but never confirmed, or a review was sent but never
read back. The loop will **never** replay such a turn, because a replay could post twice or push
twice. An uncertain turn also keeps its seat claim, so that PR's seat stays occupied until you
act.

**What you see.** `explain` shows a `run:` line with the state `uncertain` and a next step like
`may have written (<what>) — inspect the PR, then python -m review_loop.run_supervisor reconcile
DB <run id> --reason REASON --acknowledge-no-live-worker`. `retry` refuses it with the same
instructions.

**How to inspect.**

1. Open the PR on GitHub. Look for what the run may have done at that head: a review from
   `rev-bot`, a commit from `dev-account`, a comment. Note what is there.
2. Make sure no worker for that run is still alive. The ledger lists the run and its state:

   ```bash
   python -m review_loop.run_supervisor status ~/.hermes/state/review-loop-runs.sqlite
   ```

   Run it from the plugin's directory (it is a module of this plugin). It prints the failed,
   waiting and uncertain rows as JSON, with the run id, reason and output tail.

**How to reconcile.** Once you have inspected the PR and know no worker remains:

```bash
python -m review_loop.run_supervisor reconcile ~/.hermes/state/review-loop-runs.sqlite <run-id> --reason 'external writes inspected' --acknowledge-no-live-worker
```

It prints `reconciled`. The run becomes `failed` with your reason, its seat claim and in-flight mark
are freed, and **nothing is replayed**. It refuses while the run's worker process still exists.
Then carry on from what you saw on GitHub: if the review or push landed, the loop continues from
there; if it did not, push a new commit or request a review again, and the new head gets a fresh
turn.

## Duplicate Telegram messages, or every stall twice

**What you see.** The same stall arrives twice in Telegram, worded differently.

**Why.** Two separate things can talk to you:

- the **watchdog** (the cron job) reports *trouble*: stalls, stuck state, failed runs. It goes
  wherever `--watchdog-deliver` sent it at `init`.
- the **observer** reports *progress*: opened, handoff, verdict, approved, escalation, ruling,
  closed. It goes to the observer's `--observer-deliver` target.

The `stall` event goes to **both**. If both deliver to the same Telegram chat, you see each stall
twice.

**Fix.** Drop `stall` from the observer's events, so stalls come only from the watchdog:

```bash
hermes review-loop set --loop name --observer-events opened,handoff,verdict,approved,escalation,ruling,closed
```

The observer never sends the same transition twice on its own: redelivered webhooks and re-run
sweeps are deduplicated. More: [the observer feed](observer.md).

## "Claude Code is not installed" in resolver output

**What you see.** A message saying Claude Code is not installed shows up in the output when a seat's
model is resolved (for example during `selftest`).

**Why.** The plugin runs Hermes's model resolver with a minimal `PATH` (`/usr/bin:/bin`), so Hermes
does not find a `claude` binary that lives elsewhere and says so. The resolver does not need it.
For ordinary API-key and OAuth seats this message is **harmless**. Nobody needs to chase it.

The one exception is a seat whose profile uses the provider
`claude-subscription-directsdk-experimental`. For that provider the resolver adds `/usr/local/bin`
and `~/.local/bin` to `PATH`, because that backend really does run Claude Code on the host. If the
message appears for such a seat, install it where those directories can find it.

## Token and identity refusals

`init`, `set` and `doctor` check the accounts before they write anything. The common refusals:

- **The reader is also a seat.** `the reader 'reader-bot' is also the reviewer seat`. The reader,
  reviewer, fixer and (if set) adjudicator login must be four different accounts. The broker would
  refuse every write otherwise.
- **Two roles share a token file.** `… read the same token file — one account wearing two hats`.
  Give each account its own file.
- **A login with no token file.** For example `read_token names 'reader-bot', which has no token
  file`, or, at `init --hooks`, `--admin-token '…' has no token file — add --token …=/path/to/pat
  (hook write access)`. Map the file with `--token LOGIN=/path/to/pat`: at `init`, or later with
  `hermes review-loop set --loop name --token LOGIN=/path/to/pat`.
- **A token file that is not private.** Token files must be absolute paths to regular files you own,
  mode 600. Fix with `chmod 600 ~/.hermes/keys/*-pat`.
- **Hook write refused.** `arm`, `arm --pause` and `apply --hooks` act as the reader unless you pass
  `--admin-token LOGIN`. A refusal prints `PATCH failed (HTTP 403 …)` and a `fix:` line naming the
  scope that login's token needs (`repository_hooks: write`, `admin:repo_hook`, or classic `repo`).
  On a repo owned by a user account, only the owner can manage hooks.

`set --token` maps the file for the login named by `--read-token` or `--adjudicator-login`, or
for an extra login such as a hook admin. It refuses a seat's login: seat token files move through
the plugin settings and `apply`. Step-by-step fixes for each of these
are in [Accounts and tokens](accounts.md#common-refusals-and-their-fixes); the scopes each role
needs are in [Token scopes by role](operations.md#token-scopes-by-role).

## A gate crashed or timed out

The gateway runs each gate inside the webhook request, with a time limit (30 seconds by default).
A crash, a timeout and a normal refusal all get the same 200 reply, and GitHub does not redeliver
by itself. So the loop keeps its own record.

**What you see.** The watchdog's output (wherever cron delivers it) has a line like
`⚠️ Review loop [name] owner/name — gate failure <id>: gate_reviewer <kind> on #12 @ <head>
(<action>), <N> attempt(s): <error type>: <message> — <what happens next>`, where `<kind>` is
`crash`, `timeout` or `stopped`. `explain` lists the failure as a blocker for that PR on its
`gates:` line.

**What happens next by itself.** The watchdog re-runs the reviewer or fixer gate on the stored
payload, up to 3 times. That is safe: the gate re-reads the live PR, and the run ledger ignores a
second enqueue of the same turn. The entry resolves once the same event completes cleanly.
Adjudicator and triage gate failures are reported but never re-run: redeliver those from GitHub. A loop whose hooks are paused still gets
the alerts, but nothing is re-run until it is armed.

**Confirm and fix.**

```bash
hermes review-loop explain --loop name --pr 12
hermes review-loop doctor --loop name
```

- If the kind is `timeout`, look at `doctor`'s `gate:timeout:<profile>` lines. They show the
  gateway's limit for each profile hosting a loop route, flag a limit below 27 seconds, and name
  the file to change.
- If a payload was too large to keep (over 1 MiB), the alert says it cannot be re-run. Redeliver
  it from GitHub: *Settings → Webhooks → (the route's hook) → Recent Deliveries → Redeliver*.
- After 3 failed re-runs, read the error in the alert, fix the cause, and redeliver from GitHub.

The failures are stored in `gate-failures.json` in the loop's state directory. Full details:
[When a gate crashes or runs out of time](operations.md#when-a-gate-crashes-or-runs-out-of-time).

## Routes were overwritten or changed under you

The loop's webhook routes live in the gateway's shared `webhook_subscriptions.json`. Other
tools can edit that file too: Hermes's own CLI or dashboard, or another plugin. They do not take
the plugin's lock, so they can drop or change a loop route (#1).

**What you see.** Events stop waking a seat. `doctor` shows a `route:` line as ❌, for example
`wakes profile 'some-other-agent', but seats.fixer.profile is 'drey' — the wake would run the wrong
agent`, or the route is missing. GitHub deliveries may start failing with 404 or 401.

**What happens by itself.** Every armed watchdog sweep compares the loop's routes with the
plugin's own record of them and restores any route another writer erased or changed, with the
same secret, so GitHub's hooks keep working. The cron output then says
`🔧 Review loop [name] — restored <N> route(s) …`.

**Fix it now** instead of waiting for the sweep:

```bash
hermes review-loop doctor --loop name --repair
```

`--repair` restores this loop's routes (and gate shims) from that record, then runs the normal
checks. It is the only write `doctor` ever makes. If the route has no record to restore from (an
install older than the record), rebuild it from the loop config:

```bash
hermes review-loop apply --loop name --recreate-routes
```

That gives the route a new secret and re-keys one repo hook for it in the same step. For a route
bound to the wrong profile or at the wrong gateway origin, `doctor`'s `fix:` line usually says
`hermes review-loop apply --loop name`, which rebinds it and keeps its secret.

Always change routes through `set`, `apply` or `uninstall`. A route edited by hand elsewhere is put
back by the next sweep. More: [the shared route registry](architecture.md#the-shared-route-registry-issue-1).

## Disk usage keeps growing

A review is expensive locally: a checkout per head, a build directory, logs. When a PR is merged or
closed, the reviewer gate's closed path reclaims that PR's disk by itself. A backlog can still pile
up: PRs closed while the hooks were paused, or closed before the loop existed.

**See what would be removed** for every closed PR, then do it:

```bash
hermes review-loop cleanup --loop name --dry-run
hermes review-loop cleanup --loop name
```

For one PR, add `--pr 12`. Cleanup only removes things for a PR that a fresh GitHub read confirms
is closed. It only touches paths inside the loop's configured `roots` and its own
`artifacts/<PR>/` directory, never touches a worktree with a branch checked out, and skips evidence
directories. The report counts **file bytes removed**, which can be more than the disk you get back
on a compressed or deduplicating filesystem; check `df` if that matters.

Other things that take space, and their limits:

- the host crate cache (`deps/` in the state directory) is capped at 2 GiB by default
  (`REVIEW_LOOP_CRATE_CACHE_GIB`);
- the workers' log, `~/.hermes/state/review-loop-runs.sqlite.workers.log`, rotates once to `.1`
  past 256 KiB;
- a sandbox's working space is a size-capped tmpfs, so it uses memory, not disk, and is gone when
  the turn ends ([sandbox size caps](operations.md#sandbox-size-caps-the-two-writable-mounts)).

## After `uninstall`, things are left behind

`uninstall` removes, in this order: the loop's repo hooks, the watchdog cron job and its shim
(only when this was the last loop, since all loops share one job), the routes, the gate shims no
other loop needs, and the loop config. It reads the hook list back from GitHub before it calls the
hooks gone.

**What stays on purpose:**

- **The state directory**, unless you pass `--purge`. `uninstall` says
  `state kept: <path> (pass --purge to remove it)`. A state directory outside the default location is
  never purged; `uninstall` prints the `rm -rf` command for you to check and run.
- **Files shared by every loop**: the run ledger `~/.hermes/state/review-loop-runs.sqlite` (and its
  `.workers.log`), the pacing file `~/.hermes/state/review-loop-pacing.json`, the runtime file
  `~/.hermes/review-loop-runtime.json`, and your token files in `~/.hermes/keys/`. Remove them by
  hand once no loop is left, and revoke the tokens on GitHub.
- **The Hermes profiles** (`drey`, `vex`, `tuck`) and the plugin itself. They are yours.
- **Hooks**, if you passed `--keep-hooks`. They stay live and post to routes that no longer exist.

**When it stops halfway.** If `uninstall` cannot delete a hook (a token without hook admin access)
or remove the cron job, it changes nothing else and prints the exact `gh api -X DELETE …` or
`hermes cron remove …` commands. If a later step fails, it ends with
`uninstall INCOMPLETE — removed: …; left behind: …` and the commands that finish the job.

Hooks are deleted as the reader unless you name another login with `--admin-token`. That login
must already have a token file mapped on the loop (at `init`, or with
`hermes review-loop set --loop name --token LOGIN=/path`):

```bash
hermes review-loop uninstall --loop name
hermes review-loop uninstall --loop name --admin-token admin-login --purge
```

## An issue opened and nothing was labelled

**What you see.** Someone opened an issue, and no triage labels appeared. Issue triage is opt-in;
the step-by-step setup is in [issue triage and issue fixes](issues.md).

`explain` and `trace` can't help here: `explain` is for PRs only, and `trace` cannot replay
`issues` deliveries yet (#230). Triage sends no observer notice either (#231). Confirm with these
instead:

1. **Is triage on, and for whom?**

   ```bash
   hermes review-loop triage --loop name
   ```

   It prints `issue triage: off`, or the route, the `authors` and the `labels`.
2. **Is the wiring right?**

   ```bash
   hermes review-loop doctor --loop name
   ```

   Look at `profile:triage`, `credential:triage`, `model:triage`, `route:name-triage` and
   `hook:name-triage`.
3. **Did GitHub deliver the event?** On GitHub, open *Settings → Webhooks*, pick the hook whose URL
   ends in `/webhooks/name-triage`, and open *Recent Deliveries*. No delivery for the issue means
   the hook is paused or missing. `200 {"status": "ignored", "reason": "script"}` means the gate
   ran; it does not tell you what it decided.
4. **Did a turn run, and what did it record?** Every triage turn is a row in the host run ledger,
   keyed by the issue number. Failed, waiting and uncertain runs, with their reasons (run it from
   the plugin's directory):

   ```bash
   python -m review_loop.run_supervisor status ~/.hermes/state/review-loop-runs.sqlite
   ```

   Every recent triage run and what it wrote (this needs the `sqlite3` command; it only reads):

   ```bash
   sqlite3 -readonly ~/.hermes/state/review-loop-runs.sqlite "SELECT r.pr, r.state, r.error, t.state, t.labels, t.error FROM runs r LEFT JOIN triage_results t ON t.run_id = r.id WHERE r.seat = 'triage' ORDER BY r.created DESC LIMIT 10"
   ```

   The triage states are `posted` (written), `skipped` (a person labelled it first, or the issue
   changed), `nothing` (no label fitted), `denied` (the broker refused it, with the reason) and
   `uncertain` (sent, outcome unknown; never replayed). No row at all means the gate did not queue
   a turn.

The causes, most common first:

- **Triage is off.** Turn it on: [issue triage, step by step](issues.md#turn-on-issue-triage).
- **The issues hook is paused or missing.** `triage --enable --admin-token …` creates the hook
  paused. Arm it; if it is missing, create it first:

  ```bash
  hermes review-loop apply --loop name --hooks --admin-token reader-bot
  hermes review-loop arm --loop name --admin-token reader-bot
  ```

- **The author is not in `triage.authors`.** Issues from anyone else are dropped at the gate, on
  purpose. Add the login (the list you pass replaces the old one, so repeat every author):

  ```bash
  hermes review-loop triage --loop name --enable --author you --author teammate
  ```

- **It was not a new issue.** Only `opened` is triaged. Editing, reopening, transferring or
  labelling an issue does not start triage, and each issue is triaged at most once.
- **The issue changed before the turn ran.** The gate, the worker (just before launch) and the
  broker (just before writing) each re-read it: an issue that was closed, turned out to be a pull
  request, or changed author is left alone, and the ledger row says why.
- **It already had a triage label.** If any label from your list is on the issue, a person got
  there first, and triage writes nothing (`skipped`).
- **No runtime file, or no worker could start.** The turn could not be queued, and nothing retries
  it. Fix the runtime file ([runtime file problems](#runtime-file-problems)), then redeliver the
  issue's `opened` delivery from the hook's *Recent Deliveries* page (**Redeliver**).
- **The daily cap is spent.** The run is `waiting` with `held: triage daily turn cap (<N>) reached`.
  It starts after midnight by itself. See [held by a daily cap](#held-by-a-daily-cap).
- **The turn failed.** The ledger shows the reason, such as a model the triage profile cannot
  resolve. Fix the cause, then re-arm it. Issues and PRs share GitHub's numbering, and the ledger
  keys a triage run by the issue number, so `retry` takes the issue number as `--pr` (without
  `--seat`):

  ```bash
  hermes review-loop retry --loop name --pr 45
  ```

- **The broker refused the write (`denied`).** Usually the labelling account: its token must be
  the triage login's own (`rev-bot` by default), never the reader's, with `issues: write`.

## A fix label was applied and no PR came

**What you see.** A maintainer added the fix label (say `agent-fix`) to an issue, and no PR or
comment followed. The setup is in [issue fixes](issues.md#turn-on-issue-fixes).

**Confirm.** Start with the settings, then the ledger. The issue-fix run's seat is `issue_fixer`,
keyed by the issue number:

```bash
hermes review-loop triage --loop name
```

```bash
sqlite3 -readonly ~/.hermes/state/review-loop-runs.sqlite "SELECT r.pr, r.state, r.error, f.kind, f.state, f.branch, f.pr_number, f.error FROM runs r LEFT JOIN issue_fixes f ON f.run_id = r.id WHERE r.seat = 'issue_fixer' ORDER BY r.created DESC LIMIT 10"
```

The issue-fix states are `pushed`, `opened`, `requested` (the PR is open and the reviewer was
asked), `posted` (it commented instead of fixing), `denied` and `uncertain`. No row at all means
the gate queued nothing.

The causes:

- **Not the fix label.** `triage --loop name` prints `issue fixes: label 'agent-fix' by …`, or
  `issue fixes: off`. The name must match (case does not matter).
- **The account that added it is not a maintainer.** Only a label added by a login in
  `triage.maintainers` counts. List everyone who may hand issues to the fixer (the list you pass
  replaces the old one):

  ```bash
  hermes review-loop triage --loop name --enable --fix-label agent-fix --maintainer you --maintainer teammate
  ```

- **Unattended fixer pushes are off.** `triage --loop name` then ends that line with
  `OFF until unattended fixer pushes are on`, and `doctor` shows `⚠️ fixer-push`. Read the
  [push policy](security.md#unattended-fixer-push-policy-host-operator-not-github-owner-consent),
  then:

  ```bash
  hermes review-loop fixer-push --loop name --enable --acknowledge-pr-race
  ```

  Then remove the label and add it again: the earlier event was dropped, not queued.
- **The issue does not qualify.** It must be open, by an author in `triage.authors`, and still
  carry the label when the turn starts and when the host writes.
- **The base branch could not be read**, or **no worker could start** (no runtime file). Nothing
  was queued. Fix the cause, then remove and re-add the label.
- **The branch `review-loop/issue-N` already exists**, from an earlier attempt. The push requires
  the branch to be absent, so it is refused and nothing is overwritten. Because the push had
  started, the record says `uncertain` with `push unknown`: check the branch on GitHub (it still
  points where it was), delete it if you want a new attempt, then remove and re-add the label.
- **Re-adding the label at the same base commit starts no second turn.** One issue-fix turn runs
  per issue and base commit. Re-adding the label only re-arms that turn if it failed without
  writing anything; so does `hermes review-loop retry --loop name --pr N` (the issue number,
  without `--seat`).
- **`seat model unresolved: unknown seat 'issue_fixer'`.** A bug in versions before the fix in
  PR #254: every issue-fix turn failed before it launched. Update the plugin.

## The loop's posts carry a footer or trailer you did not expect

Everything the loop posts is signed, unless you turn it off: reviews, the fixer's answers
comments, ruling comments, triage comments, an issue fix's PR description and comment end with
`🤖 Automated by hermes-review-loop · <seat>`, and commits the fixer pushes carry an
`Automated-By: hermes-review-loop (…)` trailer. The host adds them when it sends the write, so a
seat cannot remove them. Labels are not signed.

To stop signing for one loop:

```bash
hermes review-loop set --loop name --attribution off
```

`status` shows `signed: off`, and `doctor`'s `attribution` line agrees. If the settings form also
names the setting, set it there too, or the next `apply` turns signing back on. Details:
[what the loop signs](operations.md#what-the-loop-signs).

## Still stuck?

1. Run `doctor`, fix every ❌, and decide every ⚠️.
2. Run `selftest --no-model`, then with `--pr 12`.
3. Run `explain --pr 12` and do what its `next:` line says.
4. If a webhook did nothing, `trace` that delivery and read its `outcome:`.

The design behind each of these is in [architecture](architecture.md); day-to-day operation is in
[operating a loop](operations.md).
