# DirectSDK backend validation and remaining scope

**Current status:** text, streaming, a synthetic no-write sandbox reviewer turn, native
history/tool-result replay and observed helper/native-child cancellation passed live.
The receipts below are chronological; early failed/unverified statuses are superseded by
the follow-up sections. The full offline suite still has the independently reproduced
base leakguard fixture failure. Real GitHub/route/fixer/adjudicator operation is not claimed.

Implementation base: `28e7eb5f4ac3494d4b26d12188669e68f99127fd`.
Read-only upstream Hermes source inspected: `a7c2df3846f7d6040dd81b7573bd962da546222e`.
Installed experimental DirectSDK source inspected: `ef73726cfaf2fa0ee041e55572f406e2c24fed83`.

## Receipts

- Canonical standalone harness, `/usr/bin/python3 tests/run_tests.py`:
  **1760/1760 checks pass**, exit 0.
- Full offline pytest, runtime venv, `python -m pytest tests/ -q -o addopts= --tb=short`:
  **1134 passed, 18 skipped, 690 subtests passed, 1 failed**, exit 1.
  The failure is `RecorderReachesOnlyFixtureWorkers.test_a_fixture_command_run_by_the_worker_carries_it_too`
  in `tests/test_leakguard_children.py:278`: the fixture worker's PYTHONPATH has an empty first entry
  instead of the expected leaksite directory. The exact same test fails at the unchanged base
  `28e7eb5` in a disposable detached worktree. No leakguard changes are included here.
- Final targeted tests after the added helper tests and resolver/wire changes:
  `tests/test_directsdk_child.py tests/test_directsdk_backend.py tests/test_inference_proxy.py
  tests/test_seat_models.py tests/test_oauth_seats.py tests/test_selftest.py`:
  **126 passed, 4 skipped, 76 subtests passed**, exit 0.
- `git diff --check`: exit 0.
- Real upstream provider-profile import, runtime discovery and message sanitization were exercised
  in a disposable scratch Hermes home without a model call. The inert wire profile resolves to
  `chat_completions` at `http://127.0.0.1:18761/v1`, and the native assistant reasoning carrier
  survives `ChatCompletionsTransport.convert_messages`.

The full-suite receipt preceded the final helper cancellation/resolver test additions. Their final
state is covered by the targeted receipt; no claim is made that a second full suite passed.
The repository's existing `_home_guard` relocates guarded fixture scratch outside the real home
(to `/var/tmp`) despite the invoking TMPDIR under the operator's scratch. It was not disabled or
weakened. Logs and probes were retained under `/home/jeremy/.hermes/cache/scratch`.

## Live smoke — failed, not a successful inference receipt

Exactly one controlled text-only model probe was attempted through production
`selftest._one_completion` -> the Unix inference capability -> the host DirectSDK helper.
It requested `claude-sonnet-5[1m]`, 16 output tokens, and “Reply with the single word OK.”
It used the existing host native login, no repository context, no tool schemas, no repository
writes, and no GitHub posts. Result: **HTTP 502**, with the sanitized DirectSDK-host-process failure.
The installed module import, native command discovery and Client construction were separately
verified without another model request. The native failure was not diagnosed to a specific
provider/auth/CLI cause; do not treat this as a working live-subscription receipt.

No live sandbox reviewer/fixer/adjudicator turn, native history replay, native tool-result round
trip, live streaming, or live native descendant cancellation was exercised. Offline fixtures
prove wire forwarding, host helper isolation, response bounds, cancellation and SDK-shaped data,
not the installed native CLI's complete end-to-end behavior. Existing full-suite vertical tests
cover the general production sandbox boundary, not a successful live DirectSDK turn.

## Follow-up: false disconnect diagnosis and fix

The authorized diagnostic retry still returned 502. A no-network replay then reproduced
502 at the real socket/helper boundary. Python performs its timeout poll before `recv`
even when `MSG_DONTWAIT` is supplied: the capability's client socket has a positive
three-second request timeout, so peeking at an idle, connected client raised TimeoutError.
The relay classified that as a backend failure and closed the helper. This was independent
of SELinux and could cancel initialization before its diagnostic handler was installed.

The backend now checks socket readability with a zero-timeout select before peeking.
A new regression exercising the actual Unix HTTP capability with a disposable SDK
fixture failed with HTTP 502 before the fix and passed with HTTP 200 afterward.
Follow-up targeted receipt: **19 passed, 1 skipped, 46 subtests passed** for the DirectSDK
child/backend and inference-proxy files. Native inference remains unverified: no additional
post-fix live request was made under the one-diagnostic-request permission.

## Post-fix live verification

With explicit permission for up to three additional tiny requests, all three passed:

1. Host-diagnostic text probe through `selftest._one_completion`: **HTTP 200, reply OK**.
2. Uninstrumented production selftest probe: **HTTP 200, reply OK**.
3. Uninstrumented production socket/helper SSE probe: **HTTP 200, reply OK**, terminal
   `[DONE]`, final finish reason, and the exact native history carrier present.

Each requested 16 output tokens from `claude-sonnet-5[1m]`, used no tool schemas or
repository context, and performed no repository writes or external posts. The temporary
host tracer was not installed into production. These receipts supersede the earlier
unverified text/streaming status; no live sandbox role turn, native tool-result round trip,
carrier replay on a subsequent request, or forced native descendant cancellation was tested.

