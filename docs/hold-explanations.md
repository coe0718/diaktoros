# Hold explanations

[Documentation map](README.md) · [Development](development.md) · [Architecture](architecture.md) · [Operations](operations.md)

This page is the developer contract for one shared explanation of why a ledgered run is held.
It is a **partial implementation of #674** (the ledger slice), not the complete three-surface policy contract.
It introduces an additive, consumed contract for ledgered stalled runs. It does not close #674.

## Owner and consumers

`diaktoros.run_supervisor.views.hold_explanation(row)` accepts the artifacts returned by the existing
read-only `runs_view` / `read_only_view`. It performs no IO, probe, admission decision or write.
`describe_run` appends its exact public text, so CLI status, explain and the supervisor's status
formatter share the explanation. Desktop's existing `/pr` read attaches the same object to
`stuck[].explanation`; the registered drawer validates it before cache admission and displays its
exact text. No query route, GitHub polling loop or write control is added.

The structured hold reasons come from one owner, `run_supervisor.waiting_hold(error)`. It
recognises only the host writers' own prefixes and marks (`CI_HOLD`, `pacing.closed_account`,
`DAILY_TURN_CAP` / `DAILY_TOKEN_BUDGET`), the same ones `next_step`, `hold_remedy`, the observer's
held notice and the ledger's SQL already key on; the daily-cap writers in `turns` now word their
lines with those constants. `views.HOLD_REASONS`, `HOLD_COPY` and `WAITING_FOR_COPY` hold the
pairing and copy, and the Desktop validator mirrors them (a test pins the mirror). Write evidence
or an uncertain state always wins over any hold line.

`next_step` remains the sole next-action advice. `hold_explanation.text` adds recorded facts,
not a second instruction to inspect or a duplicated automatic-resume verdict. Its structured
`automatic_resume` and `operator_action` fields remain compatibility metadata, not another
executable action. Waiting guidance describes a recorded earliest recheck, never a promised
start or a known provider reset: a start still depends on host eligibility. Existing retry,
budget and policy-cancellation advice and CLI exit codes are retained. A contradictory
waiting/cancelled row with durable write evidence, or an uncertain row without a write
annotation, cannot advise automatic restart or re-admission. This changes presentation, not
`Supervisor.retry`, gate execution, workers, or watchdog behavior.

The PR read always includes an explicit boolean `ledger_available`. A missing ledger returns
`false`; empty ledger-derived collections in that response are not verified-empty history.
The flag is `true` only after successful reads, including a successfully read empty ledger.
Unreadable SQLite data follows the route's existing bounded unavailable error contract.
Independent findings/decisions remain visible when the ledger is absent.
Desktop requires this flag before cache admission; an older response without it is unavailable,
not verified-empty. Failed or malformed refreshes retain the last successful read with a stale
disclosure. Empty turn/verdict/waiting claims are shown only for a successfully read ledger.

## Schema 1

| Field | Meaning |
| --- | --- |
| `schema` | Integer `1`; incompatible schemas are rejected. |
| `reason_code` | One of the stable codes below. No arbitrary error-string classification. |
| `run_id` | The host's 32-lowercase-hex run identity, or null when unavailable/malformed. |
| `head` | Full 40-lowercase-hex recorded head, or null; never a live-head claim. Desktop requires a nonnull full safe head whose prefix matches a valid parent row. A malformed parent with a null explanation head is unavailable for that row, not the entire PR response. |
| `evidence_source` | `run_ledger`; not current GitHub, runtime health or a delivery receipt. |
| `completeness` | `partial`: a ledger snapshot; current admission facts are not established. |
| `waiting_for` | `host_recheck`, `operator_inspection`, `unknown`, or a structured hold's own condition: `ci_checks_or_new_head` (event), `provider_window_reset` or `daily_cap_reset` (time). |
| `automatic_resume` | `never` for external-write replay guards; `after_event` for an event-based hold; `after_recorded_time` for a time-based hold; otherwise `unknown`. Each reason code has exactly one pairing. |
| `next_eligible_start` | Compatibility field name for recorded earliest recheck (`retry_at`) epoch seconds for a waiting, non-write-guarded row, or null; not a start promise. For a time-based hold it is the recorded reset (window end, local midnight); for the CI hold it is the next CI read. Finite, nonnegative and bounded to the public datetime epoch range. |
| `operator_action` | `inspect` only. This is guidance, not an executable command or permission. |
| `text` | Host-generated bounded rendering of recorded facts only; never next-action advice, error, path, account, credential or write-evidence prose. |

A recorded eligibility time is **not a promised start**, a live provider reset, current CI result,
or proof that the run remains eligible. Rechecking can discover another hold. A null time means
unknown, not immediate eligibility. Automatic resume stays unknown unless the host's own hold
writer supplied the condition (a structured hold above); even then it means the host rechecks
the row after that event or time, not that a turn will start. Desktop retains and labels stale reads; an older
backend without the additive explanation field explicitly shows that explanation as unavailable
(provided its PR envelope includes the required ledger availability). Malformed recorded heads
are withheld as an empty wire identifier and displayed as unknown — in stuck rows, turns and the
drawer's adjudicator-ruling summary alike; the row and host next-step
remain visible, but its explanation cannot claim head binding.

### Stable reason vocabulary

- `run_waiting`: the ledger records waiting; the cause/event is not structured here (a retry
  backoff, a claim-read backoff, or any `held:` line no structured writer below produced).
- `ci_wait`: a reviewer turn held by `CI_HOLD` (running, cancelled, missing or red checks, or a
  head that conflicts with its base). Event-based: waits for CI checks to settle or a new head;
  a new head queues a fresh run rather than resuming this one.
