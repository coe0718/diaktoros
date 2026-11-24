# Experimental native macOS

## Current-main operator guide

Linux remains the supported catalog platform. Native macOS execution is a
**development opt-in, disabled by default**; it is not a production-support claim.
The manifest and catalog remain Linux-only until the acceptance requirements in
[issue #624](https://github.com/coe0718/diaktoros/issues/624) are met.

The implementation uses Seatbelt, fixed-capacity APFS images and independent
process supervision, not a VM or an unsandboxed fallback. It integrates scoped
broker writes, cancellation, dependency inputs and bounded output. Chat
Completions, Responses and Messages use Unix-domain transports. Native DirectSDK
is explicitly refused.

### Opt in deliberately

Use a manually loaded development checkout in an isolated test profile, not a
live unattended installation. Set this in the **host process** running Diaktoros:

```sh
export DIAKTOROS_NATIVE_MACOS=1
```

Only the value `1` enables native execution. Without it, native production turns
are refused before staging or opening model/broker capabilities. Setting it does
not change catalog compatibility, create GitHub identities, arm hooks or establish
acceptance. Do not add it to unrelated profiles or remove platform checks merely
to install this experimental code.

### Prerequisites and runtime roots

Use Python 3.11+, a dedicated Hermes checkout/venv, a native Rust toolchain and
Apple Command Line Tools (`xcode-select --install`). `sandbox-exec`, `hdiutil`,
`diskutil`, `codesign` and the selected SDK/compiler must be available. No root
service, per-turn account or permanent APFS volume is required.

Apple Silicon also needs a plain `arm64` Bash, for example `brew install bash`.
The backend discovers it on `PATH` or at the standard Homebrew locations. It
does not re-sign Apple's `arm64e` system shell or require altered boot security.
Intel can use the system Bash. The private copy stays read-only after signing;
its non-system libraries receive exact-file read grants, not a Homebrew-wide grant.

In a development installation, the normal setup supports `--source`, `--venv`,
`--runtime`, `--rust` and optional `--node` overrides. `--node ""` selects no Node.
Do not reuse a Linux runtime JSON unchanged. Setup writes private 0600 settings.

Choose exact trusted installation generations, not `/opt/homebrew`, `/usr/local`,
your home directory, `.hermes`, SSH/cloud credential stores or all of Xcode.
Framework Python must resolve to its selected runtime and preserve the venv.
Node links must stay within their dedicated installation. Runtime roots are
checked again at launch, including resolved symlinks and optional inputs; editing
a runtime file does not bypass those checks. Do not keep credentials inside any
readable runtime root.

### Verify before arming

Keep hooks paused. Run doctor and selftest only against the intended development
profile and loop. The containment selftest actually exercises native Python/Cargo,
host-secret and network denial, environment isolation and process restrictions.
Missing prerequisites or failed probes are not synthetic passes.

`hermes dk selftest --loop ID --no-model` can still contact GitHub with the host's
configured credentials. It is **not** an offline test command. For credential-free
qualification, use the [native tester](native-macos-tester.md):

```sh
python3 tests/native_tester.py preflight --output /NEW/ABSOLUTE/EVIDENCE/PATH
python3 tests/native_tester.py boundary --output /ANOTHER/NEW/EVIDENCE/PATH
```

Each output path must be new and have an existing canonical parent. The tester
explicitly opts its disposable child processes in. Its `hermes` mode uses prepared,
pinned Hermes/Rust and fake external services through the production entry point;
it does not establish live-provider authorization or a live GitHub workload.
Repeat relevant qualification after changing OS, interpreter, SDK or toolchains.
Intel/Rosetta, physical reboot and live workloads require their own acceptance.

### Storage, cancellation and cleanup

The ledger-adjacent `native-cleanup` pool holds main turns;
`native-proof-cleanup` holds isolated proofs. Each admits one active workspace.
Account for both bounded pools and retained images when provisioning disk space.
An occupied main pool pauses new turns without consuming retry budget. Proofs do
not modify or allocate another slot in the active main workspace.

Runtime roots and the original checkout export are read-only. The working copy,
HOME and scratch live on bounded APFS storage. A private signed shell preserves
the spawn adapter; kernel process restrictions remain mandatory. Model and GitHub
credentials remain host-side behind the scoped sockets.

A busy eject can outlive the agent. Verified process completion with pending
storage is different from unknown process completion. Recovery sweeps only the
two exact owned pools. Inspect a reported pool before requesting a sweep:

```sh
python -m diaktoros.native_cleanup /EXACT/REPORTED/POOL
python -m diaktoros.native_cleanup /EXACT/REPORTED/POOL --sweep
```

A sweep timeout is not success. Keep retained receipts and evidence. Never delete
an image that could be attached, kill a discovered DiskImages helper, or infer
process exit solely from a missing worker PID, directory or backing image.

### Manually reconcile a quarantined run

Automatic native worker-loss handling remains fail-closed. An operator must stop
and independently verify all descendants of the exact run (or reboot the host)
before using the explicit manual reconciliation acknowledgement. A worker PID
being gone is insufficient. The CLI acknowledgement is an operator attestation,
not automatic proof from the cleanup sweep; it must not be automated.

After that inspection, use the explicit ledger/run command:

```sh
python -m diaktoros.run_supervisor reconcile /EXACT/LEDGER RUN_ID \
  --reason "external writes inspected; all descendants stopped" \
  --acknowledge-no-live-worker \
  --acknowledge-native-descendants-stopped
```

Both acknowledgements are required. Reconciliation is audited and still refuses
a live or unverified worker. It releases the claim without replaying an ambiguous
write; ordinary retries remain blocked until the operator resolves that outcome.

## Historical evidence

The original experiments, soak results and forensics history belong to
[PR #606](https://github.com/coe0718/diaktoros/pull/606) and
[issue #624](https://github.com/coe0718/diaktoros/issues/624). They are not current
production acceptance. Natural-eject, forensics and long soak workflows are
manual evidence runs; an inconclusive run remains inconclusive.
