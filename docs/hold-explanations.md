# Shared hold explanations — ledger slice of #674

This is **partial implementation of #674**, not the complete three-surface policy contract.
It introduces an additive, consumed contract for ledgered stalled runs. It does not close #674.

## Owner and consumers

`diaktoros.run_supervisor.hold_explanation(row)` accepts the artifacts returned by the existing
read-only `runs_view` / `read_only_view`. It performs no IO, probe, admission decision or write.
`describe_run` appends its exact public text, so CLI status, explain and the supervisor's status
formatter share the explanation. Desktop's existing `/pr` read attaches the same object to
`stuck[].explanation`; the registered drawer validates it before cache admission and displays its
exact text. No query route, GitHub polling loop or write control is added.

Existing host next-step guidance and CLI exit codes are retained. The only correction to the
next-step formatter is that a contradictory waiting/cancelled row with durable write evidence,
or an uncertain row without a write annotation, cannot advise automatic restart or re-admission.
This changes advice, not `Supervisor.retry`, gate execution, workers, or watchdog behavior.

## Schema 1

| Field | Meaning |
| --- | --- |
| `schema` | Integer `1`; incompatible schemas are rejected. |
| `reason_code` | One of the stable codes below. No arbitrary error-string classification. |
| `run_id` | The host's 32-lowercase-hex run identity, or null when unavailable/malformed. |
| `head` | Full 40-lowercase-hex recorded head, or null; never a live-head claim. Desktop checks its prefix against the parent row. |
| `evidence_source` | `run_ledger`; not current GitHub, runtime health or a delivery receipt. |
| `completeness` | `partial`; policy-specific reason and current admission facts are not established. |
| `waiting_for` | `host_recheck`, `operator_inspection`, or `unknown`. |
| `automatic_resume` | `never` for external-write replay guards; otherwise `unknown`. |
| `next_eligible_start` | Recorded `retry_at` epoch seconds for a waiting, non-write-guarded row, or null. Finite, nonnegative and bounded to the public datetime epoch range. |
| `operator_action` | `inspect` only. This is guidance, not an executable command or permission. |
| `text` | Host-generated bounded rendering of those facts; never error, path, account, credential or write-evidence prose. |

A recorded eligibility time is **not a promised start**, a live provider reset, current CI result,
or proof that the run remains eligible. Rechecking can discover another hold. A null time means
unknown, not immediate eligibility. Automatic resume is deliberately unknown until the policy
owner can supply a structured, current condition. Desktop retains and labels stale reads; an older
backend without the additive field explicitly shows that the shared explanation is unavailable.

### Stable reason vocabulary

- `run_waiting`: the ledger records waiting; the cause/event is not structured here.
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

- `run_supervisor.next_step` / `describe_run`: existing ledger advice, `write_evidence`, budget and
  policy cancellation distinctions. `retry_action.plan` / `page_targets` and transactional
  `Supervisor.retry` remain the sole retry owners.
- `gate.explain`: ordered live guard predicates and `next` report; `situation.resolve` /
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

1. Policy owners must emit structured CI/provider-window/daily-cap/human-path/runtime/conflict/
   escalation conditions, evidence provenance and supported checked actions. In this slice those
   cases are **not** reclassified from their recorded prose; a waiting row remains `run_waiting`
   with partial/unknown policy facts. No missing field is treated as an off switch or permission.
2. Integrate `gate.explain`'s structured answer without changing its guard order or its documented
   CLI semantics. Non-ledger holds and pre-admission runtime queue failures are not covered by this
   ledger projection.
3. Integrate observer notices from those same policy-owned artifacts. Existing observer wording,
   delivery and reconciliation remain unchanged; **three-surface consistency is not yet met**.
4. Supply source-specific currently eligible actions from existing checked operations. `inspect`
   intentionally does not advertise retry, continue, pacing release, conflict writes or escalation.
5. Prove all named issue scenarios using their actual policy writers across all three consumers.
   The current prose-invariance tests prove non-inference, not complete scenario acceptance.

Core files owned by concurrent #1151/#1154 work are intentionally untouched. If their new contracts
alter gate facts, integrate with those owners rather than adding another policy implementation.

## Tests

`tests/test_hold_explanations.py` creates real disposable supervisor ledger artifacts, reads them
through production readers, checks CLI/Desktop equality, compares the public fixture matrix,
exercises a durable review write-ahead receipt, and tests unknown/malformed fields and replay
safety. The future-state fixture is an explicit compatibility unit case, not a writer-produced
policy state. No test starts a real worker or contacts GitHub.

`tests/desktop_behavior.mjs` consumes that backend-checked matrix through the production drawer's
query callback and renderer. It rejects malformed nested identity/provenance/actions/text before
cache admission, and distinguishes retained stale, unknown and legacy responses. React/SDK hooks
in these unit tests remain synthetic; native React browser render QA is separate. The required
locked real-cache runner now executes 205 tests (192 fixture cases plus the existing 13 named
real-TanStack cases), with exact execution counts and zero skips/TODOs required.
