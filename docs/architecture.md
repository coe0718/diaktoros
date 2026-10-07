# Architecture

[Documentation index](README.md) · [Security](security.md) · [Development](development.md) · [Operations](operations.md)

## Contents

- [Execution flow](#execution-flow)
- [Seats and scheduling](#seats-and-scheduling)
- [Per-turn staging](#per-turn-staging)
- [Inference and publication](#inference-and-publication)
- [Durable state and recovery](#durable-state-and-recovery)
- [Routing and observation](#routing-and-observation)
- [Source map](#source-map)

## Execution flow

The plugin is a host-side control plane around isolated Hermes turns. Gate scripts do not dispatch credential-owning gateway agents: eligible events enqueue isolated work and return `[SILENT]`. A configured runtime and armed hooks are prerequisites for production execution; an event reaching a route is not evidence that a turn launched. See [configuration](configuration.md) and [troubleshooting](troubleshooting.md).

```text
GitHub webhook -> Hermes route -> gate script -> SQLite run ledger
                                               |
                                         supervisor worker
                                               |
                                  fresh reads, exact-head export
                                               |
                                    bubblewrap-contained Hermes
                                      /                 \
                            model.sock                 broker.sock
                                |                           |
                      host inference capability      host scoped broker
                                |                           |
                      model provider/native client   GitHub REST / leased Git

watchdog -> recovery, pacing, route checks, operator notices
observer -> event delivery, not another agent seat
```

A production turn proceeds as follows:

1. **Admission:** the gate filters the event, checks current facts and queues a deduplicated row with a seat and turn budget. The worker launch is separate from the webhook response.
2. **Claim:** the supervisor transaction checks seat capacity and PR occupancy. It records launch intent before entering the launch path and maintains a lease/heartbeat.
3. **Revalidation:** the production worker reloads runtime settings and reads the current PR or issue. Closed, moved, draft, unreadable and policy-held work are handled differently; unknown state is not an eligible state.
4. **Preparation:** the host creates the prompt/change record, records incomplete-view reasons, exports the exact commit and prepares permitted dependencies. This preparation precedes the sandbox's turn clock.
5. **Execution:** the worker exposes two per-turn Unix-socket capabilities and starts Hermes inside bubblewrap. The sandbox has no host network namespace or real seat credentials.
6. **Publication:** the sandbox proposes a bounded operation. The broker—not the model—selects the destination and identity, checks live authorization and records intent before external writes.
7. **Completion:** the worker records the outcome and releases ordinary completed claims. An uncertain run keeps its occupancy until reconciled; failure notices use a host outbox.

The sandbox process exiting successfully is not enough: `trusted_turn.run_turn` rejects a zero exit without a completed scoped submission. A recorded adjudicator ruling is complete even if a later optional comment/notice delivery fails. See [security](security.md#scoped-write-policy).

## Seats and scheduling

| Seat | Work | Boundary |
|---|---|---|
| `reviewer` | Review an eligible PR | One `APPROVE` or `REQUEST_CHANGES`; no `COMMENT` verdict |
| `fixer` | Answer a changes-requested review and fix its scoped head | Push is disabled by default; publication requires host opt-in and admission |
| `adjudicator` | Rule after an opted-in escalation | Read-only checkout; `ACCEPT`, `REJECT` or `RESPEC` |
| `triage` | Classify an opted-in, allowlisted issue | No repository checkout; configured labels and optional comment |
| `issue_fixer` | Fix an opted-in issue | Scoped base export; new-branch/PR workflow or explanation comment |

The main seats have effective per-seat concurrency, falling back to the loop limit and then serialized operation. Adjudicator and triage seats default to one but have independently configurable concurrency in the loop file; they do not inherit the loop limit. Issue fixing remains fixed at one concurrent `issue_fixer` turn. The ledger also prevents concurrent active runs on the same repository/PR. Capacity is not enforced by sharing a checkout: each turn has a private export. A configured `clone` is still required when effective main-seat concurrency exceeds one, even though isolated exports do not build in that clone.

Reviewer event filtering deliberately ignores `synchronize`. Initial eligible open/ready/reopen events and the fixer's authorized review request are handoffs; intermediate pushes are not review triggers. The round cap is based on effective GitHub verdicts rather than a local incrementing round counter. Without the opt-in adjudicator route, escalation records a breach/operator handoff instead of launching another seat; [add adjudication to an existing loop](configuration.md#adjudication) when wanted. See [concepts](concepts.md) and [issues](issues.md).

Profiles, GitHub logins, credentials and webhook routes are distinct pieces of seat configuration. Changing identity can require rebinding a route, not merely changing a config field. See [accounts](accounts.md) and [configuration](configuration.md).

## Per-turn staging

There are two different source trees:

- **Hermes executable source:** `trusted_turn._safe_code_snapshot` exports allowed regular blobs from the configured Git repository's committed `HEAD`, not its mutable worktree or index. It verifies blob hashes, filters paths/content, and mounts the snapshot as `/opt/code`. Uncommitted Hermes changes therefore do not enter a turn by this path.
- **Repository under review:** `trusted_fetch.stage` validates identity and live head, obtains commit/tree metadata and a bounded tarball, verifies every file against the Git tree, rechecks the head, then publishes the export without replacing an existing directory. It carries no `.git` checkout or remote.

The latter accepts regular files and executable regular files, not symlinks, submodules or `.gitmodules`; a truncated tree, unsafe path or blob mismatch refuses the export. Bounds are 10,000 tree entries and 100 MiB of file bytes, with separate metadata/response bounds. Triage uses an empty read-only directory instead; issue fixing stages a commit still on the configured base branch.

Writable seats copy the read-only export into a sized `/work` tmpfs. Adjudicator and triage `/work` mounts stay read-only; a separate sized `/target` supports builds without editing their export. `/tmp` is also a sized tmpfs. The per-turn home is a writable host bind, so not every sandbox write surface is size-capped; [security limitations](security.md#limitations-and-operator-responsibilities) matter here.

Dependency prefetch currently implements Rust. The host parses the root `Cargo.lock`, downloads supported crates.io sources using a synthetic manifest and scrubbed environment, and never runs the PR's build scripts on the credentialed host. A held cache generation is mounted read-only; Cargo runs offline in the sandbox. Missing dependencies, Git sources or alternate registries are reported as unavailable, not silently fetched from PR-selected URLs. This is not universal dependency support.

## Inference and publication

The inference proxy fixes the model and upstream from host configuration and owns API-key/OAuth authentication. Its local bridge is inside the sandbox network namespace; it is not an exposed gateway service. Wire contracts cover `chat_completions`, `codex_responses` and `anthropic_messages`. A host-only DirectSDK backend additionally adapts `claude-subscription-directsdk-experimental` behind the same inference socket. The sandbox does not choose or launch that native executable. See [security](security.md#inference-policy-and-directsdk).

Fixers do not push their checkout. They supply whole-file bytes, hashes and a scoped base SHA in a manifest. The host validates it and constructs one direct-child commit in a fresh bare repository, then updates the scoped branch using an exact-SHA lease. The host verifies the resulting ref and PR again. This protects the branch SHA, **not atomic PR metadata authorization**: closing, drafting or retargeting can race the last check and Git's receive-pack. [Security](security.md#push-policy-and-the-pr-race) states the policy and residual risk.

A reviewer submission is associated with a host-owned generation and an exact review-ID receipt, not just a matching head SHA in arbitrary review text. `review_receipt.generation_for` currently resolves the configured root base and records an empty, unverified parent chain. Stacked-state/reconciliation machinery exists elsewhere, but this receipt implementation is not a complete stacked-parent provenance resolver.

## Durable state and recovery

Host-wide state includes `diaktoros-runs.sqlite` and pacing state beneath `$HERMES_HOME/state/`. The run ledger contains run claims, review receipts, push intents, rulings, triage/issue-fix results, answers and notice delivery state. `ledger.connect` wraps transaction semantics and closes connections deterministically.

Per-loop JSON files provide visible claims, pending/in-flight work, breach markers, observations, route intent and other operational records; `broker-audit.jsonl` journals broker metadata. See [configuration](configuration.md) for locations and [operations](operations.md) for reconciliation. Do not treat deleting these files as clearing a harmless cache.

Recovery distinguishes an expired claim before launch from an ambiguous launched run. Pre-write failures may wait with backoff and retry; write evidence or an uncertain outcome prevents automatic replay. Runtime reload failures, turn-budget kills, policy cancellation and provider pacing have their own handling. Retry decisions come from host records, not a successful/failed child exit alone. Daily caps and provider rate limiting postpone admission/execution rather than authorizing unbounded retries.

Notices are not an exactly-once distributed delivery guarantee. The outbox claims `sending` before a transport call; explicit failure can return it to `pending`, while a crash can leave ambiguous sending state. Operators should retain run IDs and inspect unresolved state.

## Routing and observation

`routes.py` edits Hermes's subscription registry with locking, conflict checks and durable replacement, preserving unrelated entries. Route intent supports restoring missing or still-owned entries; a name now owned by another script is reported as a conflict, not overwritten. Watchdog recovery cannot substitute an arbitrary route for an owned seat gate.

Profile binding and actual registered route must agree. Gateway route-script resolution is part of the integration boundary and is tested against pinned Hermes source in CI. Observer routes use Hermes's `deliver_only` mode to deliver events without another model turn. The observer is neither an adjudicator nor a security audit service. See [observer](observer.md).

## Source map

Links point to implementation, not an assertion that a test ran on this machine.

| Concern | Implementation | Regression evidence |
|---|---|---|
| Admission and event filtering | [`gate.py`](../review_loop/gate.py), [`scripts/`](../scripts/) | [`test_fixer_gating.py`](../tests/test_fixer_gating.py), [`tests/harness/gates.py`](../tests/harness/gates.py) |
| Worker lifecycle, capacity and recovery | [`run_supervisor.py`](../review_loop/run_supervisor.py), [`ledger.py`](../review_loop/ledger.py) | [`test_run_supervisor.py`](../tests/test_run_supervisor.py), [`test_retryable_turns.py`](../tests/test_retryable_turns.py), [`test_ledger_close.py`](../tests/test_ledger_close.py) |
| Launch, snapshots and mounts | [`trusted_turn.py`](../review_loop/trusted_turn.py), [`contained.py`](../review_loop/contained.py) | [`test_contained_agent.py`](../tests/test_contained_agent.py), [`test_snapshot_secrets.py`](../tests/test_snapshot_secrets.py), [`test_sandbox_limits.py`](../tests/test_sandbox_limits.py) |
| Exact-head export and dependencies | [`trusted_fetch.py`](../review_loop/trusted_fetch.py), [`deps.py`](../review_loop/deps.py) | [`test_trusted_fetch.py`](../tests/test_trusted_fetch.py), [`test_deps.py`](../tests/test_deps.py) |
| Inference and native adapter | [`inference_proxy.py`](../review_loop/inference_proxy.py), [`directsdk_backend.py`](../review_loop/directsdk_backend.py), [`directsdk_guard.py`](../review_loop/directsdk_guard.py) | [`test_inference_proxy.py`](../tests/test_inference_proxy.py), [`test_directsdk_backend.py`](../tests/test_directsdk_backend.py), [`test_directsdk_guard.py`](../tests/test_directsdk_guard.py) |
| Scoped writes and receipts | [`broker.py`](../review_loop/broker.py), [`broker_ipc.py`](../review_loop/broker_ipc.py), [`safe_push.py`](../review_loop/safe_push.py), [`review_receipt.py`](../review_loop/review_receipt.py) | [`test_broker_ipc.py`](../tests/test_broker_ipc.py), [`test_safe_push.py`](../tests/test_safe_push.py), [`test_review_receipt.py`](../tests/test_review_receipt.py) |
| Registry and installed integration | [`routes.py`](../review_loop/routes.py), [`route_intent.py`](../review_loop/route_intent.py), [`CI`](../.github/workflows/ci.yml) | [`test_routes_atomic.py`](../tests/test_routes_atomic.py), [`test_gate_shims.py`](../tests/test_gate_shims.py), [`test_route_worker_vertical.py`](../tests/test_route_worker_vertical.py) |
