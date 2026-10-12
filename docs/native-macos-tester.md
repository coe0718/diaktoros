# Native Mac tester: first hardware pass

Use a separate checkout of draft PR #606's `feat/native-macos-seatbelt` branch.
This first pass exercises the experimental Seatbelt boundary, process cleanup,
watchdog death and APFS recovery. Production native Hermes turns are still
disabled. The commands below do not install webhook routes, change your live
Hermes configuration or post GitHub reviews.

## Prepare

Start with macOS 15 on Apple Silicon or Intel, Python 3.11 or newer, and Apple's
Command Line Tools. Check `python3 --version` and `xcrun --sdk macosx --show-sdk-path`.
If the latter is missing, install the tools with `xcode-select --install`, then
finish Apple's installer before proceeding. No administrator setup, per-turn
users or new permanent APFS volume are needed by this experiment.

```sh
git clone --branch feat/native-macos-seatbelt --single-branch https://github.com/coe0718/diaktoros.git diaktoros-native-test
cd diaktoros-native-test
python3 -m venv .venv-native
. .venv-native/bin/activate
mkdir -m 700 "$HOME/diaktoros-native-evidence"
python tests/native_tester.py preflight --output "$HOME/diaktoros-native-evidence/preflight-01"
```

Each output directory must be new. `result.json` records the architecture,
macOS/Python versions and prerequisite results. `commit.log` pins the tested
commit; `working_tree.log` records local edits. A failed preflight prevents all
native suites from running. The runner never prints environment variables and
does not pass inherited API tokens or live plugin configuration to the tests.

## Run the boundary and watchdog checks

```sh
python tests/native_tester.py boundary --output "$HOME/diaktoros-native-evidence/boundary-01"
```

This runs the same nine test suites used for the native boundary in CI, including
session/spawn escape probes, filesystem/network denial, watchdog-death recovery,
durable ownership, queued disposal and the bounded cleanup worker. The existing
test-home guard gives these suites a disposable Hermes home. Tests create small
temporary APFS images; failed cleanup can retain them for verified recovery.
The runner stops at the first failed suite, preserving its log and the result
manifest. Each suite has a four-minute outer timeout.

## Optional 200-turn cleanup soak

Run after the boundary pass succeeds:

```sh
python tests/native_tester.py soak --output "$HOME/diaktoros-native-evidence/soak-01"
```

Allow up to an hour. These are guarded Python command fixtures, including
detached-process attempts and simulated build servers, with 20 injected eject
deferrals. They are not 200 live Hermes model/tool turns. The soak verifies exit
proof, result preservation and disposal within five sweeps/five minutes; it
retains per-turn evidence under `soak/`. No physical reboot is performed.

## Report and recovery

Send the `result.json`, `commit.log`, `working_tree.log` and failed suite's log,
or the soak's result/events. Logs can include local paths; review them before
sharing. Do not include API keys, a live Hermes configuration or image contents.
Keep the evidence directory and retained images until the failure is understood.

For an explicitly reported cleanup pool, inspect it first:

```sh
python -m diaktoros.native_cleanup /exact/reported/private/pool
```

Retry only that owned pool using the bounded worker:

```sh
python -m diaktoros.native_cleanup /exact/reported/private/pool --sweep
```

Never delete a backing image that might still be attached or kill a process from
a diagnostic PID list. Unknown ownership and interrupted setup stay retained.
The bounded worker reports a timeout without claiming disposal and allows a later
sweep to retry. A missing image alone is not process-exit proof.

## Pinned Hermes/Rust tool turn

After the boundary pass, stay in the separate checkout and activated test venv.
This stage needs `rustup` on PATH; check `rustup --version` first. The preparer
does not install rustup globally. Install the CI-pinned uv in the test venv:

```sh
python -m pip install uv==0.12.19
python tests/native_tester.py prepare-hermes --output "$HOME/diaktoros-native-evidence/hermes-preparation-01"
python tests/native_tester.py hermes --prepared "$HOME/diaktoros-native-evidence/hermes-preparation-01/result.json" --output "$HOME/diaktoros-native-evidence/hermes-turn-01"
```

Preparation downloads Hermes at commit
`be5e9f72c6681af9dfb75bf480f08844f1499949`, its locked Python/dependencies, a
dedicated Rust 1.85.1 toolchain and the memchr 2.7.4 vendor fixture. It selects
the installed Apple SDK and compiles the trusted fork/exec adapter. Downloads,
toolchains, caches and the venv live inside the new preparation directory; the
existing Hermes/Rust installs are not used as writable fixtures. Allow roughly
45 minutes and several GB of free space for this stage. Each preparation step
has a timeout, a log and a recorded result; any failure stops subsequent steps.

The test command verifies the pinned source commit and required staged files
before launching. Missing or failed preparation is an error, not a skipped
success. It launches real Hermes and an offline Rust build under Seatbelt,
using a fake model provider and fake GitHub responses over scoped sockets.
No live provider key or real review is involved. `hermes_model_turns` remains
false in the report because this is not a live model workload. The `hermes-turn`
CI jobs run these exact two tester commands on both architectures.

Share the preparation `result.json` and relevant `.log` files plus the turn's
`result.json`/`native_hermes.log`; the downloaded checkout, toolchain, caches and
image files are not needed as evidence. Keep preparation intact until the turn
finishes, since it supplies read-only runtime inputs.

## Remaining tester stages

The cleanup worker now sends pause/resume transitions to an existing configured
observer as immediate `held` events, using the urgent route when configured.
Messages say `native storage`, contain no invented PR number and link to the
repository. Existing mute/event filters apply; with no observer, notices stay on
the console. The isolated fixture tests do not configure or send live chat notices.
For the later live trial, verify both a pause and its recovery reach the selected
route once. Failed or interrupted delivery stays uncertain in doctor/explain;
reconcile it with the observer receipt before attempting a manual resend.

A live provider/workspace workload, observer pause/resume delivery, physical
reboot with pending images and loss of all completion observers remain gates.
The normal plugin installer and doctor still require Linux; they are not the
entry points for this first Mac experiment.
