# DirectSDK backend validation and remaining scope

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

This branch is for parent review, not deployment authorization. No push or PR was performed.
