# Native macOS experiment

This branch starts issue #256's native alternative with a standalone Seatbelt
profile builder, a native staged-Hermes launcher and executable boundary probes. It does **not** enable native
production Hermes turns. `contained.py`, setup, doctor and the manifest retain
the existing Linux requirement.

The host generates a default-deny profile, passes filesystem paths as parameters,
and launches through `/usr/bin/sandbox-exec`. The profile permits narrow runtime
and disposable working directories, system executable/library reads, and exact
per-turn Unix socket connections. It grants no TCP/UDP, socket binding, general
Mach-service lookup, Apple Events or Launch Services authority. macOS system
paths are canonicalized before use. The host chooses these paths; this builder
must never accept a PR-supplied access list.

## First acceptance gate

The dedicated workflow runs on pinned macOS 15 Apple Silicon and Intel images.
The initial 11 profile/boundary tests passed on both architectures in
[run 38066847221](https://github.com/coe0718/diaktoros/actions/runs/38066847221).
`DIAKTOROS_REQUIRE_SEATBELT=1` makes missing macOS prerequisites a failure rather
than a skipped success. No GitHub/model credentials or live services are used.

```sh
python tests/leakguard.py discover -v -s tests -p 'test_seatbelt.py'
```

The probes exercise Python startup, a scrubbed child environment, writable
scratch, read-only source, denied host-secret access, symlink escapes, descendant
inheritance, TCP/UDP denial, exact socket access and socket replacement denial.
Linux runs validate profile construction and fail-closed launch behavior; the
native probes skip there. Linux results do not prove macOS containment.

## Native Hermes vertical

The additional `hermes-turn` job installs the same pinned Hermes commit used by
Linux CI into a disposable native venv. It stages committed source for its
fixture and starts the real Hermes CLI under Seatbelt. A model fixture requests
a terminal command that verifies host-secret/network denial and submits a review
to a real scoped broker backed by fake GitHub responses. The test verifies model
authentication stays host-side and exactly one scoped fake review is posted.
The Python-only Hermes fixture passed on both architectures in
[run 38068669256](https://github.com/coe0718/diaktoros/actions/runs/38068669256).

The next fixture also runs `cargo test --offline --locked --lib` using Rust
1.85.1 and a dedicated read-only vendor snapshot of memchr 2.7.4. A generated
Cargo build script verifies host-secret/network denial and vendor-write denial;
the test verifies an actual dependency rlib and a passing Rust unit test. The
host resolves the selected Apple SDK and compiler toolchain before launch.
Only those SDK/toolchain roots are granted, not all of Xcode, `/Applications`,
Homebrew or the operator's Cargo profile. Cargo gets a fresh scratch home and
direct compiler/linker paths; dependency downloads happen before containment.

`seatbelt_wire.py` supplies an OpenAI client with an httpx Unix-domain transport
through Hermes's provider hook. There is no localhost listener or TCP allowance;
only Chat Completions is supported in this first native vertical. Host inference
capability quotas, byte limits, upstream selection and credential handling remain
in force. `broker_client.py` accepts portable path settings from the rebuilt
child environment; these do not change broker authority.

`native_macos.run` remains experimental and is not selected by production code.
It uses the shared bounded output/time capture and requires a host-created
fixed-capacity workspace. It provides no supervisor-death guarantee or
detached-child cleanup. The vertical's source
export is a trusted fixture, not the portable production snapshot implementation.
The Rust fixture covers a small pure-Rust vendored dependency and Apple's
linker/SDK, not arbitrary workspaces, C/C++ dependencies or durable production
review receipts. Do not interpret this as completed native support.

## Bounded native storage

`native_storage.Workspace` creates a fixed-size UDRW image containing a
case-sensitive APFS filesystem. The host attaches it at a private mountpoint and
verifies image identity, mounted filesystem identity, capacity and case-sensitive
names before launch. All three writable roots (`home`, `work`, `scratch`) share
that filesystem allocation budget. A missing/incorrect mount fails closed.
The backing image, disk devices and capability sockets are outside the child's
filesystem authority; the child cannot resize the image through file access.

The dedicated storage probes fill a small volume until the kernel returns
`ENOSPC`, verify the shared limit across all writable roots, denied image/symlink
writes and unchanged backing-image size, and exercise cleanup after an exception.
The real Hermes/Rust fixture uses a separate 512 MiB volume. The bound covers
allocated filesystem blocks, not sparse-file logical lengths or total process
memory. Production storage sizing still needs representative workspace tests.

Cleanup rediscovers attached devices by the exact private image, detaches, and
verifies removal before deleting the backing files. If detach/verification fails,
it retains the private image directory and raises an error for host recovery.
This is not cleanup after host death: startup reconciliation, detached descendant
termination and lifecycle-safe disposal remain production gates.

## Lifecycle characterization

`tests/test_native_lifecycle.py` deliberately measures two unresolved gaps in
`contained.capture`: a child that calls `setsid()` can survive process-group
cleanup after timeout, and a child can survive `SIGKILL` of its host supervisor.
The probes demand fresh filesystem activity after failure and verify that the
survivor still cannot read a host secret. A green characterization test confirms
these limitations; it is **not** production lifecycle acceptance.

The fixtures are cooperative and time-bounded. Before triggering failure, the
host registers `kqueue` process-exit notifications and waits for actual exit
before disposing of fixture paths. No general process-tree polling/killing
mechanism is introduced. Production needs an enforceable descendant ownership
mechanism and independent host-death recovery, including capability revocation
and safe handling of attached storage. Apple launchd's process-group cleanup
alone does not establish ownership of a descendant that changes its group.

## Remaining work before production support

1. Add a portable per-turn layout and backend selection. Generate entry points,
   Hermes configuration, prompts and broker-client paths from that layout; remove
   Linux `/opt`, `/work` and `/proc/self/fd` assumptions without weakening pinned
   source-snapshot race protections.
2. Replace the fixed localhost inference bridge. Prefer direct Unix-socket HTTP
   transports where Hermes provider clients permit them. Any TCP alternative
   needs exclusive per-turn ports, authentication and exact endpoint permissions.
3. Extend native Rust/SDK/offline dependency coverage to representative workspaces
   and native build dependencies against local model and broker fixtures. Do not allow all of Homebrew,
   the user's home or `/Library` to solve missing-runtime failures.
4. Validate bounded-volume sizing with representative workspaces and integrate
   crash recovery/reconciliation. Seatbelt alone supplies no sized tmpfs or disk
   quota; a directory-size watcher is not an equivalent bound.
5. Design and test supervisor death and detached descendant cleanup. Process
   groups and profile inheritance alone do not reproduce bubblewrap's PID
   namespace and parent-death semantics.
6. Extend doctor/selftest and verify full install/review operation with a Mac
   tester. Keep production support experimental until the acceptance gate passes.

`sandbox-exec` is deprecated. Its presence and effective restrictions must be
tested on every supported OS rather than inferred from the OS name. This
experiment is not App Sandbox packaging or an Apple-supported stable SBPL API.

References: [Codex's Seatbelt implementation](https://github.com/openai/codex/tree/main/codex-rs/sandboxing/src),
[Anthropic's macOS sandbox implementation](https://github.com/anthropics/sandbox-runtime/blob/main/src/sandbox/macos-sandbox-utils.ts).