- `provider_window`: `pacing.hold_line`'s closed account (a usage window or an unavailable
  provider). Time-based: `next_eligible_start` is the recorded window end.
- `daily_cap`: a seat's daily turn cap, review-only cap or daily token budget. Time-based:
  `next_eligible_start` is the recorded local midnight.
- `run_failed`: the ledger records failure without write evidence in this artifact.
- `run_cancelled`: the ledger records cancellation without write evidence in this artifact.
- `external_write_guard`: the host's write-evidence helper found a write-ahead/recorded write, or
  the recorded state is uncertain. This includes confirmed writes: it does **not** claim every
  receipt is unverified. No replay recommendation or next eligible start is emitted.
- `unknown`: this artifact does not establish one of these explanations.

The wire validator bounds the exact nested shape, provenance, vocabulary, identifiers, timestamp
and public text. Its allowlist validates schema/copy; it neither grants a retry nor runs gate
predicates. Retry buttons continue to depend only on the existing `retry_action` verdict.

## Inventory taken before this slice

- `run_supervisor.views.next_step` / `describe_run`: existing ledger advice, `write_evidence`, budget and
  policy cancellation distinctions. `retry_action.plan` / `page_targets` and transactional
  `Supervisor.retry` remain the sole retry owners.
- `explain.explain`: ordered live guard predicates and `next` report; `situation.resolve` /
  `parent_readiness`: stacked-base identity and associated parent approval checks.
- `pacing.seats` / `held` / `when`: live provider windows and seat daily counters. Historical ledger
  text must not be promoted into this live view.
- `fix_hold.ask_line` / origin-PR holds / `issue_deps.blocking`: maintainer question/plan,
  originating PR and declared prerequisite waits; these have distinct release conditions.
- `pr_hold.line` / `release_how`, `scope_lock.line` / snapshot compare-and-release, exact-head
  `human_hold`: deliberate holds and independently authorized release paths.
- `observer.hold_reason` / `verified_turn`: exact-head runtime queue prefix only, not all holds;
  observer receipts and uncertain delivery reconciliation have their own replay rules.
- Runtime enqueue failure, human paths, conflicts and escalation predicates remain with their
  existing host owners. No formatter infers their remedies from exception strings.

## Residual work for #674

1. Structured ledger holds now cover CI (including base conflicts), provider windows/outages and
   daily caps. Still unstructured (`run_waiting` or not ledger rows at all): retry and claim-read
   backoffs, human-path holds, runtime enqueue failures, escalation parks (a gate marker that
   cancels the queued turn, owned by `explain`), maintainer `pr_hold`/scope-lock holds and issue
   prerequisites. No missing field is treated as an off switch or permission.
2. Integrate `explain.explain`'s structured answer without changing its guard order or its documented
   CLI semantics. Non-ledger holds and pre-admission runtime queue failures are not covered by this
   ledger projection.
3. Integrate observer notices from those same policy-owned artifacts. Existing observer wording,
   delivery and reconciliation remain unchanged; **three-surface consistency is not yet met**.
4. Supply source-specific currently eligible actions from existing checked operations. `inspect`
   intentionally does not advertise retry, continue, pacing release, conflict writes or escalation;
   `next_step` still carries the `--clear-hold` / raise-the-cap advice for pacing holds.
5. Desktop `/now` rows carry `why` (the host line) but not this structured explanation.

### Closure boundaries

The narrow #1159/#1161/#1166 fixes require missing-versus-empty production-route/query/render
proof, rejection of null explanation heads against valid parent identities, visible unavailable
explanations for unknown heads, and consistent real-ledger waiting/failed/cancelled/uncertain
advice in CLI and Desktop. Acceptance must run at the frozen source commit, including the locked
JavaScript runner's 232 tests (13 of them named real-TanStack cases) with no skips,
the strict dependency-equipped DesktopPython lane, full harness and leakguard sweep, plus
source-hash-bound actual React browser renders. Browser SDK fixtures are not live Electron
acceptance. These fixes do not close #674, implement #1301, or change execution policy.

## Tests

`tests/test_hold_explanations.py` creates real disposable supervisor ledger artifacts, reads them
through production readers, checks CLI/Desktop equality, compares the public fixture matrix,
exercises a durable review write-ahead receipt, and tests unknown/malformed fields and replay
safety. Real-ledger waiting, failed, policy-cancelled and uncertain rows retain host advice
without duplicated instructions in their explanation. Missing versus successfully read empty
ledgers and the actual production PR route's missing/corrupt ledger responses are covered.
The structured-hold cases are made by their real writers (`turns._cap_held`, `pacing.hold_line`,
`turns._ci_hold_text`) and appear in the CLI and Desktop identically; `test_pacing` and
`test_review_after_ci` also read the rows the production worker records for closed windows,
outages, daily caps, token budgets, running CI and base conflicts. Unrecognised `held:` prose
stays `run_waiting`, and every hold line with write evidence or an uncertain state stays a
write guard with no recheck or replay advice. The future-state fixture is an explicit
compatibility unit case, not a writer-produced policy state. No test starts a real worker or contacts GitHub.

`tests/desktop_behavior.mjs` consumes that backend-checked matrix through the production drawer's
query callback and renderer. It rejects malformed nested identity/provenance/actions/text before
cache admission, and distinguishes retained stale, unknown and legacy responses. React/SDK hooks
in these unit tests remain synthetic; native React browser render QA is separate. The required
locked real-cache runner checks exact execution counts and requires zero skips/TODOs, including
the named real-TanStack cases.
