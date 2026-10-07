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

The harness also validates fenced `hermes dk` command examples in README/docs/skill against the CLI parser. Keep commands valid and explain every option next to the example; parser acceptance alone does not verify operational effects.

### Boundary and regression suite

```sh
python tests/leakguard.py discover -v -s tests -p 'test_*.py'
python tests/leakguard.py discover -v -s tests -p 'test_safe_push.py'
```

- `tests/leakguard.py` wraps unittest with resource-leak/child-process checks; use it rather than silently dropping the guard.
- `discover` invokes unittest discovery; `-v` names tests and makes skips visible.
- `-s tests` sets the discovery directory. This avoids accidentally importing Hermes's own `tests` package when Hermes is on `PYTHONPATH`.
- `-p 'test_*.py'` runs all matching test modules; the quoted glob is passed to discovery, not expanded by the shell. `-p 'test_safe_push.py'` narrows the run to that exact file.

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
| `plugin-guard` | The Hermes install scanner at the pinned version; also Hermes `main` on scheduled/manual runs |
| `installed-mode` | Pinned Hermes importability, model/cron resolver harness and gateway shim tests on Python 3.14 |
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
| Claims/recovery/state handles | [`run_supervisor.py`](../diaktoros/run_supervisor.py), [`ledger.py`](../diaktoros/ledger.py) | `test_run_supervisor.py`, `test_retryable_turns.py`, `test_retry_live_route.py`, `test_ledger_close.py`, `test_home_guard.py` |
| Routes/gates/integration | [`routes.py`](../diaktoros/routes.py), [`route_intent.py`](../diaktoros/route_intent.py), [`gate.py`](../diaktoros/gate.py), [`gate_shims.py`](../diaktoros/gate_shims.py) | `test_routes_atomic.py`, `test_route_self_heal.py`, `test_gate_shims.py`, `test_route_vertical.py`, `test_route_worker_vertical.py` |
| Dependency fetch policy | [`deps.py`](../diaktoros/deps.py) | `test_deps.py` |
| Documentation/CLI examples | [`tests/commands_doc.py`](../tests/commands_doc.py), [`tests/harness/docs.py`](../tests/harness/docs.py) | `test_commands_doc.py`, behavioral harness `docs` area |

Test names in this table refer to files beneath [`tests/`](../tests/). Use discovery's `-p` pattern for a targeted file, then rerun the full relevant lane. A targeted regression alone does not prove unrelated admission, recovery and integration paths stayed intact.

## Reporting results

For a change, report the exact commands, interpreter/Hermes version or pin, pass/fail/skip results and prerequisite limitations from real output. Separate compilation, standalone tests, installed-mode checks, contained local-fake verticals, scanner verdict and authorized live probes. Include untested provider paths and partial/no-write outcomes. Do not invent successful CI runs, external writes, infrastructure controls or coverage counts.