## Live sandbox, replay and cancellation verification

The separately authorized six-call integration budget used **five host inference calls**:

- Production `trusted_turn.run_turn` launched real Hermes in bubblewrap against a disposable
  synthetic repository, with the actual host DirectSDK client and native subscription.
  It exited **0** and submitted **one authorized APPROVE verdict** to the production broker's
  host-only `no_write` mode. The verdict contained the exact token read from the fixture file.
- **Two requests replayed sandbox tool results** to the native client. The captured host request
  shapes contained both the exact native assistant carrier and the fixture token in tool output.
  This verifies a real sandbox file read, native history/tool-result replay and subsequent
  verdict—not just an SDK-shaped fake response.
- A separate live streaming request was cancelled only after observing a native child process.
  **Two owned processes were observed** (helper and native child); after cancellation,
  **zero remained live**, helper exit code was **1**, and cleanup took **0.086 seconds**.
  PID start times were checked to avoid PID-reuse false positives. This is cancellation, not
  normal completion. The probe makes no claim to have separately exercised every possible
  late-spawned native grandchild shape.
- **Zero GitHub writes**. All GitHub reads were an explicit synthetic fixture; unexpected
  endpoints or any write method failed. No real PR authorization or live webhook/worker
  dispatch was claimed. Fixture/export/work paths were disposable, with no real repository
  contents or host credentials delivered to the model/sandbox.

The host fixture enforced a shared cap of six calls across main/auxiliary requests and the
cancellation probe, reduced output ceilings to 512 tokens, used a 180-second sandbox turn
budget, and stopped with five calls. It kept production snapshot, containment, broker,
inference socket, inert wire profile and installed native-client implementations unchanged;
only repository/GitHub fixtures and stricter inference test limits were supplied.

Local receipts: `/home/jeremy/.hermes/cache/scratch/directsdk-integration-receipt.json` and
`/home/jeremy/.hermes/cache/scratch/directsdk-live-integration.log`.
Opt-in fixture runner: `/home/jeremy/.hermes/cache/scratch/directsdk-live-integration.py`.
Those files are host-local probes, not installed runtime/config changes.

Still outside scope: real GitHub identity/ref authorization, live route-to-worker delivery,
fixer publishing, adjudicator ruling, and native cancellation under every descendant race.

## Security maintenance notes

- Preserve native reasoning using an inert registered ProviderProfile declaring the exact carrier
  type. A generic custom HTTP route strips another provider's `.native_assistant` sidecar.
- Keep DirectSDK Client construction in the host helper. Do not route sandbox JSON into command,
  args, environment, cwd, plugin paths, timeout, or any other constructor kwargs.
- Native login defaults to the OS user's account. Distinct Hermes profiles require explicit
  native config-directory selection to use different subscriptions; never copy auth stores.
- Keep arbitrary `process://` URLs refused by the HTTP endpoint validator. Only the exact named
  process backend marker chooses DirectSDK, with the existing request/model/token/call limits.
- Keep SDK cancellation through Client.close(): native Claude owns a separate process group,
  so killing only the helper group is insufficient for ordinary cancellation.

## Independent host launch lockdown (PR #204 review fix)

The helper installs review-loop's `subprocess.Popen` guard before importing the provider or
constructing its client. Every launch must use the host-resolved Claude executable and the
reviewed one-turn flag grammar: empty tools/settings sources, strict MCP configuration,
`dontAsk`, one turn, no slash commands and no session persistence. Duplicate flags, equals-form
flags, unknown flags and alternate executables/shell launches fail closed before spawning.
MCP must name exactly the provider's inventory-only server, whose bytes are pinned to the
reviewed ef73726 SHA-256; changed server code requires an explicit review-loop update. Settings
may contain only the per-request generation environment entry, in the provider's private
request directory. This protects against provider launch-contract drift, not intentionally
malicious Python plugin code (installed plugins remain trusted host code).

A refusal is a failed inference (HTTP 502); selftest names the lockdown in its remediation.
The per-call timeout is 120 seconds, including long reasoning; exceeding it cancels the call.

Tests exercise every required flag omission through the actual helper with a fake provider
and a recording native executable; none starts a process. Recorded pinned argv passes,
and unsafe additions, changed values, duplicates, malicious MCP/settings and server-code drift
are rejected. The no-guard sensitivity control starts the same unsafe recording executable.
The installed ef73726 provider's real generated argv also passed an offline interception at
Popen; no native process or model call was started. Earlier live receipts precede this guard.

Review-fix validation: targeted pytest **52 passed, 64 subtests passed**; independent guard
unittest **6 passed**; canonical harness **1765/1765 checks passed**. Full offline pytest:
**1180 passed, 18 skipped, 752 subtests passed, 1 failed**, the same previously base-reproduced
`test_leakguard_children.py:278` PYTHONPATH fixture failure. The first foreground full-suite
attempt timed out at 420 seconds; the notified background rerun completed in 511.09 seconds.
Independent review found no blocker within the trusted-plugin launch-contract-drift scope.
Non-blocking native-login isolation probes and installed-version diagnostics are tracked in #207.

Published for parent review as PR #204; deployment and merge are not authorized by these receipts.
