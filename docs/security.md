# Security model: what runs, and what can write

This page describes the trust boundary of a running loop: which process holds which secret,
what an agent turn can see, and every way something can reach GitHub. It is a reference for
operators and reviewers. For the vocabulary (seat, gate, turn, broker, ledger) see
[concepts](concepts.md); for how the pieces fit together see [architecture](architecture.md).

The short version:

- **Gates decide; they never run an agent.** Every gate script (reviewer, fixer, triage) reads
  the webhook, checks it against GitHub, and answers `[SILENT]`. An eligible event becomes a row
  in the host run ledger. No gate returns a payload to the gateway, so the gateway never starts a
  credential-owning agent for this plugin.
- **Every agent turn runs isolated.** A detached worker runs each turn inside bubblewrap
  (`bwrap`): no network, no GitHub token, no model key, no view of your home folder.
- **The host broker makes the only GitHub writes.** The sandbox reaches GitHub through one Unix
  socket, scoped to one run, which allows a fixed set of operations for that seat and nothing else.

## What the agents can and cannot do

The table in [concepts: what the agents can and cannot do](concepts.md#what-the-agents-can-and-cannot-do)
is the summary. In short:

| an agent in a turn | |
|---|---|
| holds a GitHub token or a model key | no. Tokens stay with the broker; the model is reached through a host proxy that adds the key |
| reaches the network | no. Only two sockets: the inference proxy and the run's broker |
| reads your files | no. Only a copy of the code at the exact head, the PR's diff, a throwaway home and the read-only toolchain |
| writes to GitHub | only through the broker, and only its seat's operations (table below) |
| merges | no seat has a merge operation |

Each seat's writes, as the broker allows them (`review_loop/broker.py`, `review_loop/broker_ipc.py`):

| seat | turn starts when | what the broker lets it write |
|---|---|---|
| reviewer | a PR is opened, made ready, reopened, or its review is requested by the fixer | one review: APPROVE or REQUEST_CHANGES with a body |
| fixer | a changes-requested verdict, and the loop has opted in to unattended pushes | one push of whole files, then one review request; with it, one answers comment |
| adjudicator | the round cap is spent, **and** the loop was set up with `--adjudicator-route` | one ruling (ACCEPT, REJECT or RESPEC with a reason); posted as a PR comment only by an optional, distinct adjudicator account |
| triage | an allowlisted author opens an issue, and triage is enabled | labels from the loop's list (at most `max_labels`), and a comment only where the loop allows one |
| issue fixer | a maintainer applies the fix label, and issue fixes plus unattended pushes are on | either one new branch, its PR and a review request, or one comment on the issue |

Without `--adjudicator-route` a spent cap writes only a breach marker; no adjudicator turn runs.
The triage gate also answers `[SILENT]`: it drops any issue whose author is not in
`triage.authors` before a model sees its text. See [issues](issues.md) for the triage and
issue-fix flows.

Every write is recorded before it is sent. A write whose outcome is unknown (a timeout, a lost
connection) is recorded as `uncertain` and is never retried automatically.

## Processes and secrets

| process | holds | runs |
|---|---|---|
| gate script (in the gateway) | the read token, through the loop config | reads GitHub, writes a ledger row or a hold, answers `[SILENT]` |
| supervisor worker (detached) | the read token; it starts the proxy and broker | claims a ledger row, re-reads the PR, stages the turn, launches the sandbox |
| inference proxy (in the worker) | one seat's model credential, for one turn | forwards the sandbox's model requests upstream |
| broker (in the worker) | the seat's GitHub token, used only for that seat's writes | answers the sandbox's requests on one Unix socket |
| bubblewrap sandbox | nothing secret | the Hermes agent for one turn |
| watchdog (cron) | the read token | sweeps, heals routes, drains queues, emits notices |

The sandbox is started with `--unshare-all` (its own network, PID, IPC and user namespaces),
`--die-with-parent` and `--new-session` (`review_loop/contained.py`). It sees:

- read-only `/usr`, `/bin`, `/lib` (and `/lib64` when present);
- a staged `/etc` (#240): a host-written directory holding one `passwd` and one `group` entry
  (user `agent`, the sandbox's own uid and gid, home `/home/agent`) and an empty mount point onto
  which the host's `/etc/alternatives` symlink farm is bound read-only, so compilers resolve on
  Debian and Ubuntu. Nothing else from the host's `/etc` is visible. The directory is written
  fresh for each launch and checked before use: anything other than those three entries is
  refused;
- a source snapshot of Hermes from one pinned commit (hash-checked regular files, not your
  working tree), the venv, the runtime and the Rust toolchain, all read-only;
- `/work`: the exact-head export. For the reviewer and fixer it is a size-capped tmpfs filled
  from a read-only bind of the export; for the adjudicator it is the export itself, read-only;
  for triage it is an empty directory;
- `/opt/review/pr.diff` (read-only, outside `/work`), a size-capped `/tmp`, a throwaway
  `/home/agent`, the query file, and the two socket directories.

The root and `/dev` are remounted read-only at the end.

It does **not** receive the model key or OAuth token, any profile's `.env`, `auth.json`,
`auth.lock`, `config.yaml` or `.anthropic_oauth.json`, your Claude Code, Codex or Qwen credential
files, any GitHub token, your home folder, or the host network. The seat's `config.yaml` inside
the sandbox names only the model, the local bridge, the matching `api_mode` and a dummy key (for a
Claude subscription, a dummy OAuth-shaped `ANTHROPIC_TOKEN`, so Hermes applies the Claude Code
request identity). One turn's proxy holds only its own seat's credential.

## Seat models and model credentials

Each seat's model, provider and key come from **that seat's Hermes profile**. The host resolves
them per turn with Hermes's own resolution, in a separate process with a from-scratch
environment (#32). An explicit `seats.<seat>` override in the runtime file is also accepted, and
the legacy top-level `model`/`upstream`/`key_file` only as a fallback for an unresolvable
profile. Precedence and supported providers:
[configuration](configuration.md#runtime-file-and-seat-models-review-loop-runtimejson).

The runtime file (`$HERMES_HOME/review-loop-runtime.json`) must be mode 0600 and name the host
paths `source`, `venv`, `runtime` and `rust`. Without a valid one, an eligible event is held with
the reason; nothing falls back to the gateway.

The upstream must be HTTPS and use one of the proxy's per-`api_mode` contracts:
`chat_completions`, `codex_responses` or `anthropic_messages`. Each contract fixes the upstream
path and the sandbox path, allowlists request headers (the sandbox's `Authorization` and
`x-api-key` are always dropped), forces the request's model to the seat's, enforces the output
cap in that mode's own field, and applies a quota.

OAuth and subscription providers that resolve to one of those modes (`openai-codex`,
`xai-oauth`, `qwen-oauth`, `nous`, `minimax-oauth`, a Claude subscription on `anthropic`) are
accepted. Only the short-lived access token reaches the proxy. The host re-runs the isolated
Hermes resolution near its expiry, or once after an upstream 401, serialized per profile through
Hermes's own `auth.lock`, so the refresh token never leaves the host's Hermes auth store.

Copilot, Bedrock, Vertex, Azure Foundry, MoA, the `codex_app_server` runtime and any other
`api_mode` are refused before their credentials are touched. A seat that cannot be resolved is
held before launch with the reason in the ledger. It never runs with another seat's model or key.

## What a turn sees of the change (partial-view rule)

Right before launch the worker reads, with the read token, the PR's title, description, base
ref and SHA, and every page of `pulls/N/files` (path, status, additions and deletions, patch).
The prompt appends them as a labelled, bounded data section: single-line facts escaped, the
description and each clipped patch fenced, the file list and patches capped. The whole diff,
bounded to 1 MiB, is mounted read-only at `/opt/review/pr.diff`, outside the `/work` a fixer
publishes from. An unreadable PR fails the turn before launch.

An unreadable file listing depends on why (#110):

- a transient failure (5xx, 429, a rate-limit 403, a timeout, a lost connection) is a pre-write
  failure, retried with backoff (#53);
- an answer GitHub will keep giving (404, 410, another 403, a malformed or oversized listing), or
  the run's last allowed attempt, does not kill the turn. The turn runs with a change section that
  says the host could not read the file list and why, gives no files and no patches, and tells the
  seat it cannot see the whole change and must not approve it. The same words are used past
  GitHub's 3,000-file cap.

The broker enforces the "do not approve" (#93, #110). The worker records the incomplete view, with
the host's reason, in the run ledger (`runs.partial_view`) and in the run's host-built scope
before the seat starts. The broker refuses an APPROVE from that run before its one write is
spent, before any GitHub request, and again inside the receipt claim. The seat is told that an
approval is refused and to submit REQUEST_CHANGES explaining what was unavailable; that verdict
goes through in the same turn. Nothing in the sandbox can set or clear the record: requests carry
only operation, verdict and body. Such a head can only collect REQUEST_CHANGES, which the cap
bounds. `explain` shows it on a `view:` line, so an operator reviews that head by hand or the PR
is split.

A fixer turn with an incomplete view cannot push either. The broker refuses its push the same
way, before any Git call, and tells it to publish its answers instead
(`request_review --answers-file`, no push). That answers comment is the turn's one write.

## The run ledger and the notice outbox

The SQLite run ledger (`$HERMES_HOME/state/review-loop-runs.sqlite`) is the source of truth for
isolated turns (`review_loop/run_supervisor.py`):

- it commits a turn's identity before launch and deduplicates on repo, PR, head, seat and turn
  key, so a redelivered webhook never starts a second turn;
- it enforces each seat's concurrency and the one-PR-one-seat rule at claim time;
- every sandbox write goes through the run's broker, which commits a write-ahead record (a review
  receipt claim, a push intent, a ruling, an answers, triage or issue-fix row) **before** the
  external call;
- a run that failed with no write-ahead record waits with backoff and is relaunched a bounded
  number of times; a redelivered event or `hermes review-loop retry` re-arms a failed one
  ([details](operations.md#when-an-isolated-run-fails));
- a run with any write-ahead record, or one quarantined as `uncertain`, is never relaunched;
- a live worker heartbeats its lease; a worker whose lease expired is quarantined, never
  relaunched.

Failed and uncertain runs enter a durable operator notice outbox. Each watchdog sweep emits up to
20 notices through its cron output. Notices carry the PR URL, seat, full head and stable run ID.

To inspect or release runs by hand:

- `python -m review_loop.run_supervisor status DB` lists failed, waiting and uncertain rows with
  their reason, output tail and notice state;
- `python -m review_loop.run_supervisor sweep DB` recovers expired leases and emits notices
  without launching workers;
- `python -m review_loop.run_supervisor reconcile DB RUN_ID --reason 'external writes inspected'
  --acknowledge-no-live-worker` releases an uncertain run. **First** make sure no worker or
  descendant process remains, and inspect the PR for writes that landed. A present or
  inaccessible PID blocks the release; a missing PID alone is not proof. Reconciliation marks the
  run failed, is idempotent, and never replays the original write.

Cron output is a local handoff, not a confirmed receipt: a scheduler failure after the ledger
marks a notice delivered can lose it, and a crash between output and commit can duplicate it.
Use the stable run ID to deduplicate, and check `status` regularly; a `delivered` flag is not
proof that you saw it.

## Dependency prefetch (issue #51): the host fetches, the sandbox builds offline

The sandbox has no network and a scratch `CARGO_HOME`, so on its own it cannot resolve one crate.
Before launch the worker (`review_loop/deps.py`, called by `trusted_turn.run_turn`) prefetches
what the staged head's root `Cargo.lock` pins into a private per-repository host cache,
`<state_dir>/deps/cargo` (0700). The sandbox gets only that cache's `registry/` directory,
**read-only**, at `/tmp/cargo/registry`, inside an otherwise writable scratch
`CARGO_HOME=/tmp/cargo`, and `CARGO_NET_OFFLINE=true` is always set. The mount point is fixed by
`contained.DEPENDENCY_MOUNTS`, not chosen by the caller.

The seat's query opens with a host-written "Build environment" note, placed ahead of the prompt's
PR records so that PR-shaped data cannot imitate it. If the prefetch is refused, fails or times
out, the turn still runs, and the note says plainly that dependencies are unavailable: the
reviewer judges by reading and does not treat "could not build" as a finding, the fixer says its
fix is unbuilt, and the adjudicator discounts arguments that rest only on the missing build.
`selftest --pr N` reports `build:rust:fetch` (the host prefetch) and `build:rust` (offline
`cargo metadata --locked` inside the real sandbox layout).

**Trust decision.** A PR's manifests are attacker-controlled, and the prefetch runs on the host
with network, so the host never runs cargo on them:

- `Cargo.toml`, `build.rs`, `.cargo/config.toml` (which can name a `rustc-wrapper`, a credential
  provider, source replacement or `git-fetch-with-cli`), `rust-toolchain.toml`, `[patch]` and path
  dependencies never reach the host cargo process. The host reads `Cargo.lock` as data with
  `tomllib` (at most 4 MiB and 4000 packages) and accepts only packages whose `source` is
  crates.io (registry or sparse). Names and versions must match strict patterns. A git source or
  any other registry refuses the prefetch, because fetching it would mean the host contacting a
  URL the PR chose. The host then writes its **own** synthetic manifest that pins exactly those
  `name = "=version"` pairs, seeded with the PR's lockfile as resolver data so that yanked
  versions still resolve, and runs `cargo fetch` on it.
- `cargo fetch` downloads and unpacks `.crate` files. It compiles nothing and runs no build script
  or proc macro (checked with cargo 1.98: `cargo fetch --locked` on a crate whose `build.rs`
  writes a marker left no marker and no `target/`). Crate checksums come from the crates.io index
  over HTTPS.
- The fetch runs from a private empty directory under the system temp directory. The only cargo
  config in scope is the cache's own, which nothing writes. Its environment is built from scratch:
  `PATH=/usr/bin:/bin`, a throwaway `HOME`, no token, proxy or credential variables, and git
  config pinned to nothing. It uses the configured toolchain's own `bin/cargo` and `bin/rustc`,
  never a rustup proxy that would honour a toolchain override. It has a 300 s timeout (process
  group killed), and captured output is bounded to 64 KiB.
- **Bytes, not just packages.** A PR chooses the lockfile, so it chooses how much the host
  downloads. The whole cache is capped at 2 GiB, overridable with `REVIEW_LOOP_CRATE_CACHE_GIB`
  (whole GiB, 1-1024) in the environment of the process that runs the supervisor; the worker
  receives it by name (`run_supervisor.HOST_LIMIT_ENV`), and a value that cannot be parsed keeps
  the default and is named by `selftest`'s `build:rust:fetch`. The cap is enforced **during** the
  fetch: the cache's disk usage is measured every 0.2 s while cargo runs, and the fetch's process
  group is killed the moment it passes the cap. Everything that fetch added is then removed. The
  seat is told dependencies are not available because the host cache would exceed the cap, and
  that this is the sandbox, not the PR.
- **A cache that grew across PRs is not one PR's fault.** When the cap is hit and the cache holds
  crates earlier PRs left behind, that generation is retired (renamed, so a running turn that
  mounted it keeps reading it) and the fetch is retried once into an empty cache. If the lockfile
  alone does not fit, the old cache is put back and the turn is told why. A turn holds a shared
  lock on the generation it mounts until its sandbox exits, and a retired generation is deleted
  only once no turn holds it. At most one retired generation exists, so disk stays under twice the
  cap. Prefetches for one repository are serialized by a lock file, within the same 300 s bound.
- **Visible after the fact.** The worker records the prefetch in the run ledger (`runs.deps`):
  `fetching — started …` while it runs, then each ecosystem's outcome, such as
  `rust: ready — 224 crates.io crates from Cargo.lock (2.4s)` or `rust: unavailable — <reason>`.
  It is one host-written line of at most 600 characters, and never includes tool output. `status`
  lists the newest turns' lines and `explain --pr N` that PR's.
- **Git dependencies stay unavailable, on purpose.** A `git+…` source, or a package from any
  registry other than crates.io, is a URL the PR chose; fetching it from the host would let a PR
  point the host's network at a private repository or the local network. Such a lockfile is
  reported as *unavailable* (the reason counts the refused sources and never quotes a PR-chosen
  name), and the seat's note says this is a host boundary, not a defect of the PR. A planned
  opt-in for public GitHub sources is [#114](https://github.com/coe0718/hermes-review-loop/issues/114).
- **Prefetch time never comes out of the turn's budget.** It runs before the sandbox starts,
  bounded at 300 s, and the sandbox's own clock starts after it. The worker's heartbeat keeps the
  run's lease alive meanwhile.
- Other ecosystems plug in through `deps.ECOSYSTEMS` and `contained.DEPENDENCY_MOUNTS`; only Rust
  is implemented.

## Unattended fixer push policy (host operator; not GitHub owner consent)

An installed config defaults `unattended_fixer_push` to `false`, including legacy files. Neither
`init`, plugin settings, `apply`, route events nor sandbox requests opt a repository in. Only the
explicit command changes it, after warning about the accepted non-atomic PR-metadata/ref race
(see [Known limits](#known-limits)):

```bash
hermes review-loop fixer-push --loop name --enable --acknowledge-pr-race
hermes review-loop fixer-push --loop name --disable
```

The caller of that command controls the local host config. This is **host-operator opt-in**, not
proof that the GitHub repository owner or an admin authorized it; a personal-account collaborator
cannot be assumed to have admin permission.

At push time the broker reloads the repository config; an absent, invalid, duplicate or disabled
permission denies before Git. The loop ID and exact config filename must match the launch
snapshot; symlinked config files are refused. The CLI serializes policy changes with the broker's
final reload and the whole ref operation, so a successful `--disable` returns only after any
already-authorized push finishes. Enabling checks the run ledger and gate state for fixer runs in
flight. Fixer enqueue takes the same policy lock and records `push_admitted` in the run ledger: a
run admitted while pushes were off cannot borrow a later enable, and a later disable still revokes
permission at the broker. Direct edits of the config file do not take this lock and are not
covered; disable with the CLI and inspect active runs.

The per-run socket binds repo, PR, head, branch and fixer seat, and spends its one push attempt
before the write. PR eligibility and ref identity are rechecked around an exact-SHA lease. Before
the push the broker commits a push intent to the host ledger; that intent is a PR-wide hold until
a successful exact ref and PR readback and a committed completion clear it. An unknown ref update,
or a published ref whose PR state cannot be verified, quarantines the seat, suppresses the
review and merge handoff, and queues an operator notice; it is not retried.

While a loop is opted out, the fixer gate does not start a fixer turn at all. A changes-requested
verdict is held in the loop's fixer queue with the reason and the exact enable command (shown by
`explain`, `doctor`, the watchdog and the observer), and **no run-ledger row is created**. After
an explicit opt-in the watchdog drain re-delivers the held verdict to the fixer gate, which
re-checks it and enqueues a *new* row under the current policy. As defence in depth the
supervisor cancels, at claim and right before launch, any fixer row that was not admitted or whose
loop has since opted out, so no model turn is spent on a push the broker would refuse. A
redelivered event never upgrades an admission; `hermes review-loop retry` re-admits a run under
the policy in force at that moment and refuses while pushes are off.

Issue fixes (#214) need the same opt-in: the triage gate queues an issue-fix turn only while
unattended pushes are on, and the broker re-checks it under the same lock before the branch push.

**The fixer's client.** Inside the sandbox the fixer builds its push with
`python -m review_loop.broker_client push --files <path>... --message-file <file>` (or
`--message`; `--dry-run` checks without sending). It reads whole files from `/work`, fills
`content_b64` and `sha256`, and takes `base_head` from `/opt/client/review-loop-turn.json`, a
read-only file the host writes for fixer turns. That value is a convenience, not an authority:
the broker still requires `base_head` to equal the run's scoped head. The client repeats the
broker's limits (24 files, 64 KiB per file, 128 KiB total, a 240-byte message, safe path segments,
nothing under `.github/`, no `.git`, `.gitmodules`, `.gitattributes` or `CODEOWNERS`) so a refusal
happens before the one write is spent. A manifest can only add or replace regular files: never
delete, rename, change a mode or write a symlink.

**The fixer's answers.** The fixer's answers to the findings leave the sandbox only with its review
request: `python -m review_loop.broker_client request_review --answers-file <file>` (at most
8 KiB, checked by the client and again by the broker). The broker accepts answers only after this
run's confirmed push (or, for an incomplete view, instead of a push), authorizes them like the
fixer's other writes, commits a `posting` row to the ledger's `fixer_answers` table, then POSTs
**one** issue comment as the fixer, before the review request, so the woken reviewer can read it.
The comment starts with a hidden `<!-- review-loop:fixer-answers run=… head=… base=… -->` marker.
A POST whose outcome is unknown is recorded as `uncertain` and never retried; the review request
goes ahead either way. The next reviewer's and the adjudicator's PR records include only comments
that carry that marker **and** are authored by the configured fixer login, and only for a verdict
still in the record. The text is labelled as the fixer model's words: data, not instructions.

## Rulings, triage and issue fixes

- **Ruling.** The adjudicator's broker accepts exactly one `ruling` (ACCEPT, REJECT or RESPEC, with
  a bounded reason). The host records it in the run ledger first, then sends an observer `ruling`
  notice and queues it in the operator outbox. It posts a PR comment only for an optional,
  distinct `seats.adjudicator.login` (its own login, token file and `/user` principal, rechecked
  against the live PR before the POST).
- **Triage.** The broker accepts one `triage` request: labels only from the loop's list, at most
  `max_labels`, distinct, and a comment only where the loop allows one. It records the triage
  before writing, re-authorizes against the live issue and current config, then applies the
  labels and comment as the triage login (its own, or the reviewer's). An unknown outcome is
  recorded as `uncertain`.
- **Issue fix.** The broker accepts either `open_pr` (a file manifest, title and body) or
  `issue_comment`. It requires issue fixes enabled, distinct reader, reviewer and fixer accounts
  whose tokens resolve to themselves, and the issue still open, by an allowlisted author, with
  the fix label a maintainer applied. The branch push takes the push-policy lock and its lease
  requires the branch not to exist yet. Then it opens the PR as the fixer and requests review.
  Any step whose outcome is unknown is recorded as `uncertain` and not replayed.

The broker appends one metadata line per GitHub write (repo, PR, head, role, operation and
login; a push also lists its paths; never a token or a model-written body) to the loop's `broker-audit.jsonl`.

## Live verification: `hermes review-loop selftest`

`hermes review-loop selftest --loop ID [--pr N] [--no-model] [--live-turn] [--ping] [--timeout S]`
(command order: [Verifying the isolated setup](operations.md#verifying-the-isolated-setup-selftest))
checks, step by step:

1. the private runtime file and the paths it names, and each seat's resolved profile → provider /
   model (never the key or token);
2. bubblewrap user namespaces, and a probe inside the real sandbox layout that must not read a
   dummy host secret or any configured secret path;
3. one tiny real completion per distinct seat resolution (`--no-model` skips it);
4. `/user` identities and distinct principals for the read, reviewer, fixer and optional
   adjudicator tokens;
5. with `--pr N`: the reviewer-write authorization (`broker.authorize`, reads only), and whether
   the seat can build that head (`build:rust:fetch`, `build:rust`);
6. the run ledger and `doctor`'s cron, state and gateway checks;
7. with `--live-turn --pr N`: one real isolated reviewer turn, whose verdict is printed and never
   posted.

**No-write guarantees.** Every GitHub call the selftest makes passes a GET-only guard. The live
turn's `RunBroker(..., no_write=True)` is a host-only constructor flag, accepted only for reviewer
scopes. It is not a socket field: requests keep exactly `operation`, `verdict` and `body`, so a
request carrying any extra key is refused. In that mode the broker serves one `review`, runs
`broker.authorize` (reads only), keeps the verdict and body in host memory and answers the sandbox
as a real write would. It never calls `broker.perform`, the receipt path, the audit log or a
ledger. The printed checklist is redacted against the configured tokens and model keys.

**The one write: `--ping`.** With `--ping`, and only then, the selftest asks GitHub to ping each of
the loop's repo hooks (`POST /repos/{repo}/hooks/{id}/pings`, `review_loop/hook_ping.py`) and
reads back how the gateway answered. That proves the hook's secret end to end. The ping is
harmless by construction: the gateway checks the signature first (401 when it does not verify, 403
for a disabled route), and the loop's routes do not subscribe to `ping`, so a verified ping is
answered "ignored" and never reaches a gate. It needs a token with hook admin rights
(`--admin-token`). `arm` sends the same pings after it activates the hooks; `doctor` never does.

```bash
hermes review-loop selftest --loop name --no-model --ping
```

**What it does not prove.** The live turn runs in the CLI process, not in the detached supervisor
worker (different environment, no ledger row, no operator notice). Alert delivery still has to be
confirmed with a real enqueued turn and a watchdog sweep.

## Known limits

These are real and accepted, not bugs waiting to be found:

1. **The push is not an atomic PR-state/ref compare-and-swap** (`review_loop/safe_push.py`). The
   broker requires a freshly read PR that is open, not a draft, with matching number, base, head
   repository, branch and SHA, before every write and at the push's checkpoints, and the push uses
   an exact-SHA lease on `refs/heads/<branch>`. But Git's lease constrains only the branch ref.
   A close, draft conversion or retarget just after the last PR read can still land before GitHub
   accepts an unchanged branch ref. The readback after the push reports persistent ineligibility
   as `published_pr_unverified`, but cannot undo the published ref, and a close and reopen wholly
   inside that window is invisible (a test models exactly this). This is why enabling unattended
   pushes requires `--acknowledge-pr-race`.
2. **Same UID.** The sandbox shares the host user's UID inside a user namespace. It is not a
   separate host account. The boundary is the mount, network and process namespaces, not Unix
   permissions.
3. **`/etc/alternatives` is the host's.** It is bound read-only onto the staged `/etc` so that
   compilers resolve; it holds only symlinks into the read-only `/usr`.
4. **Not independently audited.** The source-snapshot and export staging (symlinks, file races,
   `/proc`, mounted sockets, the installed runtime) are covered by this project's tests, not by an
   external security review.
5. **Notices are not acknowledged delivery.** The operator outbox hands notices to cron output;
   see [the run ledger](#the-run-ledger-and-the-notice-outbox).
6. **Only some providers have run live.** The OAuth contracts, refresh and proxy are exercised
   against fake upstreams in the test suite. Live use so far (2026-10-03) covered an OpenAI Codex
   (OAuth) reviewer and an OpenAI-compatible API-key fixer, end to end on a real repository; the
   Claude-subscription DirectSDK backend was validated live on its own (#204). The other OAuth
   providers (xAI, Qwen, Nous, MiniMax) have passed only the offline tests; `selftest` (one real
   completion per seat) is the live check for yours.
7. **A PR chooses what the host downloads**, within the crate-cache cap and from crates.io only
   (see [dependency prefetch](#dependency-prefetch-issue-51-the-host-fetches-the-sandbox-builds-offline)).
   The cache is shared by a repository's PRs but read-only to every sandbox, so one turn cannot
   poison another.
8. **Model-readable text is untrusted.** PR descriptions, patches, issue bodies and the fixer's
   answers reach a model. The host labels them as data and limits what any answer can do: one
   scoped write per turn, through checks the model cannot change.
9. **Direct config edits bypass the push-policy lock.** Use `fixer-push --disable` rather than
   editing the file.

## Offline evidence

The test suite exercises these boundaries without live credentials:

- `tests/test_route_vertical.py` runs the real route scripts as subprocesses with disposable
  `HOME`/`HERMES_HOME`: rejection, missing-runtime hold, durable enqueue, duplicate suppression,
  detached worker failure, and `[SILENT]` on every path.
- `tests/test_turn_vertical.py` runs a real bubblewrapped Hermes tool turn against a dummy model
  endpoint and a fake GitHub API: the model's terminal cannot read host-only dummy token or key
  paths, runs `cargo test --offline`, and receives a scoped broker acknowledgement.
- `tests/test_route_worker_vertical.py` runs the reviewer route script, the production worker and
  the unchanged turn, broker and fetch modules in a disposable package copy, through to one scoped
  review and a `succeeded` ledger row, including stale heads, an out-of-scope write and
  token/key containment.
- `tests/test_seat_models.py`, `tests/test_seat_model_boundary.py` and `tests/test_oauth_seats.py`
  cover per-seat resolution, refused providers, the per-mode proxy contracts, OAuth refresh, and
  bwrap probes that cannot read profile secrets.
