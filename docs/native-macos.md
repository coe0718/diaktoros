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

`seatbelt_wire.py` supplies an OpenAI client with an httpx Unix-domain transport
through Hermes's provider hook. There is no localhost listener or TCP allowance;
only Chat Completions is supported in this first native vertical. Host inference
capability quotas, byte limits, upstream selection and credential handling remain
in force. `broker_client.py` accepts portable path settings from the rebuilt
child environment; these do not change broker authority.

`native_macos.run` remains experimental and is not selected by production code.
It uses the shared bounded output/time capture, but provides no hard disk quota,
supervisor-death guarantee or detached-child cleanup. The vertical's source
export is a trusted fixture, not the portable production snapshot implementation.
It tests a terminal/Python turn, not Rust/SDK/offline dependency builds or durable
production review receipts. Do not interpret this as completed native support.

## Remaining work before production support

1. Add a portable per-turn layout and backend selection. Generate entry points,
   Hermes configuration, prompts and broker-client paths from that layout; remove
   Linux `/opt`, `/work` and `/proc/self/fd` assumptions without weakening pinned
   source-snapshot race protections.
2. Replace the fixed localhost inference bridge. Prefer direct Unix-socket HTTP
   transports where Hermes provider clients permit them. Any TCP alternative
   needs exclusive per-turn ports, authentication and exact endpoint permissions.
3. Verify native Python/venv, Rust, SDK and offline cache access with actual Hermes
   turns against local model and broker fixtures. Do not allow all of Homebrew,
   the user's home or `/Library` to solve missing-runtime failures.
4. Design and test hard scratch/build storage bounds. Seatbelt filesystem rules
   do not provide sized tmpfs or disk quotas; a directory-size watcher is not an
   equivalent bound.
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
