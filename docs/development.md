# Development and testing

[Documentation index](README.md) · [Architecture](architecture.md) · [Security](security.md) · [Troubleshooting](troubleshooting.md)

## Contents

- [Working on the plugin](#working-on-the-plugin)
- [Offline test lanes](#offline-test-lanes)
- [Installed-mode checks](#installed-mode-checks)
- [Real-Hermes verticals with local fakes](#real-hermes-verticals-with-local-fakes)
- [Live operational verification](#live-operational-verification)
- [CI and scanner policy](#ci-and-scanner-policy)
- [Change-specific source and test map](#change-specific-source-and-test-map)
- [Reporting results](#reporting-results)

## Working on the plugin

The plugin and its standalone tests use Python's standard library. There is no plugin `pip install`, pytest dependency or fabricated build system required for the basic suite. Run commands below from the plugin checkout root with a Python interpreter available as `python`; Git is required for fixture repositories. CI tests Python 3.11 and 3.14, rather than claiming every Python/platform combination is covered.

The code under `diaktoros/` implements the host control plane; `scripts/` supplies route/gate/watchdog entry points; root `__init__.py` integrates with Hermes. The reviewed repository is not the plugin checkout. Production contained turns export **committed Hermes source**, so editing that source worktree without committing does not change the source snapshot a turn runs. Do not modify real accounts/tokens or armed routes merely to run unit tests.

Before editing a boundary, read its implementation and the targeted tests in the [source map](#change-specific-source-and-test-map). For an integration change, inspect the pinned Hermes checkout too: the plugin's parser, provider or gateway assumptions depend on that version. Consult [Hermes documentation](https://hermes-agent.nousresearch.com/docs) for Hermes setup; use this repository's CI as the reference for its tested integration recipe.

## Offline test lanes

“Offline” here means no live GitHub/model service or real credential is required during the test run. It does not mean fixture tests create no sockets/processes: local HTTP servers, Unix sockets, SQLite files and disposable Git repositories are deliberately exercised. Installing Python, Git, bubblewrap, Hermes dependencies or Rust can itself require network access beforehand.

### Compile and behavioral harness

```sh
python -m compileall -q diaktoros scripts __init__.py tests
python tests/run_tests.py
python tests/run_tests.py --list
python tests/run_tests.py watchdog
```

- `-m compileall` runs Python's module; `-q` suppresses normal per-file chatter. The trailing paths identify plugin modules, entry scripts, integration entry point and tests. Compilation is syntax evidence, not execution coverage.
- `tests/run_tests.py` runs all registered harness groups when no selector is supplied. It uses stubbed GitHub state, a local webhook server and real disposable Git operations.
- `--list` lists area/group selectors without executing them.
- `watchdog` is a positional area selector, not a flag or live watchdog launch. Other area/group names must come from `--list`.
- `python3 tests/affected.py PATH…` lists the test modules a change depends on; `--run` runs them one module at a time and `--budget SECONDS` (default 360) stops starting new ones past that budget, naming what it skipped. This repository's own `fixer_check` is `python3 tests/affected.py --run $CHANGED` (#362), so a fixer turn runs the dependents of its change while CI still runs everything.

The harness also validates fenced `hermes dk` command examples in README/docs/skill against the CLI parser. Keep commands valid and explain every option next to the example; parser acceptance alone does not verify operational effects.

### Desktop component behavior

```sh
node --experimental-vm-modules --test tests/desktop_behavior.mjs
# Required real-TanStack lane, with only its isolated locked test dependency:
npm ci --ignore-scripts --no-audit --no-fund --prefix tests/query-core
python tests/run_desktop_query_core.py
python tests/leakguard.py -v test_desktop_query_core_runner
node --input-type=module --check < desktop/plugin.js
python tests/leakguard.py discover -v -s tests -p 'test_desktop.py'
python tests/leakguard.py -v test_desktop_reads   # needs FastAPI/HTTPX (and Hermes for the mount case)
```

The required `verticals` core job runs the **entire** Node suite through
`tests/run_desktop_query_core.py` on Node 24. `tests/query-core/package-lock.json`
locks only `@tanstack/query-core==5.101.2`, matching CI's `HERMES_PINNED` desktop
lock; `npm ci --ignore-scripts` installs it without lifecycle scripts or the
Hermes frontend/Electron dependency tree. Run the same two commands above from
the plugin root in a disposable development checkout. Installation needs registry
access; execution uses local fixtures only. The runner sets the ESM entry itself,
ignores ambient package/plugin overrides and Node injection options, and requires
205 executed passes (192 fixture + 13 named real-cache cases), zero skips/TODOs,
and matching TAP plan/summary counts. A mounted-page SDK adapter connects the
production Health/useHealth call directly to QueryObserver subscriptions: concurrent
pages, fresh remounts, loop selection, fallback and Retry run without injecting a
health result or extracting options to launch reads manually. Other SDK/React hooks
remain synthetic; this is not a React DOM or Electron test. Missing packages, omitted
real cases or count drift fail; update the explicit count deliberately when adding cases. The runner's
unit tests use clearly synthetic TAP parser fixtures, not component-run evidence.
The additive ledger hold contract, its vocabulary and the remaining #674 integration
work are documented in [Shared hold explanations](hold-explanations.md).
The dependency-free command remains available for quick local checks, but alone
is not required-lane acceptance.

The Node harness executes the shipped plugin's registered page and chip with explicitly synthetic SDK/query fixtures. It tests loading, unavailable, stale cached data, successful empty reads, retry callbacks, summaries, and notifications; it is not a live backend or a browser layout test. Its independent elapsed-timer and wall clocks fire component expiry timers and hook-driven renders without a manual rerender, including a hung refresh, backward wall-clock changes, timestamp replacement, and unmount cleanup. It also checks malformed nested responses, safe error categories, and a missing-ledger payload pinned from real `stats.collect` output (the Python test verifies the same fixture against the collector and Desktop endpoint). `test_desktop.py` also runs this harness when Node is available. The VM module flag is required by this dependency-free harness, not by the Desktop plugin itself.

The page and chip expire `/now` reads at 60 seconds and loop-discovery reads at 120 seconds; the page expires `/stats` reads at 120 seconds. Failed refreshes mark the corresponding read stale immediately. Component-scoped React effects schedule the freshness boundary and cancel their timers on replacement or unmount; they are not a second polling loop. Status labels and dependent empty/setup claims expire together even when polling pauses or a refresh hangs. Elapsed expiry stays sticky for that successful read's data identity and cache timestamp, regardless of backward wall-clock changes; a new successful read resets it. Cached values stay visible with their last successful query-cache timestamp and an explicit stale label; first-load failures show no invented zero. Successful empty reads mean only that this backend returned an empty ledger view, not that GitHub or a fleet was checked. A successful `/stats` payload with `turns: null` instead means no run ledger: the page explicitly shows unavailable counts, never a malformed-response failure or verified zero. Nested counts, run rows, seat turns, duration summaries, and pacing entries are validated before caching; malformed reads are failures, not verified zeros. Retry refetches the relevant read only. The page's one write (#371) is exercised the same way: the Retry control appears only on a row whose backend `retry` verdict is eligible, a click opens the confirmation with the loop/PR/seat/head it would re-arm, cancel sends nothing, confirm posts that exact target, the answer shows the host's line and the read-back state, a 409 shows the host's reason and keeps the dialog open, and one confirm is one request. Those tests drive the registered page against a recorded `rest` seam, not a live backend. The chip shows its last good timestamp and a bounded safe error category in its tooltip, with a separate retry action on failures. Neither the page nor tooltips repeat arbitrary transport error messages.

The health regression cases execute the registered component's query function with
403, transport, and 503 probe failures followed by a separately validated offline
response. They assert local checks remain visible, the caller/unreachable disclosure
is distinct, wrong-loop/malformed/offline=false fallback payloads are rejected, and
successful probes require offline=false. Real-cache mode also verifies concurrent
observers and fresh remounts share exactly one probe plus one fallback; explicit
Refresh GitHub returns to the selected probe endpoint and clears fallback provenance.
Both-read failures retain stale data and safe errors; Retry reissues that same
bounded pair. These cases are fixture evidence, not live GitHub availability evidence.

Native Desktop render verification is still required: light/dark themes, narrow layouts, chip tooltip/action, and hot reload. Unified-package Desktop code is copied at boot or Settings → Capabilities → Plugins → Rescan, not read directly from the package source. A loop whose config file does not load is named by `/loops` and `/overview` (`unreadable: true`, empty `repo`, a bounded `reason`) and renders as `config unreadable` (fix the file), distinct from `not read` (a configured loop this read did not name: wait or retry); the Node harness covers both, but the native render of that state is not yet checked.

### Socket invalidation (#647)

The plugin registers one SDK `ctx.socket('/events', ...)` subscription, shared by
page, pane and chip through the app query cache. The authenticated backend sends
only `{"event":"invalidate","version":1}` (including once on each connection).
It never sends replacement view data: the existing validated REST readers fetch
again. Only server-query namespaces are invalidated; the client-only focus key
is untouched. SDK reconnect/backoff and profile switching remain host-owned.

While at least one socket is connected, one scanner per app/home samples file
metadata every second: ledger and WAL (not reader-mutated SHM), loop configs,
state roots, pacing/runtime, hooks and host config. This detects workers outside
the dashboard without requiring them to publish events. Config JSON is bounded
and read only to discover custom state roots; token files are never opened.
Scans cap entries at 4096 and config bytes at 1 MiB; overflow conservatively
invalidates each interval. A socket has at most one pending hint. The last socket
leaving cancels and joins the scanner, including in-flight thread IO; server
cancellation also joins socket tasks. Unavailable metadata cannot establish
view availability: REST and existing stale/unavailable states remain decisive.

Polling is always retained: now/overview/hooks every 30 seconds and stats every
60 seconds, with other views retaining their existing on-demand refresh behavior.
OAuth, absent/no-op socket transports (the supported SDK always provides `ctx.socket`), refused auth, host-isolated plugin APIs
(which currently lack WebSocket forwarding, including secondary profiles), and disconnects degrade to
existing polling/on-demand reads. There is no new realtime-freshness guarantee.
The new backend route requires a future authorized backend restart to deploy;
no live restart or install is part of development acceptance.

`test_desktop_events.py` exercises real temporary SQLite WAL/state changes and
FastAPI WebSocket admission/disconnect/cancellation. The Node behavior suite
also includes real TanStack QueryClient/QueryObserver coverage: shared page/pane
REST refresh, missed-change repair on reconnect, polling fallback and cleanup.
The required core lane supplies the locked ESM entry and rejects omitted or skipped
real-cache cases. The direct dependency-free command omits those cases. Neither
fixture suite establishes full Electron/native-grant integration.

### Boundary and regression suite

```sh
python tests/leakguard.py discover -v -s tests -p 'test_*.py'
python tests/leakguard.py discover -v -s tests -p 'test_safe_push.py'
```

- `tests/leakguard.py` wraps unittest with resource-leak/child-process checks; use it rather than silently dropping the guard.
- `discover` invokes unittest discovery; `-v` names tests and makes skips visible.
- `-s tests` sets the discovery directory. This avoids accidentally importing Hermes's own `tests` package when Hermes is on `PYTHONPATH`.
- `-p 'test_*.py'` runs all matching test modules; the quoted glob is passed to discovery, not expanded by the shell. `-p 'test_safe_push.py'` narrows the run to that exact file.

CI runs this suite in three shards per Python (the `unit (3.x, N/3)` jobs), and the harness alone in `tests (3.x)`. To run one shard as CI does:

```sh
python tests/leakguard.py -v $(python tests/shard.py 3 1)
python tests/shard.py 3 --all
```

- `tests/shard.py 3 1` prints the modules of shard 1 of 3. Every `tests/test_*.py` is in exactly one shard, and `test_shard.py` fails otherwise, so a new test file needs no CI change.
- `--all` prints each shard's file count and weight. The weights in `tests/shard_weights.json` are measured seconds per module, from a run with `--durations 0`. Refresh them when one shard's CI time drifts well past the others; a stale weight only unbalances the split, it never drops a test.
- A loop that gates on this repository's CI must list the `unit` jobs, `installed-mode (resolver)` and `verticals (seats)` in `required_checks` (`hermes dk set --loop <id> --required-check NAME`, once per check; it replaces the list), or a red shard won't hold a review.

Some sandbox tests skip without usable bubblewrap/user namespaces; real-Hermes tests can skip without source/venv/Rust prerequisites. A green standalone result with skips is not a complete integration result. Install bubblewrap using the host's package manager and check the host user-namespace policy before treating sandbox coverage as exercised. CI's Ubuntu sysctl adjustments are runner preparation, not instructions to disable a production security policy.

The test guards in `tests/_home_guard.py`, `tests/_ledger_guard.py` and `tests/leakguard.py` isolate HOME/state, refuse the real ledger and detect resource leaks. Preserve their bootstrap ordering when adding tests. Fake network/token seams belong in tests; they must not become a production bypass.

## Installed-mode checks

Some behavior changes when Hermes is importable: model resolution and stored cron-expression checks use actual Hermes code. CI therefore repeats the harness with the pinned Hermes checkout on the interpreter path. This lane is **not** a full native Hermes install: its Python 3.14 job installs only the pinned resolver prerequisites, `croniter==6.0.0` and `ruamel.yaml==0.18.16`.

Prepare a Hermes checkout at the `HERMES_PINNED` commit in [CI](../.github/workflows/ci.yml), then use this lane from the plugin root:

```sh
python -m pip install 'croniter==6.0.0' 'ruamel.yaml==0.18.16'
export HERMES_AGENT_SOURCE="<absolute-hermes-source-path>"
export PYTHONPATH="$HERMES_AGENT_SOURCE"
export DIAKTOROS_REQUIRE_HERMES_SOURCE=1
python -c "import hermes_cli; print(hermes_cli.__file__)"
python tests/run_tests.py
python tests/leakguard.py discover -v -s tests -p 'test_gate_shims.py'
```

- `-m pip install` installs the two explicitly pinned packages into the selected interpreter; do this in a disposable development environment. Downloading them is not an offline step.
- `HERMES_AGENT_SOURCE` names the prepared Hermes Git checkout, not the plugin or a real profile directory. Replace the quoted angle-bracket placeholder with its absolute path.
- `PYTHONPATH` makes that checkout importable to the parent interpreter; preserve any other needed paths deliberately rather than assuming this example appends them.
- `DIAKTOROS_REQUIRE_HERMES_SOURCE=1` converts supported missing-prerequisite skips into failures. It does not create the missing runtime or eliminate every possible skip.
- `-c` executes the inline Python import check; printing its path proves which Hermes module is importable.
- The final discovery pattern selects gateway shim/resolver integration tests only; other flags mean the same as in the boundary lane.

Do not claim the standalone harness exercised installed-mode behavior when Hermes was absent. Do not claim these resolver prerequisites are all the dependencies for a sandboxed Hermes turn.

## Real-Hermes verticals with local fakes

The vertical lane runs actual Hermes/tool dispatch inside bubblewrap, but uses local fake GitHub/model endpoints and fake tokens. It does **not** require a real GitHub token, API key, OAuth login or native Claude subscription.

Required preparation, matching the [CI workflow](../.github/workflows/ci.yml):

1. Linux with working `bwrap --unshare-all` and permitted unprivileged user namespaces. CI tests that ability explicitly.
2. A Hermes checkout at CI's `HERMES_PINNED` commit, with `venv/bin/hermes` executable.
3. The venv interpreter must be uv-managed CPython in its own runtime directory: containment mounts that directory, so a shared system-runtime directory is not an equivalent substitute.
4. The Python version must come from that checkout's `pm/lock.json`; CI uses uv 0.12.19 to create `venv` and sync the lock with the `anthropic` extra. Installing this environment may require network and native build prerequisites.
5. A stable Rust toolchain at `$HOME/.rustup/toolchains/stable-x86_64-unknown-linux-gnu/bin/cargo`, the path the fixtures check. This fixture assumption is not a claim of tested architecture portability.

Inside the prepared Hermes checkout, CI's dependency sync is equivalent to:

```sh
UV_PROJECT_ENVIRONMENT="$HERMES_AGENT_SOURCE/venv" uv sync --frozen --no-dev --extra anthropic
```

- `HERMES_AGENT_SOURCE` must already point to this checkout, and its `venv` must already have been created with the Python version from `pm/lock.json` and `UV_PYTHON_PREFERENCE=only-managed`.
- `UV_PROJECT_ENVIRONMENT` selects the explicit venv, rather than an ambient environment.
- `uv sync` installs the project's locked dependencies; `--frozen` avoids updating the lock; `--no-dev` omits development dependencies; `--extra anthropic` installs Hermes's native Messages SDK extra. This is environment bootstrap, not an offline test command.

Return to the plugin root and execute the CI-selected vertical tests:

```sh
export HERMES_AGENT_SOURCE="<absolute-hermes-source-path>"
export DIAKTOROS_REQUIRE_HERMES_SOURCE=1
export PYTHONPATH="$PWD/tests"
python tests/leakguard.py -v \
  tests.test_route_vertical \
  tests.test_turn_vertical \
  tests.test_route_worker_vertical \
  tests.test_contained_agent \
  tests.test_inference_proxy \
  tests.test_oauth_seats \
  tests.test_seat_models.HermesAgreement
```

- The source placeholder is the prepared Hermes checkout. `DIAKTOROS_REQUIRE_HERMES_SOURCE` makes known prerequisite failures explicit, as above.
- `PYTHONPATH="$PWD/tests"` makes the test bootstrap modules available; it is intentionally different from the installed-mode lane. Run from the plugin root so `$PWD` is correct.
- `-v` is verbose unittest output. The remaining arguments are explicit unittest module/class selectors; `HermesAgreement` selects the real-resolver agreement class.
- The backslashes continue one shell command; they are not unittest options.

Require the `leak guard armed:` line and inspect the complete output for skips. CI refuses any skipped test in this lane in addition to enabling skip-or-fail behavior. Local success with missing vertical prerequisites should be reported as skipped coverage, not success of the contained path.

DirectSDK tests are separate offline adapter/guard tests using disposable plugin/native fixtures. Passing them does not prove a live native subscription round trip, and the vertical selection above is not a DirectSDK live-login lane.

## Live operational verification

Use live checks only with operator authorization for the selected loop/account. They are not the offline CI suite. See [accounts](accounts.md), [configuration](configuration.md), [operations](operations.md) and [troubleshooting](troubleshooting.md) for exact setup/command syntax.

Live prerequisites include correctly mapped distinct GitHub identities/token files with the needed repository permissions, a reachable configured webhook/gateway for delivery checks, resolved seat model authentication/provider extras, valid runtime source/venv/toolchain paths, and the loop's configured hooks/policy. DirectSDK additionally needs the installed experimental provider, matching guarded inventory code and a usable host-side native CLI/login/config directory. None of those credentials should be added to the test fixtures.

`doctor` is a prerequisite/configuration diagnostic, not a proof that a complete turn wrote successfully; its `--offline` option skips external probes. `selftest` is a live check, not an offline suite: `--no-model` skips inference but still performs real GitHub reads, and it rejects the GitHub stub seam. Its `--live-turn` option requires `--pr` and model inference; the reviewer exercise uses a host-only no-write broker to record a proposed verdict in host memory without posting a review. Its optional `--ping` deliberately performs a GitHub hook-ping write outside the read-only guard. These probes are not proof of a production review POST or a successful fixer push. Read the actual probe results, especially skipped/partial checks, instead of inferring capability from a single overall status.

Enabling unattended fixer push is a separate policy decision with a documented PR metadata race. Testing a no-write reviewer does not authorize it. For any deliberately authorized live write, verify the exact resulting review/comment/ref and host receipt/ledger state before calling it successful. Never run ambiguous writes again solely because the first response was lost.

## CI and scanner policy

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) is the executable reference. It defines:

| Job | What it actually exercises |
|---|---|
| `tests` | Python 3.11/3.14 syntax and behavioral harness; installs bubblewrap before full leak-guarded discovery; scheduled runs repeat boundary discovery |
| `unit` | The unit suite sharded 3 ways across Python 3.11/3.14, so one slow test file does not serialize the lane |
| `plugin-guard` | The Hermes install scanner at the pinned version; also Hermes `main` on scheduled/manual runs |
| `plugin-validate` | Hermes's own catalog validation action against the manifest and the pinned Hermes checkout |
| `installed-mode` | Two parts: the harness with the pinned Hermes importable (model/cron resolver checks and gateway shims), and the `installed-mode (resolver)` tests, both on Python 3.14 |
| `verticals` | Prepared real Hermes/uv runtime/Rust/bubblewrap with local fakes, required prerequisites and no-skips enforcement |

CI runs for main-branch pushes, pull requests, its weekly schedule and manual dispatch. Use the current workflow's pin and dependency recipe rather than assuming the moving upstream `main` is the supported runtime.

The scanner wrapper can be run from the plugin checkout:

```sh
python .github/scripts/./plugin_guard.py "<absolute-hermes-source-path>" .
```

- The first positional argument supplies Hermes's scanner source (its `tools/` package is needed), not model credentials. Use the desired pinned/prepared checkout.
- `.` is the plugin directory to scan from the current checkout root.
- The script prints the real report and uses `should_allow_plugin_install(..., force=True)`: `dangerous` blocks, while `caution` can pass with findings printed for review. It does not install the plugin.

A scanner result is version- and input-dependent. Do not document an invented fixed finding count, describe passing `caution` as “no findings,” or treat the scan as an independent security audit.

## Change-specific source and test map

| Change | Read first | Targeted tests |
|---|---|---|
| Mounts, environment, resource bounds | [`contained.py`](../diaktoros/contained.py), [`trusted_turn.py`](../diaktoros/trusted_turn.py) | `test_boundary.py`, `test_contained_launch.py`, `test_sandbox_identity.py`, `test_sandbox_limits.py`, `test_turn_budget_sandbox.py` |
| Committed source/PR exports | [`trusted_turn.py`](../diaktoros/trusted_turn.py), [`trusted_fetch.py`](../diaktoros/trusted_fetch.py) | `test_snapshot_secrets.py`, `test_trusted_fetch.py`, `test_reader_identity.py` |
| Credentialed publication | [`broker.py`](../diaktoros/broker.py), [`broker_ipc.py`](../diaktoros/broker_ipc.py), [`safe_push.py`](../diaktoros/safe_push.py), [`review_receipt.py`](../diaktoros/review_receipt.py) | `test_broker_ipc.py`, `test_safe_push.py`, `test_push_policy_boundaries.py`, `test_post_write_quarantine.py`, `test_review_receipt.py`, `test_partial_view_no_approve.py` |
| HTTP inference/native adapter | [`inference_proxy.py`](../diaktoros/inference_proxy.py), [`directsdk_backend.py`](../diaktoros/directsdk_backend.py), [`directsdk_child.py`](../diaktoros/directsdk_child.py), [`directsdk_guard.py`](../diaktoros/directsdk_guard.py) | `test_inference_proxy.py`, `test_oauth_seats.py`, `test_directsdk_backend.py`, `test_directsdk_child.py`, `test_directsdk_guard.py` |
| Claims/recovery/state handles | [`run_supervisor/`](../diaktoros/run_supervisor/__init__.py), [`ledger.py`](../diaktoros/ledger.py) | `test_run_supervisor.py`, `test_retryable_turns.py`, `test_retry_live_route.py`, `test_ledger_close.py`, `test_home_guard.py` |
| Routes/gates/integration | [`routes.py`](../diaktoros/routes.py), [`route_intent.py`](../diaktoros/route_intent.py), [`gate.py`](../diaktoros/gate.py), [`gate_shims.py`](../diaktoros/gate_shims.py) | `test_routes_atomic.py`, `test_route_self_heal.py`, `test_gate_shims.py`, `test_route_vertical.py`, `test_route_worker_vertical.py` |
| Dependency fetch policy | [`deps.py`](../diaktoros/deps.py) | `test_deps.py` |
| Operator CLI commands | [`cli/__init__.py`](../diaktoros/cli/__init__.py) (`register_cli`: every command, in help order; each module's `add_<verb>` builds that command's parser beside it), [`cli/settings.py`](../diaktoros/cli/settings.py) (`set`, `apply`, `show`, `settings`, `triage`, `fixer-push`), [`cli/install.py`](../diaktoros/cli/install.py) (`init`, `setup`), [`cli/checks.py`](../diaktoros/cli/checks.py) (`doctor`, `selftest`, `corpus`, `arm`), [`cli/lifecycle.py`](../diaktoros/cli/lifecycle.py) (`migrate`, `backup`, `restore`, `uninstall`), [`cli/operations.py`](../diaktoros/cli/operations.py) (`status`, `list`, `retry`, `explain`, `ready`, `review`, `escalate`, `regression`, `depends`, `drain`, `cleanup`), [`cli/reports.py`](../diaktoros/cli/reports.py) (`trace`, `stats`, `report`, `models`), [`cli/common.py`](../diaktoros/cli/common.py) (what several groups share: routes, webhooks, the watchdog's cron job and shim, the loop-file write, credential lines). `cli` re-exports every moved `cmd_*` and calls shared helpers as `cli_common.<name>`, so patch them there (#676) | `test_cli_modules.py`, `test_commands_doc.py`, then the command's own tests (`test_stats.py`, `test_trace.py`, `test_seat_models.py`, `test_review_command.py`, `test_retryable_turns.py`, `test_escalate.py`, `test_issue_deps.py`) |
| Desktop one-shot actions (#644) | [`one_shot_action.py`](../diaktoros/one_shot_action.py) (shared selector/operation; review/drain Desktop refusal), [`desktop_api.py`](../diaktoros/desktop_api.py), [`dashboard/plugin_api.py`](../dashboard/plugin_api.py), [`desktop/plugin.js`](../desktop/plugin.js) | `test_one_shot_actions.py`, `test_desktop_actions.py`, `test_review_command.py`, `test_escalate.py`, route guards and `node --experimental-vm-modules --test-reporter=tap --test tests/desktop_behavior.mjs` |
| Desktop hook pause/resume | [`arm_action.py`](../diaktoros/arm_action.py) (shared CLI/route owner), [`desktop_api.py`](../diaktoros/desktop_api.py), [`dashboard/plugin_api.py`](../dashboard/plugin_api.py), [`desktop/plugin.js`](../desktop/plugin.js) | `test_desktop_hooks.py`, `test_arm_verify.py`, `test_desktop.py` route guard; `node --experimental-vm-modules --test tests/desktop_behavior.mjs` |
| Documentation/CLI examples | [`tests/commands_doc.py`](../tests/commands_doc.py), [`tests/harness/docs.py`](../tests/harness/docs.py) | `test_commands_doc.py`, behavioral harness `docs` area |

Test names in this table refer to files beneath [`tests/`](../tests/). Use discovery's `-p` pattern for a targeted file, then rerun the full relevant lane. A targeted regression alone does not prove unrelated admission, recovery and integration paths stayed intact.

## Reporting results

For a change, report the exact commands, interpreter/Hermes version or pin, pass/fail/skip results and prerequisite limitations from real output. Separate compilation, standalone tests, installed-mode checks, contained local-fake verticals, scanner verdict and authorized live probes. Include untested provider paths and partial/no-write outcomes. Do not invent successful CI runs, external writes, infrastructure controls or coverage counts.
