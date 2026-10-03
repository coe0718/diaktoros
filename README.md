# hermes-review-loop

**Two agents review each other's pull requests, unattended — and every step in between is a
script, not a model.**

One seat writes code and asks for review; a *different* seat (different model, different account)
reviews it and returns a verdict; the first answers the verdict and asks again; the loop runs until
someone approves it, or until the budget runs out and a human gets handed the decision.

The agents do the work. The loop — who runs, when they are allowed to run, how many rounds are
left, whether the thing has quietly died — is decided by deterministic Python, because that is the
part that must not be creative.

```
        ┌────────────── open / request ───────────────┐
        │                                             ▼
   ┌─────────┐   push + ask for review          ┌──────────┐
   │  fixer  │ ───────────────────────────────▶ │ reviewer │
   └─────────┘                                  └──────────┘
        ▲                                             │
        │            changes requested ◀──────────────┘
        │                    │
        └──── under the cap ─┘
                             │
                    cap spent │ → you decide (an adjudicator rules first, if configured)
```

## Before you start

You need:

- **Linux with [bubblewrap](https://github.com/containers/bubblewrap)** (`bwrap`). Every agent
  turn runs inside it, with no network and no credentials. Install it from your distribution
  (`dnf install bubblewrap`, `apt install bubblewrap`). `selftest` checks that it works.

  **On macOS:** bubblewrap needs Linux kernel features macOS doesn't have, so the loop can't run
  natively on a Mac yet. Run Hermes, and this plugin with it, inside a Linux VM: OrbStack, Lima,
  Colima and UTM all work. Everything below then happens inside the VM, which also needs a public
  HTTPS address for GitHub's webhooks. Native macOS support through a Docker or Podman sandbox is
  planned (#256).
- **Hermes Agent, installed from its Git checkout with a virtualenv.** The loop runs each turn
  with that checkout and venv, and records their paths in its private runtime file.
- **A Rust toolchain** (`rustup`, or a system `cargo`). The sandbox mounts it so a seat can build
  and test Rust code.
- **A Hermes gateway that GitHub can reach over HTTPS.** GitHub sends webhooks to it, so it needs
  a public address, for example behind a tunnel or reverse proxy.
- **A GitHub repository you administer,** and **three GitHub accounts**: the one that writes
  fixes, a *different* one that reviews them, and a reader (often your own account). Each one
  needs its own token. [Accounts and tokens, step by step](docs/accounts.md) walks through
  creating them.
- **Two Hermes profiles** for the two agents (for example `coder` writes fixes and `critic` reviews),
  each with the model it should use.

## New here? Read in this order

1. [How it works](docs/concepts.md): the loop in plain words, and what every term means.
2. [Accounts and tokens](docs/accounts.md): create the GitHub accounts and tokens.
3. [Install](#install) and [First run](#first-run-in-order) below.
4. [Command reference](docs/commands.md): every command and flag.
5. [Troubleshooting](docs/troubleshooting.md), when something doesn't happen.

## Why it exists: unattended loops fail quietly

The hard part of an agent loop is not the agents. It is that a loop which stopped being driven
looks *exactly* like a loop with nothing to do:

- GitHub clears a pending review request the moment a verdict lands — so a fixer that pushes
  without re-requesting review silently ends the loop. Nobody notices for days. (In the loop this
  was built from, the fixer leg was also dead on arrival because webhook payloads spell review
  states `changes_requested` while the REST API spells them `CHANGES_REQUESTED`. Synthetic tests
  written from the author's own assumptions passed the whole time.)
- A verdict that keeps coming back as "changes requested" burns a night and a budget with no exit.
- Two runs sharing one clone corrupt each other's worktrees and produce **wrong verdicts**, which
  is worse than a failed run.
- Every review leaves a checkout and a build directory behind. Merged PRs used to leave all of it:
  one measured at **8 GB**, and a backlog sweep reclaimed **87 GB**.

Every one of those is a *silent* failure, so this plugin makes each one loud or impossible:

| failure | what the loop does |
|---|---|
| push without a request | the gate only wakes the reviewer on an explicit request, and the fixer's turn has exactly two broker writes: one push, then one `request_review` |
| fixer never pushes | the watchdog reports "changes requested N hours ago at head X, fixer never pushed" |
| reviewer never posts | "head first observed N hours ago, 0 verdicts at this head" |
| verdict ping-pong forever | the cap is counted in **verdicts**; hitting it hands the PR to an adjudicator instead of buying round four |
| two runs, one clone | every turn gets its own sandbox and its own exact-head export of the PR, so parallel runs never share a checkout; the host run ledger enforces each seat's capacity |
| a run dies mid-way | the run ledger holds each run on a lease its worker keeps renewing; a lost worker's run is marked `uncertain` (it may have written) and never replayed, while a failure before any write retries after 2, 4 and 8 minutes (four attempts in all) |
| disk creep | a merged/closed PR runs the cleanup: worktrees, build dirs, logs, locks, counters |
| another registry writer erased or rewrote a route | the watchdog restores it from the plugin's own intent record, same secret, and says so; `doctor` flags it; `doctor --repair` restores it now |
| "did the loop ever run?" | every branch of every gate either enqueues an isolated turn or logs *why not*; the watchdog reads GitHub state directly instead of trusting anyone's summary |

## Install

```bash
hermes plugins install coe0718/hermes-review-loop
```

**The install asks you to confirm, and that's expected.** Hermes scans every plugin it installs
and rates this one **caution**: well over a hundred findings, none critical. It prints them all, then asks
`Install anyway? Only continue if you trust the source. [y/N]`. A plugin whose job is to run
agents in a sandbox and drive `git` will always trip pattern-based checks. Here is what the
findings are:

| what the scanner reports | what it actually is |
| --- | --- |
| ~150 `execution` (runs another program) | the plugin starts `bwrap` (the sandbox), `git` (snapshots and pushes), `hermes cron` (the watchdog job), its own workers and Hermes's model resolver — and most hits are in `tests/`, which start sandboxes, fake GitHub servers and throwaway repos |
| 4 high `privilege_escalation` in `selftest.py` | the *advice text* `selftest` prints when bubblewrap is not set up (`sudo sysctl …`, `sudo dnf install bubblewrap`); the plugin never runs `sudo` |
| 1 high `traversal` in `trusted_turn.py` | `git -C /proc/self/fd/N` — reading the source through a pinned file descriptor so it cannot be swapped mid-snapshot |
| 1 high `exfiltration` in a test | a test that tries to read Hermes's own secrets file **from inside the sandbox**, to prove it cannot |
| `sudo apt-get …` in `.github/` | the CI workflow installing bubblewrap on the test runner |
| a few in `docs/` | sentences that mention `git clone`, `.env` and the like |
| `http://127.0.0.1:…` (low) | the sandbox's local bridge to the model, and test fixtures |

Read the list yourself before answering `y`. CI runs the same scanner on every change (job
`plugin-guard`), and a **dangerous** verdict — the one that blocks installs outright — fails the
build. The Hermes Desktop app installs caution-rated plugins only from Hermes's reviewed plugin
catalog; until this plugin is listed there, install it from a terminal.

Then set up one loop per repository. `setup` does the whole first install, asking for each answer
(the plugin settings form's values are the defaults):

```bash
hermes review-loop setup --repo owner/name
```

It detects the runtime paths and writes `review-loop-runtime.json`, runs `init` (showing its dry run
first), schedules the watchdog, runs `doctor` and `selftest --no-model`, and arms the hooks only
after a clean pass and only if you say yes. Any ❌ stops it with its fix line. Re-running it is
safe: whatever is already in place is kept, and a runtime path that stopped working is replaced.
`--yes` takes the flags and the settings form as the answers (no questions), and `--dry-run` shows
every step and writes nothing.

`init` is the same install, flag by flag:

```bash
hermes review-loop init \
  --repo owner/name \
  --fixer dev-account \
  --reviewer rev-bot \
  --fixer-profile coder --reviewer-profile critic \
  --cap 3 \
  --reviewer-concurrency 2 --fixer-concurrency 1 \
  --clone ~/projects/name \
  --root ~/reviews --root ~/.hermes/cache/scratch \
  --read-token owner-account \
  --token owner-account=~/.hermes/keys/owner-account-pat \
  --token rev-bot=~/.hermes/keys/rev-bot-pat \
  --token dev-account=~/.hermes/keys/dev-account-pat \
  --host https://your-gateway.example \
  --hooks --admin-token owner-account \
  --schedule 15m --watchdog-deliver telegram
```

The reader (`--read-token`), the reviewer seat and the fixer seat are three different accounts, each
with its own token file (a fourth, `--adjudicator-login`, is optional) — `init` and `set` refuse a
reader that is a seat or shares a seat's file, because the broker would refuse every write. Each
seat login must be in its `--reviewer`/`--fixer` allowlist. `--hooks` and `arm` edit the repo
hooks as `--admin-token`'s login (default: the reader). On a user-owned repo only the owner can
manage hooks, and here the owner is also the reader, so its file needs hook write — fine-grained
`repository_hooks: write`, or classic `repo`; `init --hooks` prints the login and this need (in
`--dry-run` too), and `arm` exits 1 naming it if GitHub refuses. To keep the reader read-only, leave `--hooks` off and add and toggle the hooks by
hand (or, on an org repo, name a separate admin login with its own file and pass the same
`--admin-token` to `arm`). A reader can be changed later with
`hermes review-loop set --loop ID --read-token LOGIN --token LOGIN=/path/to/pat` — the same command
repairs a loop file that has no `read_token` at all.

**Upgrading a loop whose reader is a seat** (the shape this rule now refuses — `doctor` and
`status` flag it): give the reader its own account and PAT and move it with the `set` command
above. If that loop's hooks are edited without `--admin-token` (the default), `arm`,
`arm --pause` and `uninstall` now act as the new reader, so its PAT needs hook write
(`repository_hooks: write`, `admin:repo_hook` or classic `repo`) — a read-only reader PAT means
passing `--admin-token <owner login>` to those commands instead.

Each `--token` is a *path* to one account's PAT (mode 600), never the token itself. The two
**seat** accounts need a **classic** PAT: GitHub refuses a fine-grained token for an account that
is a collaborator on someone else's repository. The reader, when it is the repository owner, can
use a read-only fine-grained token. See [token files](docs/operations.md#token-files-one-pat-per-account) and
[scopes by role](docs/operations.md#token-scopes-by-role).

Replace `--host` with the public origin of **your own** Hermes gateway (no path), or explicitly
set `host` in this plugin's settings. There is no shared webhook host. `init` refuses a missing or
invalid host before writing the loop config or routes; `--hooks` never creates GitHub hooks in that
case. Use HTTPS for a public GitHub webhook (HTTP is useful for local testing).

`init` writes one loop config, the webhook routes (one per seat, plus the adjudicator's with
`--adjudicator-route` and the observer's with `--observer-profile`), two GitHub hooks and one cron
job — all visible and reversible; see [what `init` writes](docs/operations.md#what-init-writes) and the
[everyday commands](docs/operations.md#everyday-commands).

### First run, in order

`doctor` checks the installation; `selftest` then checks the isolated turn path. Run these in order
(replace `ID` and `N`; `N` should be an open, non-draft, same-repository PR targeting the loop's
base):

```bash
# 0. the private runtime file (host paths; each seat's model comes from its Hermes profile).
#    Switch one of two: with it, an eligible event runs an isolated seat turn instead of being held.
#    Skip this line if you ran `setup`: it already wrote the file.
(umask 077; touch ~/.hermes/review-loop-runtime.json); chmod 600 ~/.hermes/review-loop-runtime.json; $EDITOR ~/.hermes/review-loop-runtime.json
hermes review-loop doctor   --loop ID                       # installation preflight
hermes review-loop selftest --loop ID --no-model            # 1,2,4,6: runtime, bwrap, identities, ledger — free
hermes review-loop selftest --loop ID --pr N                # + one tiny completion per seat model + broker dry run
hermes review-loop selftest --loop ID --pr N --live-turn    # + one real isolated reviewer turn, NOT posted
python -m review_loop.run_supervisor status ~/.hermes/state/review-loop-runs.sqlite
hermes review-loop arm --loop ID                            # switch two: the hooks go live — reviewer turns now post
```

What each step proves, and how to read a failure, is in
[`doctor`](docs/operations.md#preflight-doctor) and
[`selftest`](docs/operations.md#verifying-the-isolated-setup-selftest). When a PR later stops moving,
`hermes review-loop explain --loop ID --pr N` says why
([details](docs/operations.md#why-isnt-this-pr-moving)).

Step 0 is for an install made with `init`: what the file holds, and how to write it by hand, is in
[the runtime file](docs/configuration.md#runtime-file-and-seat-models-review-loop-runtimejson).

## What else it can do

Beyond the review loop itself, each of these is one command away:

- **Issue triage and issue fixes.** New issues from authors you list get labels from a fixed list;
  a maintainer's label can hand an issue to the fixer, which opens a PR the loop then reviews.
  Off unless you turn it on. [Issues, step by step](docs/issues.md).
- **`trace`.** Replays one webhook delivery without side effects and says why it started nothing.
  [`trace`](docs/operations.md#why-did-that-delivery-start-nothing-trace).
- **Pacing.** A subscription seat that hits its usage window waits for the reset instead of
  failing, and `seats.<seat>.daily_turns` caps turns per day.
  [Pacing](docs/operations.md#pacing-usage-windows-and-daily-caps).
- **Signing.** What the loop posts carries a "🤖 Automated by hermes-review-loop" footer, and its
  commits an `Automated-By:` trailer. On by default; `set --attribution off` turns it off.
  [What the loop signs](docs/operations.md#what-the-loop-signs).
- **Graded reviews.** The reviewer grades every finding P0–P3 and says whether it *blocks* or is an
  *issue*. Only blocking findings request changes; an APPROVE may list the rest under
  "Issues to file".
- **`explain`, `retry`, `drain`.** Why a PR is not moving (read-only), re-arm a failed turn that
  never wrote, and start queued work now. [Commands](docs/commands.md#when-something-is-stuck).

## What the loop guarantees

- **One PR, one seat.** A PR is held by the reviewer *or* the fixer, never both: a review never
  runs against a PR the fixer is mid-fix on. The handoff is what frees the other seat — the fixer's
  `review_requested` ends the fixer's turn, the reviewer's verdict ends the reviewer's. Any other
  trigger that arrives while the other seat holds the PR queues instead of starting.
- **Capacity is per seat.** `reviewer 2 · fixer 1` means two reviews in flight and one fix — the two
  seats are usually different models on different budgets, and wanting two reviews rarely means wanting
  two fixes. Everything above a seat's limit queues, and starts when a slot frees.
- **Parallel only when it is safe.** Every turn gets its own sandbox, with its own temporary root
  and its own export of the PR at the exact head it was woken for, so parallel runs never share a
  checkout. The host run ledger enforces each seat's capacity: a turn over the limit waits.
- **The cap is a wall, not a suggestion.** `cap` verdicts, `cap - 1` fix turns. The verdict that
  reaches the cap escalates instead of buying another round. The human is the veto, not the
  reviewer: when the loop has an adjudicator route, the adjudicator rules and reports, and never
  merges or pushes; without one the PR waits for you.
- **One wake per head.** Every marker is keyed by PR *and* commit: a new commit is a new situation,
  the same commit is not. Redelivered webhooks do nothing.
- **Unknown is not a guess.** If the review list cannot be read, the gate stays silent rather than
  assuming round 1 — a skipped round beats a miscounted one.
- **The watchdog is read-only until it has a reason.** Four stall shapes, read from GitHub state;
  each armed sweep drains eligible queued runs when a seat is free, without waiting for a stall alert.
  A queued head that no longer matches the PR is dropped, never silently retargeted.
- **Asking why changes nothing.** `explain` reads GitHub and the loop's own files, reaches its
  conclusion through the *same* predicates the gates run, and writes nothing at all — no queue
  entry, no claim, no drain, no webhook POST, no token. Run it twice and the loop is byte-for-byte
  as it was.
- **Paused means silent; blind does not.** With the repo hooks off, the watchdog says nothing and
  drains nothing: a parked loop must never spend a run. When it cannot read GitHub at all (a dead
  or revoked token, a 5xx, no network) it still drains nothing, but says so — "cannot read GitHub as
  <login>: HTTP 401 — token expired or revoked?" — every cooldown until reads work, and it warns a
  week before the read token's `github-authentication-token-expiration` date.
- **A seat is who the config says it is — or the loop refuses to run.** The profile it runs as, the
  login it acts as and the route that wakes it are validated together before a config, a route or a
  hook is written, and `status` prints the installed route next to the configured seat so a
  half-applied identity change is visible instead of silent.
- **The observer is not a seat.** An opt-in feed of short notices, emitted from the transitions the
  loop already made, delivered by a `deliver_only` route with no agent behind it. A refused
  delivery costs a retry — never a queue entry, a lock, or a turn; and with the feed off the loop
  is byte-for-byte the loop without one.

## Safety model: what runs, and what can write

The short version: **no agent ever holds a GitHub credential, and nothing runs until you turn it
on.** The long version:

> **What runs, and when (issue #16).** No gateway agent ever handles a PR event: every gate
> answers `[SILENT]`, so Hermes never falls through to its normal credential-owning agent.
> Seats run only as **isolated turns** — credentialless, in a bubblewrap sandbox, writing
> through the host broker — and nothing runs until *both* switches in
> [First run](#first-run-in-order) are on: the private runtime file
> (`~/.hermes/review-loop-runtime.json`) that the worker needs, and `arm`, which turns on the
> repo hooks `init --hooks` created paused. Without the file an eligible event is queued with
> its reason and held; with it and the hooks armed, **a reviewer turn posts a real GitHub
> review** as the reviewer login, and, on a loop with an adjudicator route, a spent cap runs
> the adjudicator. The fixer is the exception: unattended fixer pushes stay **off** (below)
> until you opt in per loop. Each run's checkout is a scratch copy, not a security boundary;
> the sandbox and broker are. The whole boundary is in [Security](docs/security.md).
>
> **Adjudication is isolated like the seats, and optional.** On a loop with an
> `adjudicator.route`, a spent cap enqueues an isolated
> adjudicator turn in the host run ledger (never the legacy gateway route, which
> stays silent). It runs credentialless in the same sandbox, with a read-only
> checkout, and can only submit one ruling (ACCEPT / REJECT / RESPEC + reason)
> through the broker. The host records it, tells the operator (observer `ruling`
> notice plus the watchdog outbox), and posts it as a PR comment only when a
> distinct `seats.adjudicator.login` identity is configured. It never merges,
> pushes or reviews. Without a route the cap only writes the breach marker and the PR waits
> for you. `init --adjudicator-route <id>-breach` turns it on; `setup` never does, and no `set`
> flag adds it later. To add it to an existing loop, put
> `"adjudicator": {"route": "<id>-breach", "profile": "<profile>"}` in the loop file, then run
> `hermes review-loop apply --loop <id> --recreate-routes`. See
> [adjudication](docs/configuration.md#adjudication-the-isolated-ruling).
>
> **Operator decision: unattended fixer pushes are off by default.** A changes-requested
> verdict is held for you and no fixer turn starts until
> `hermes review-loop fixer-push --loop ID --enable --acknowledge-pr-race`. The gate rejects
> verdicts for PRs whose webhook author is not in `fixers`, and the credentialed broker reads
> the live PR author before every fixer write and rejects missing/outsider authors. These
> checks do not close the time-of-check race: a PR can close, become draft, change
> author/target, or close and reopen after the final API read and before Git receives a ref
> update. The lease only compares `refs/heads/<branch>` to the old SHA, not GitHub PR
> metadata, and a post-push readback can flag some transitions but cannot undo a published
> commit. Do not call that an atomic PR policy; read
> [the push policy](docs/security.md#unattended-fixer-push-policy-host-operator-not-github-owner-consent) before enabling it. With pushes off, make an
> individual fix by hand: inspect the live PR owner, state, draft flag, base/head repo, review
> and intended diff, push, verify the exact PR/ref afterwards, and reconcile an ambiguous
> outcome instead of retrying it.

## Advanced: Claude subscription seats (DirectSDK)

A seat whose Hermes profile selects `claude-subscription-directsdk-experimental`
uses the plugin's DirectSDK client **on the trusted host**, not inside bubblewrap.
The seat resolves with `auth=external_process`, the fixed upstream
`process://claude-subscription-directsdk-experimental`, and the public placeholder
`host-process`. That placeholder is not a subscription credential. The sandbox
still uses the ordinary chat-completions capability on its per-run Unix socket;
`InferenceCapability` selects the host backend from the credential provider's
`backend='directsdk'` marker. An arbitrary `process://` URL is not an HTTP upstream
and does not enable this backend by itself.

The host needs the experimental plugin and its native Claude prerequisite set up
for the selected profile. Keep the plugin, native executable, profile state and
subscription login on the host: do not copy them into the turn's snapshot, mount
them into the sandbox, or put account secrets in the runtime model override. A
missing plugin, invalid profile, unsupported configuration or native-client failure
must hold/fail the turn rather than silently switch to an API-key provider. This
is an experimental local integration, not a promise of provider policy approval
or a substitute for testing the installation's subscription prerequisites.

The process backend does not widen the sandbox's authority. Model selection,
request/response bounds, output-token limits and per-run call quota remain host
policy. Sandbox request fields must never become native executable arguments,
working-directory choices, environment overrides, plugin paths or client-constructor
options. Tools remain conversation data for Hermes; DirectSDK is not permission to
run native Claude tools against the checkout. Host error replies must not expose
subscription tokens, native stderr, local paths or inherited environment values.
Streaming uses OpenAI-style SSE; closing a stream or capability must release the
host-side request/client rather than leave a native inference process behind.
Reviewer, fixer and adjudicator capabilities remain profile-bound and independently
owned, even though inference runs on the same host. Native Claude authentication uses
the OS user's existing native login by default. Different native accounts require
an explicit `CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR` (or `CLAUDE_CONFIG_DIR`) in
each seat profile's host-side environment; naming different Hermes profiles alone
does not create separate native subscriptions. Host-selected executable overrides
use `CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND`; sandbox requests cannot set these.

A small **inert wire-profile plugin**, `review-loop-directsdk-wire`, is staged in
the disposable sandbox home. It only declares the loopback HTTP endpoint and
DirectSDK's native reasoning carrier type. It has no SDK, native executable,
host paths or auth. This uses Hermes's provider-profile contract to preserve signed
native history across sandbox tool iterations instead of treating the carrier as
an unrelated custom-provider sidecar. Reasoning controls, tool calls/results,
history and usage retain the installed provider's native projection.

The existing bounds remain: 1,000,000-byte requests, 4,000,000-byte replies,
4,096 output tokens and at most 32 reserved calls per turn. Nested output-token
overrides and process-configuration kwargs are refused. A host helper request is
limited to 120 seconds; disconnect, stream close and capability shutdown cancel
its installed DirectSDK client and owned native process tree.

The backend API is `DirectSDKBackend(profile, settings)`,
`post(body, headers) -> (status, content_type, iterator_of_bytes)`, and `close()`.
The public model wire contract is still `chat_completions`, not Anthropic Messages
or a native CLI RPC exposed to the seat. Offline adversarial coverage lives in
`tests/test_directsdk_backend.py`; it uses fake clients/processes and must not spend
subscription quota. Offline success does **not** prove a live model request or
native credential availability. `selftest --no-model` is the no-spend preflight;
model-spending selftests remain an explicit operator decision.

**Launch lockdown.** The host helper installs review-loop's own `subprocess.Popen` guard
(`review_loop/directsdk_guard.py`) before it imports the provider, so every native launch must
use the host-resolved Claude executable and the reviewed one-turn flag grammar (no tools, no
setting sources, strict MCP config naming only the provider's pinned inventory server, `dontAsk`,
one turn, no slash commands, no session persistence); anything else is refused before a process
starts and surfaces as a failed inference (HTTP 502), which `selftest` names. This guards against
the provider's launch contract drifting, not against deliberately malicious plugin code: installed
plugins remain trusted host code.

**When you change this code, keep these rules:**

- Preserve native reasoning with the inert registered provider profile that declares the exact
  carrier type. A generic custom HTTP route strips another provider's `.native_assistant` sidecar.
- Keep DirectSDK client construction in the host helper. Never route sandbox JSON into the
  command, args, environment, working directory, plugin paths, timeout or any other constructor
  argument.
- Native login defaults to the OS user's account. Distinct Hermes profiles need an explicit native
  config directory to use different subscriptions; never copy auth stores.
- Keep arbitrary `process://` URLs refused by the HTTP endpoint check. Only the exact named process
  backend marker selects DirectSDK, under the existing request, model, token and call limits.
- Cancel through the client's `close()`: native Claude runs in its own process group, so killing
  only the helper's group does not stop it.

## Status

**What the tests cover.** Two offline suites run in CI on Python 3.11 and 3.14:

- `tests/run_tests.py`, the harness: every gate branch (each answers `[SILENT]` and enqueues an
  isolated turn or holds it with its reason), the cap and the breach marker, one PR one seat,
  per-seat capacity and queueing, the watchdog's stall shapes and drains, `explain`'s golden cases
  and its read-only proof, `set` / `apply` / `settings` and the manifest's `config_schema`, seat
  identity and the four-identity rule, `doctor`, route self-heal, cleanup against real git, the
  observer feed, and that every command in these docs still parses. It runs once standalone
  (no Hermes importable) and once with a pinned Hermes installed.
- The boundary suite, run through `tests/leakguard.py` (which also fails a test that leaks a file,
  socket or child process): the broker, safe push, the exact-head fetch, the inference proxy, the
  run supervisor and the bubblewrap sandbox itself, with disposable credentials and fake GitHub
  and model servers. A separate CI job runs a real pinned Hermes inside bubblewrap.

CI also runs Hermes's own plugin scanner (`plugin-guard`) on every change and fails on a
**dangerous** verdict.

**Live use.** On 2026-10-03 the loop ran end to end on this public repository: a fixer PR was
reviewed about 80 seconds after it opened, changes were requested, the fixer pushed a fix and
re-requested review, and the reviewer approved. The reviews were signed, and the observer feed
posted its notices to Telegram.

**Known limits.** Worth knowing before you trust it:

- **Unattended fixer pushes are not atomic with PR metadata.** The broker checks the live PR before
  it pushes, but a PR can close, change author or retarget between that read and the ref update.
  That is why fixer pushes are off by default; see
  [the push policy](docs/security.md#unattended-fixer-push-policy-host-operator-not-github-owner-consent).
- **The sandbox is a user namespace under the same UID.** bubblewrap hides credentials, the network
  and the host's files from a turn, but the turn's processes still run as your user. A kernel or
  bubblewrap escape would act as you.
- **One gateway host per loop.** Each loop's routes live on the gateway named by its `host`. A fleet
  of gateways is untested.
- **The desktop settings form's rendering is untested.** The suite calls the plugin with the
  settings dict the form writes, so defaults, validation, preview and staged apply are covered;
  whether the desktop draws the fields the way `plugin.yaml` asks is not something the tests can see.
- **Issue triage and issue fixes have not run live yet.** They are covered by the offline suites only.
- A run that spans an identity change keeps the profile and login it started with. `--while-busy`
  says so but cannot change a run already in flight.
- Cleanup reports **file bytes removed** (`du`), which is not the same as disk recovered on a
  compressed or reflink-sharing volume: check `df` too.
- `explain`'s armed/paused line needs the read token to see the repo's hooks; where it cannot, the
  line says unknown instead of claiming the loop is parked.

## Running the tests

```bash
python3 tests/run_tests.py                                   # the offline harness
python3 tests/leakguard.py discover -s tests -p 'test_*.py'  # the boundary suite (bubblewrap; skips without it)
```

The plugin is stdlib-only and so are its tests: there is nothing to install. The harness runs in two
modes, and a few checks only decide anything in one of them — **standalone** (no `hermes_cli` on the
interpreter, which is what a stdlib-only CI image gives you) and **installed** (Hermes importable,
which is every real machine). `doctor` resolves a seat's model by running Hermes *as that profile*
and validates stored cron expressions, so a fixture that builds a "complete installation" has to
build it in both, and CI runs the suite once per mode.

## Documentation

| page | what it covers |
|---|---|
| [docs/concepts.md](docs/concepts.md) | how it works: a glossary, one PR from start to finish, the switches, what the agents can and cannot do, where things live |
| [docs/accounts.md](docs/accounts.md) | why several GitHub accounts, creating them and their tokens, storing tokens safely, checking them |
| [docs/commands.md](docs/commands.md) | every `hermes review-loop` command, what it changes, and its flags (generated from the CLI) |
| [docs/troubleshooting.md](docs/troubleshooting.md) | symptom → cause → fix: nothing happened, held turns, failed or uncertain runs, the runtime file, notices |
| [docs/issues.md](docs/issues.md) | issue triage and issue fixes, step by step: labelling new issues, handing an issue to the fixer |
| [docs/operations.md](docs/operations.md) | what `init` writes, everyday commands, the `doctor` preflight, `selftest`, `explain`, `trace`, pacing, issue triage, what the loop signs, burst handling |
| [docs/settings.md](docs/settings.md) | the desktop settings form, seat identity defaults, `settings` / `apply` |
| [docs/observer.md](docs/observer.md) | the observer feed: notices to your phone, how to turn it on, its rules |
| [docs/configuration.md](docs/configuration.md) | reference for every loop-config key, the observer block, adjudication, issue triage, plugin settings, seat identity, state files, environment overrides |
| [docs/architecture.md](docs/architecture.md) | design: the seats, isolation, escalation, the watchdog, `explain`, the observer feed, preflight, the shared route registry, stacked PRs and retargets |
| [docs/security.md](docs/security.md) | the security boundary: what runs where, the sandbox and the broker, dependency prefetch, `selftest`'s live verification, the unattended fixer push policy |
| [docs/README.md](docs/README.md) | the same index, inside `docs/` |

## Repository layout

```
plugin.yaml                manifest (no hidden capabilities: no hooks, no tools, no middleware)
__init__.py                registers the CLI and the skill
review_loop/               the library: config, state, gh, routes (+ route_intent self-heal), prompts,
                           gate runtime, observer, CLI, the read-only `doctor` preflight, `selftest`,
                           `explain` and `trace`
  run_supervisor.py        the host run ledger (SQLite): capacity, leases, retries, the detached workers
  trusted_turn.py          builds one isolated turn: temp root, prompt, tools, sandbox launch
  contained.py             the bubblewrap launcher and its size-capped mounts
  trusted_fetch.py         exports the PR at its exact head from a GitHub tarball, no `.git`, no token
  deps.py                  host-side dependency prefetch (crates.io) into a bounded cache
  broker.py, broker_ipc.py the host broker: the one-run Unix socket and the only GitHub writes
  safe_push.py             the fixer's push: a whole-file manifest, compare-and-swap on the branch
  inference_proxy.py       the per-turn model bridge; the credential stays on the host
  seat_model.py            resolves each seat's model from its Hermes profile
  pacing.py                usage-window holds and daily turn caps
  attribution.py           the "Automated by hermes-review-loop" footer and commit trailer
  runtime_detect.py        finds the host paths `setup` writes into the runtime file
scripts/gate_reviewer.py   the reviewer route's gate: a pull_request event → an isolated reviewer turn
scripts/gate_fixer.py      the fixer route's gate: a pull_request_review event → an isolated fixer turn
scripts/gate_triage.py     the triage route's gate: an issues event → an isolated triage or issue-fix turn
scripts/gate_adjudicator.py the legacy breach route: always silent (adjudication is enqueued by the gate)
scripts/broker_client.py   the credentialless client a turn uses to ask the broker for its one write
scripts/watchdog.py        cron: route self-heal, stall detection, stuck state, queue draining
scripts/cleanup.py         merge/close: reclaim the PR's local disk
scripts/observe.py         the observer route's adapter: republish the loop's notice, wake nobody
                           (the gateway runs a route's script only from the serving profile's
                           ~/.hermes[/profiles/<name>]/scripts, so init/apply put a shim there)
skill/SKILL.md             reference text: the protocol the host writes into each seat's prompt
                           (isolated turns run with plugins off and never load it)
tests/run_tests.py         the offline harness (stubbed GitHub, real HTTP sink, real git)
tests/leakguard.py         runs the boundary suite and fails any test that leaks a file, socket or child
docs/                      concepts, accounts, commands, troubleshooting, issues, operations, settings,
                           observer, configuration, architecture, security (see docs/README.md)
catalog/                   the catalog entry this repo is intended to be listed by
```

## License

MIT. See `LICENSE`.