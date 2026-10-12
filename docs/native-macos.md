# Native macOS experiment

For a separate first hardware test, use [the tester guide](native-macos-tester.md)
and `tests/native_tester.py`. Its preflight, boundary/watchdog and optional soak
commands collect evidence without selecting the production native backend.
The native CI matrix validates that entry point on both chips before a tester
is asked to use it. Observer delivery and a live Hermes workload remain separate
acceptance work.

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
It uses shared bounded output/time capture through an independent native
watchdog and requires a host-created fixed-capacity workspace. The watchdog
cleans up the original process group after supervisor death, timeout, output
limit or normal leader exit. Detached descendants remain outside that guarantee.
The vertical uses the existing committed source exporter: it reads a pinned
commit, filters credential-shaped data, bounds the export and checks each blob's
hash. Native turn layout and production integration are still experimental.
The Rust fixture covers a small pure-Rust vendored dependency and Apple's
linker/SDK, not arbitrary workspaces, C/C++ dependencies or durable production
review receipts. Do not interpret this as completed native support.

## Pinned source export on macOS

The host opens the repository directory without following a final symlink.
Linux Git continues to use its existing `/proc/self/fd` directory pin. On macOS,
an isolated Python helper inherits only the repository descriptor, calls
`fchdir`, closes that descriptor and replaces itself with `/usr/bin/git`.
The host never changes its working directory or uses a `preexec_fn` in a
multithreaded process. Renaming the repository and replacing its original path
with another repository cannot redirect this export.

The committed-tree parser, export limits, credential filters, blob hash checks
and descriptor-relative destination writes are shared unchanged. A failed
directory pin has no pathname fallback. The native Hermes fixture and Mac CI
exercise this path; the focused helper tests also run on Linux. The source must
still be a trusted credential-free checkout: code and templates are exported
byte for byte, so this filter cannot identify every committed secret.

## Turn-visible path layout

`turn_layout.TurnLayout` describes the paths visible inside one turn. Both
launchers generate the Hermes entry arguments from this layout. The Linux
layout retains its existing mount names and command arguments; the native
layout uses the validated host-created workspace and staged read roots.
Native environment values for home, scratch, Cargo and broker-client files
come from the same layout.

Host-generated role instructions render broker command examples against the
layout's work and scratch paths, with shell quoting for spaces and quotes.
Only fixed host instruction text is rendered; PR and model text is not rewritten.
Relative paths, control characters, backticks and path-list delimiters are
refused. The layout describes paths and supplies no containment or authority.
The native reviewer fixture includes these instructions and verifies that the
real model request contains them. Automatic backend selection still requires
integration and acceptance testing.

## Role-specific native checkout permissions

The native launcher requires an explicit host-selected role from the broker
scope. Adjudicator and triage turns receive read-only work roots; only home and
scratch are writable. Reviewer, fixer and issue-fixer turns retain writable
work roots. Unknown roles fail closed without a writable fallback. The broker
continues to authorize each operation independently of filesystem permissions.

Mac probes verify read access, denied create/overwrite/chmod/rename/unlink for
judging roles, writable home/scratch, descendant write denial and denied access
to host files through a staged symlink. Writing roles must perform the same
checkout mutations successfully while still denying the symlink escape.
These are filesystem-policy probes; full adjudicator/triage/fixer/issue-fixer
Hermes turns, durable receipts and full supervisor integration remain gates.

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
verifies removal before deleting the backing files. Busy ejects receive at most
five normal attempts with 2/4/6/8-second backoffs (twenty seconds of sleep), then
one final forced attempt; image ownership is checked again
before every normal or forced eject. A failed eject can already have unmounted
the filesystem, so CI also records remaining exact-image attachments. This is
a bounded retry, not proof that the intermittent busy-eject cause is resolved.
Every eject now logs elapsed time, normal/forced mode, immediate exact-image
attachments and whether the private mount is still a separate filesystem. The
Hermes fixture separately records source-export and execution duration so a slow
turn is not automatically attributed to APFS. Diagnostic failures never turn a
failed eject into successful cleanup.
When an eject fails while the exact owned volume is still mounted, a five-second
`lsof` probe records its open users before a forced eject can remove the mount.
Its return code and stderr are recorded separately: an empty holder snapshot
supports a settling race but does not prove the absence of processes, kernel
references or earlier transient holders. `.metadata_never_index` and
`.fseventsd/no_log` are installed after mount verification, before turn files,
to request that this disposable volume not be indexed or event-logged. These
markers do not guarantee that no system daemon touched it during attachment.
The longer normal backoff is informed by
[riftri #458](https://github.com/assistant-ui/riftri/commit/2e4bbad5822b3a5bb87d401d1f0b3f7f883a1e30);
its [#484 device fix](https://github.com/assistant-ui/riftri/commit/52bab66d74b71e1d604df71a1390205bc876a585)
also illustrates why every retry rediscovers the exact image's device after a
partial unmount. This is evidence from another project, not a confirmed cause
for Diaktoros or proof that the race is specific to Apple Silicon.
Diaktoros's older ARM failure in
[run 38089176853](https://github.com/coe0718/diaktoros/actions/runs/38089176853/job/114321889627)
reported busy eject, then `lsof` reported that the mount was no longer a
filesystem about three seconds later. That confirms a later unmounted state,
not an empty-holder snapshot at the instant of failure. The real Mac regression
now deliberately performs a volume-only unmount, requires the image to remain
attached, then verifies cleanup by exact device rather than mount path. A final
forced command reporting busy is also reconciled against exact-image absence;
backing files are still deleted only after attachment and mount checks pass.
The short storage fixture repeats six guarded turns with an open file and cwd
on APFS, immediately ejecting each owned image after watchdog completion. This
increases reproduction opportunities; passing cycles do not explain prior busy
ejects.
The opt-in `test_native_eject_stress.py` adds 24 fresh 64 MiB volumes per
architecture, alternating idle, metadata create/rename/delete and 8 MiB fsynced
writes. These host-only cycles launch no turn process and close host files before
eject. `NATIVE_EJECT_STRESS` records the architecture, workload, exact image,
all immediate eject diagnostics and recovery outcome in CI logs, including busy
attempts that eventually succeed. A persistent failure still retains storage
and fails the job. A run with zero busy events is explicitly reported as such;
it cannot confirm the original race's cause.
In [Intel job 114383683177](https://github.com/coe0718/diaktoros/actions/runs/38110077986/job/114383683177),
idle stress cycle 6 captured four busy normal ejects with the image still
attached but already unmounted. The fifth normal eject succeeded after the
2/4/6/8-second backoff, without force, and workspace removal passed. No turn
process ran in this cycle. This confirms a transient teardown case independent
of turn leftovers and shows it is not exclusive to Apple Silicon. Because the
filesystem was already unmounted, these diagnostics contain no filesystem
holder snapshot; the exact OS holder remains unknown.
In [Intel job 114383096129](https://github.com/coe0718/diaktoros/actions/runs/38109877354/job/114383096129),
`hdiutil create` itself reported busy before any turn launched. Cleanup recorded
an already-unmounted image still attached through physical and synthesized APFS
devices. Three normal ejects returned busy; the fourth succeeded after 2/4/6
seconds of backoff, about 12 seconds after the first failure, without force.
This captures transient image attachment teardown on Intel, independently of
turn process ownership. It does not identify the exact OS holder or establish
that every historical busy failure has this same cause.
Creation now logs `NATIVE_STORAGE` stage/attempt/error and immediate attachment
state. A busy creation can retry up to three times only after the partial image
is detached, detachment/mount absence is verified, and its private backing file
is removed. Each retry creates a fresh image; no partial creation is accepted.
Persistent/non-busy failures still fail closed. Stress records include creation
events as well as eject events, so setup failures cannot masquerade as teardown
of a completed turn.
If detach/verification fails,
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
mechanism is introduced. The unmanaged probes remain as a baseline; guarded
kernel-mask probes also exercise a double-forked, reparented descendant after
timeout, supervisor SIGKILL and normal leader exit. They require direct session
and group changes to fail, register the known fixture processes before failure,
and require kernel exit notifications before disposing of fixture paths.
These probes do not establish recovery after watchdog or machine death.
Guarded
probes now verify cleanup after supervisor SIGKILL and normal leader exit,
watchdog signal denial, private-descriptor noninheritance and bounded capture. Production needs an enforceable descendant ownership
mechanism and independent host-death recovery, including capability revocation
and safe handling of attached storage. Apple launchd's process-group cleanup
alone does not establish ownership of a descendant that changes its group.

## Independent native watchdog

`native_lifecycle.capture` launches a trusted helper outside the child's Seatbelt
profile, in a separate session with a scrubbed environment. An anonymous pipe
has a writer in the supervisor and a reader in the watchdog; either EOF or an
explicit abort byte requests cleanup. The abort byte works even if a forked host
child retains a writer while the supervisor stays alive. The watchdog also
registers a kernel exit notification for its actual
supervisor, so a forked host child retaining a writer cannot delay cleanup after
supervisor death. Parent identity is checked around registration, before any
sandboxed work starts. Configuration and result pipes
are also host-only. None of these descriptors reach the sandboxed executable.

The watchdog uses kernel notifications for its known direct child, kills the
original process group before reaping its leader, and reports completion over a
private result pipe only after verifying group quiescence. After SIGKILL, a
bounded five-second libproc check requires the group to contain only its exited,
unreaped leader; snapshots include live processes and zombies. The watchdog
repeats group signalling while waiting, never signals individual discovered PIDs,
and restores the default SIGCHLD disposition before launching the leader so an
inherited ignore disposition cannot auto-reap it. Signal denial or delivery alone
does not prove completion. Missing leader reservation, query failure, excessive
membership or a drain deadline fails closed. Keeping the leader unreaped until cleanup reserves its PID;
the helper does not signal a cached group ID after reaping and possible PID reuse.
The shared capture implementation delegates abort to this lifeline rather than
killing the independent watchdog. Missing/invalid completion or a watchdog that
cannot finish cleanup raises `CleanupIncomplete`; the native launcher retains
its workspace without attempting detach or deletion. A successful completion
record must explicitly report that the original group was drained. The repeated
APFS eject probe includes eight reparented grandchildren holding the turn's file
and cwd during normal leader exit; Mac CI must validate this stronger check.
Six additional kernel-restricted fork-churn cycles alternate normal leader exit
and timeout while a factory creates short-lived workers (at most eight live
workers and 128 forks, with its own three-second limit). Normal exit requires
the factory to still be alive after at least 24 births; timeout requires observed
fork activity. The host requires an empty original group after capture completes
and retains fixture paths if bounded fixture cleanup cannot be confirmed. This
exercises cleanup during active fork churn; it does not prove every possible
kernel fork/signal interleaving or recovery after watchdog death.

The watchdog-death storage test kills the actual unreaped helper during a
kernel-restricted turn holding an APFS file open. It exercises `native_macos.run`
with a bounded fixture entry, requires `CleanupIncomplete`, and verifies that
the still-running child leaves the image mounted and retained without any eject
attempt. Test-only cleanup asks the cooperative fixture to stop, waits for its
previously registered kernel exit notification and empty group, then releases
the retained image. This test characterizes failure and retention; it does not
provide automatic crash recovery. The retention probe passed on both Mac
architectures in native run 38110741834.

Strict adapter-selected turns now arm a host-only `ownership.json` before
launch. The record identifies the private root and backing image by filesystem
identity, records a random turn ID, and advances from armed to running to
drained, then disposed after verified detachment. Atomic replacement plus file/directory fsync preserves the receipt
across process crashes. A private exclusive `ownership.lock` stays with the
guardian while it owns execution or disposal. Neither file is in a writable
Seatbelt root. Recorded guardian/leader/watchdog PIDs are diagnostic fields,
never restart signal targets.

In this mode, the trusted helper owns an unreaped trusted group anchor as the
cleanup guardian. The anchor starts the contained command in its existing group;
restricted descendants cannot change groups through the tested syscall or spawn
routes. The anchor remains outside Seatbelt, so the seat cannot signal it or
inherit its private descriptors.
It starts a separate disposable timeout watchdog and observes watchdog exit,
the actual supervisor, the private lifeline and the turn leader with kqueue.
If the watchdog dies, the guardian drains the original group before reaping,
writes the durable receipt, then verifies exact-image detachment and removes
storage. The caller receives `WatchdogLost`, marking an interrupted turn whose
cleanup was verified. Supervisor death follows the same disposal path. Normal
exit and timeout leave a drained receipt for host context cleanup.

`native_recovery.reconcile(roots)` accepts an explicit inventory of retained
private roots. A fresh supervisor process can dispose drained images without
old Python objects or cached disk IDs. An active lease, an armed/running receipt,
missing/malformed ownership data, changed filesystem identity or failed eject
retains storage. The command `python -m diaktoros.native_recovery ROOT...` exposes
the same operation and returns a nonzero status for retained failures. This API
does not select the production native backend.

The host supervisor's macOS recovery pass now discovers `dk-volume-*` roots
under the canonical host temporary directory before resuming ledger work. It
does not run in detached worker mode or on Linux. Discovery is bounded to
10,000 directory entries and 128 candidates per pass and reports an exhausted
inventory limit. Names alone authorize nothing: every candidate still needs
private ownership paths, a valid filesystem identity, a drained/disposed receipt
and an available lease. Unknown, symlinked, corrupt and active roots remain
untouched. `python -m diaktoros.native_recovery --discover` exposes the same pass.
The supervisor accepts an explicit `native_workspace_parent` for a dedicated
trusted inventory; the default remains the native workspace allocation parent.

After detachment is verified, recovery durably records disposed before deleting
owned contents. It keeps the receipt and lease until other contents are gone.
A restart can therefore finish interrupted directory removal even if the image
or mount directory has already disappeared. It still rechecks exact-image
attachment absence, and never accepts missing files as proof of turn exit.
A crash during the final receipt/lease removal can leave a tiny unknown root,
which is retained; the backing image has already been disposed at that point.

The new supervisor restart probe crashes a real owner after partial unmount,
then recovers its still-attached image through a fresh supervisor's discovery.
It also SIGKILLs a fresh supervisor after its disposed receipt is durable while
directory removal is underway. Both cases run two subsequent fresh supervisors
to require successful removal followed by an empty inventory. Mac CI must
validate this new startup probe.

A host-only anonymous socket handshake registers the anchor's kernel exit
identity before acknowledging seat launch. The anchor watches its actual guardian
parent with kqueue. Guardian death makes the anchor send SIGKILL to its own current
process group, including itself; no saved PID is used as a signal target. A
surviving host requires the registered NOTE_EXIT and an empty live/zombie group
snapshot before recording drained and disposing the exact image. `GuardianLost`
marks the interrupted turn after that verified disposal. Turn ID and filesystem
identities must still match the armed receipt. Missing registration, live group
members, failed verification or disposal keeps cleanup incomplete.

This evidence is deliberately ephemeral. Fresh supervisors still cannot turn a
running journal's saved PIDs into authority. If guardian and anchor both die while
seat processes survive, the host retains storage. If the host also dies, the
anchor can stop the group on guardian death, but no surviving observer can persist
the drained proof; restart retains the running receipt. Machine death/reboot
recovery still needs a separate policy.

The Mac probes cover watchdog self-SIGKILL, supervisor SIGKILL, recovery from
a drained receipt in a fresh process, guardian SIGKILL killing the seat and its
double-forked descendant, and simultaneous guardian/anchor loss retaining live
execution. Local journal tests cover lease exclusion, corrupt/replaced files,
durable arming, transition checks and failed eject retry eligibility. Mac CI
must validate the new guardian/recovery probes.

This closes only the original-group supervisor-death gap. A child that changes
its process group can still survive. Restricted turns add watchdog-death cleanup
and conservative receipt-based restart recovery; loss of all live cleanup
observers without a drained receipt, capability revocation and production backend integration
remain unresolved. Production still requires an
enforceable ownership mechanism for every descendant; a polling tree scan or
`kqueue` monitoring of selected PIDs is not equivalent.

## Remaining work before production support

The native detach probe also compares direct libc/raw `setsid` and `setpgid`
calls with `posix_spawn` session/group flags. XNU implements those spawn flags
internally. Both Intel and Apple Silicon probes confirmed that denying only
syscall numbers 82/147 leaves both spawn-attribute escapes open.
A separate candidate denies syscall 244 too, measuring the cost to ordinary
spawn while verifying fork/exec still works. On both Macs it blocks all tested
detach routes and ordinary spawn, while fork/exec remains usable. Both candidates
are test-only; neither is installed by the native launcher.
Blocking ordinary spawn can break Rust and Hermes subprocess execution. These
probes establish the constraint before choosing a production ownership design.
The next unprivileged candidate is a kernel-enforced syscall mask plus a tested
fork/exec compatibility path for required tool launches. A userspace spawn shim
alone would not enforce ownership. Spawn attributes, file actions, inherited
descriptors, multithreaded callers and tool timeout behavior all need validation
before such a compatibility layer can replace native spawn.

`native_spawn.c` is the first test-only Darwin compatibility prototype. With
native spawn/session/group syscalls denied separately by the kernel, it replaces
public spawn calls with fork/exec and a close-on-exec error pipe. It records
close/dup2/open/chdir/fchdir/inherit file actions, applies signal masks/defaults,
supports CLOEXEC_DEFAULT and PATH lookup, and returns exec errors after reaping
failed children. Session/group changes and unsupported flag bits return errors.
File-action snapshots and PATH expansion happen before fork; the child uses
async-signal-safe operations. Atfork hooks protect the action registry across
multithreaded forks.

The experimental launcher accepts a trusted `spawn_adapter` opt-in; the real
Hermes/Rust CI fixture enables it with kernel denial of session/group changes
and native spawn. Its trusted Python bootstrap uses fork/exec and keeps Hermes
terminal tools in the owned group. This is not production backend selection.
ARM builds include arm64 and arm64e slices for private tools and Apple launchers.
Private spawn
attributes, newer action extensions, dyld interposition on Apple platform
binaries, inherited descriptor races and complete tool timeout semantics remain
integration gates. Kernel denial is essential: removing or bypassing the
adapter must never restore native spawn or detach. Real Mac probes compare
ordinary-spawn compatibility with direct/raw/attribute escape denial, and test
file actions, cwd, signals, descriptors, exec errors and concurrent callers.

1. Integrate backend selection and the shared turn layout with full production
   orchestration, role-specific write roots, prompts, review context and receipts.
   Source export already has a native directory pin; production runtime/source
   generation ownership still needs integration with the per-turn layout.
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


## launchd ownership characterization

`tests/test_native_launchd.py` and the dedicated native launchd workflow test a
candidate for OS-owned teardown on both macOS 15 architectures. Apple's
[launchd.plist manual](https://github.com/apple-oss-distributions/launchd/blob/main/man/launchd.plist.5)
states that launchd kills the remaining process group when a job dies unless
`AbandonProcessGroup` is enabled. The probes exercise normal job exit and
SIGKILL with a restricted seat and double-forked descendant, plus an explicit
abandonment control that must leave both children alive until cooperative stop.
They register kernel exit notifications before triggering job death and require
an empty live/zombie group before removing fixtures. Each temporary user-domain
service has a unique label and is removed after the probe. Startup failures with
no trusted job identity receipt retain fixtures rather than infer exit.

This is characterization, not a launchd backend. It does not yet establish
restart-safe job identity, durable drained receipts after loss of every helper,
APFS disposal after observer death, capability revocation, or containment of a
process that can escape the job's group. Production selection remains disabled.


The first three launchd ownership probes passed on both architectures in
[run 38136922582](https://github.com/coe0718/diaktoros/actions/runs/38136922582).
The next probes connect OS-owned teardown to the existing durable receipt rules
using a real private APFS workspace. A separate test-only witness receives live
fixture identities through an anonymous host channel, registers kernel exits
before acknowledging the failure trigger, and holds the private recovery lease.
A fresh recovery process must report the workspace active while that lease is
held. After job SIGKILL, the witness requires every registered exit and an empty
live/zombie group, then durably records drained. Another fresh process must
verify image detachment and remove the workspace using that receipt.

The negative control kills the witness before job death. Even after launchd
stops the group, fresh recovery must retain the running receipt and backing
image: it has no durable exit proof. Only the still-live test harness's registered
kernel exits can release that fixture afterward. These tests exercise the bridge
between launchd teardown and APFS recovery; they do not establish recovery when
all witnesses die before writing proof, and do not activate a production backend.


Failed ejects after partial unmount also record a five-second-bounded `lsof`
query against the exact backing image and the disk nodes rediscovered for that
image. `backing_users`, its return code/stderr, and `backing_users_targets` are
separate from mounted `volume_users`; timeout is an explicit diagnostic error.
No discovered process is signalled. An open backing-image file may be normal
for disk-image machinery and is evidence to correlate, not proof of the busy
cause. This fills the observation gap in the captured idle, unmounted Intel
failure; it does not change eject retries or disposal authorization.


## Durable ownership preparation

The first implementation slice of [#624](https://github.com/coe0718/diaktoros/issues/624)
adds opt-in `Workspace(..., durable_ownership=True)` preparation receipts. Strict
Hermes turns require this mode. Before image creation, registration persists the
owner UID, canonical image path, root identity, turn ID and reserved byte count,
fsyncs the receipt and private directory, and fsyncs the parent directory entry.
The exclusive preparation lease remains held through image creation, inode
binding (`created`) and verified mount (`attached`). Arming preserves the same
turn ID and transfers to the existing guarded lifecycle protocol. Legacy
storage-only experiments retain their previous behavior.

Startup discovers these receipts, including interrupted creation before image
binding. It reports and retains `registered`, `created` and `attached` records:
an unlocked preparation lease cannot prove an orphaned hdiutil creator exited.
Interrupted preparation cannot transition directly to drained or disposed. This slice
provides durable discovery, not automatic disposal of interrupted preparation,
free-space admission or the eventual-cleanup scheduler. The boot recovery
extension is described below. Recorded
reserved bytes are accounting metadata; no capacity admission is claimed yet.
Local subprocess-exit tests cover each preparation boundary, and the real Mac
guarded recovery and Hermes fixtures now use early registration.

## Kernel boot identity recovery

New receipts record the kernel's `kern.bootsessionuuid`, read through a bounded,
scrubbed `/usr/sbin/sysctl` invocation. Unknown or malformed identity blocks new
Mac registration. A normal supervisor restart has the same boot identity and
cannot replace missing process-exit proof. Legacy receipts without a boot
identity retain their previous rules.

For image-bound `created`, `attached`, `armed` or `running` receipts, a verified
changed boot identity proves the old creator/turn cannot still run. Recovery
also requires the exact image to have no current attachment and its mount
path to be on the host filesystem. A reattached image is retained; recovery
never signals saved PIDs or detaches it using reboot proof. Only after these
checks does it persist a disposed receipt, including the new boot identity,
then reuse verified deletion. Interrupted deletion remains recoverable.
Unbound `registered` preparation is still retained, including partial images.

Local tests simulate changed/unchanged/missing identities, reattachment,
malformed and legacy receipts, and interruption after disposal proof. Both Mac
runners check the real kernel identity across fresh Python processes and its
registration receipt. This is not a physical reboot acceptance test: that gate
still requires a reboot-capable Mac runner with storage retained across reboot.

## Eventual disposal pool (experimental)

`native_cleanup.Manager(<private-host-root>)` supplies serialized admission and
an opt-in `Workspace(..., cleanup_manager=manager)`. The root must have a
canonical, private host-owned parent and must survive the intended recovery
window. Put it beside the install ledger as `native-cleanup` to use the
supervisor/watchdog integration; a custom root can be swept explicitly with
`python -m diaktoros.native_cleanup <root> --sweep`. Without `--sweep`, the command
only reads status. Production backend selection remains disabled.

The ownership receipts inside `volumes` are the queue. Registration precedes
image creation. Verified group drain precedes pending disposal; neither an
unlocked lease nor a discovered process ID can supply completion proof. A busy
eject after verified completion persists `cleanup_pending`, first pending time,
attempt count and last error before context exit can succeed. A failed queue
write prevents that acknowledgement. Normal results survive deferred disposal;
watchdog/guardian interruption raises `StoragePending` only after verified
process completion and durable queuing, distinct from `CleanupIncomplete`.

A separate disposal lease protects the host's result-recording window after the
seat exits. Sweeps cannot delete a still-live host's workspace just because its
group is drained. The lease closes at context cleanup and is not passed to the
seat or helpers. Host death releases it; independent guardian cleanup can then
complete or queue disposal. Exact-image detach verification remains mandatory.

Admission defaults to one active turn, at most four pending images, a shared
2 GiB reservation ceiling, 256 MiB host free-space headroom, and a five-minute
oldest-pending threshold. All limits are adjustable through `Policy`; the
policy survives restart. Admission, root reservation and registration share a
private control lease. Unknown/corrupt ownership or unverified turn completion
blocks new admission and is retained. A missing control file alongside existing
workspaces is an error. Inventory is bounded at 128 entries. Unregistered
images are reported rather than adopted into deletion authority.

Each host supervisor recovery pass (including watchdog ledger sweeps) retries
one owned workspace by default. A persisted rotating cursor prevents a busy
image from starving others. Pool receipts can be discovered after restart even
if no pending flag was written; verified state and released host disposal lease
still decide eligibility. Pause/resume transitions are recorded in a bounded
outbox and emitted once into the watchdog/operator output, with count, reserved
bytes, oldest age and last error. Delivery is claimed before output: interrupted
or failed delivery remains visible as uncertain and is not blindly replayed.
The observer chat-route adapter is not wired by this slice.

Doctor and explain read the ledger-adjacent pool without creating files,
retrying detaches or acknowledging notices. Local validation covers deferred
results, failed queue writes, count/byte/age/full-disk admission, orphan
retention, result-recording leases, restart policy/notices, fair retries and
supervisor integration. Both Mac lanes exercise real guarded turn completion
with injected busy eject, plus actual watchdog SIGKILL with a double-forked
seat descendant followed by pool disposal.

This does not satisfy the production acceptance gate yet: 200-turn adversarial
soaks on both chips, physical reboot, process escapes, observer-route delivery,
and disposal within five sweeps/five minutes remain required. A sweep bounds
its number of workspaces; existing disk-command timeouts and watchdog cadence
now run inside the bounded watchdog worker described below; the overall five-minute disposal target still needs natural-busy acceptance evidence.

The `.fseventsd/no_log` hint is best effort: macOS may create a protected,
system-owned `.fseventsd` directory before the host reaches it. Permission
denial is recorded as `filesystem-event-marker` with `installed=false` rather
than aborting a verified mount. The host never changes system ownership or
kills an indexing/event helper. This hint supplies no containment proof.

## 200-turn guarded cleanup soak

Watchdog/startup pool cleanup and the cleanup CLI now use `bounded_sweep`.
A fresh isolated worker covers pool loading, inventory, lease acquisition,
disk commands, retry sleeps and disposal within one 45-second host wait budget.
The host kills only its newly created waitable worker's process group on timeout,
then allows up to one second for reaping. It returns `status: timeout`, an empty
verified-removal list and whether the worker was reaped; existing ownership and
pending receipts remain the recovery evidence. It never infers disposal from
a killed worker. A worker-side timer at budget plus two seconds also stops that
group if its parent dies. System disk-image helpers outside that group are not
signalled. A kernel-uninterruptible worker may defer exit; held leases continue
to block conflicting cleanup/admission while the watchdog can resume.

Pause/resume notices still print from the worker and keep the existing durable
delivery protocol. A malformed result or nonzero worker exit is reported as
unverified. Direct `Manager.sweep()` remains synchronous for callers with their
own external deadline (including forensic workers); doctor/explain stay read-only.
Local real-subprocess tests cover timeout, receipt preservation, subsequent
recovery, malformed/nonzero results and the orphan-worker timer. This bounds
watchdog pool work, not the separate legacy workspace-discovery API, OS process
creation latency or a physical reboot test.

`native-cleanup-soak.yml` runs 200 real guarded command turns independently on
Apple Silicon and Intel, after each lane passes the cleanup-pool preconditions.
Each turn creates a fresh 64 MiB case-sensitive APFS workspace under the opt-in
pool. The five-way mix covers fsync writes, Python and raw syscall setsid/setpgid
attempts, double fork, a background build-server-shaped helper with an open
cache file, and a background daemon with no open workspace file. These are
controlled Python command fixtures, not 200 Hermes model/tool turns or actual
language build servers; real Hermes validation remains in the boundary matrix.

The host registers live kernel exit events for the seat and its surviving
helpers before releasing the parent to exit. Every turn requires all those
exits, an empty process-group snapshot, the matching drained receipt, and its
original exit code. Results are fsynced outside the volume before releasing the
host disposal lease. An injected busy eject on every tenth turn forces queue
coverage; genuine observed deferrals are counted separately. A fresh Python
process performs each cleanup sweep. Admission pauses must clear through those
sweeps; unknown ownership fails the run.

Every eligible pending image must disappear within five total sweeps and five
minutes of result recording, including initial detach time. Late success is a
failure, not a passing retry. The last reconciliation requires an empty owned
workspace inventory. Four local acceptance-accounting checks verify deadline
and live-exit requirements. The 60-minute CI job and 90-second fresh-worker
limit bound the probe; they do not replace a production cleanup worker budget.
No physical reboot or private spawn-action compatibility is claimed.

Artifacts always retain runtime/precondition logs, durable per-turn results,
structured turn/sweep events, final acceptance counts, cleanup-control and
ownership receipts, failure traceback, current image attachments and process
snapshot. Backing images stay on the runner on failure and are not uploaded.
The soak and boundary workflows preserve in-progress runs when the PR is updated.

## Naturally busy images through the queue

`native-eventual-eject.yml` runs all four forensic modes of host-only churn on ARM and
Intel, up to 128 images per lane. It uses `native_eject_forensics.py
--eventual-cleanup --require-natural-pending --forensic-precondition`. These runs inject no eject error.
They preserve the baseline forensic mode separately.

Each image is registered under a private cleanup pool before creation. No seat
is launched in this workload. After synchronous create/attach and host writes
return, the trusted fixture records its explicit no-seat-launch completion
receipt. This test-only proof is never substituted for process drain in a real
native turn. Normal bounded detach runs first. A qualifying natural case must
have five failed normal busy ejects and a failed final forced busy eject,
followed by a matching durable pending receipt inside the same pool.

The first qualifying image is retried by up to five fresh worker processes,
using the production pool recovery API. The five-minute deadline starts before
initial detach, so the initial backoff and diagnostic overhead count. The
probe retains original failures, queue/control receipts, helper measurements,
worker stdout/stderr and OS log evidence. Failure or worker timeout retains
storage. A pass requires the exact image's verified disposal and an empty owned
inventory. A lane with no qualifying natural case is marked inconclusive and
exits 2; quiet churn cannot establish natural-busy recovery. Twenty-five local collector checks pass, including deadline, durable receipt,
exhaustion, verified removal and inconclusive-result checks for the added rules.

The next run completed 128 cycles in each of four lanes (512 total), with no
qualifying exhausted eject; all four lanes were inconclusive. The third guarded
soak passed 200 turns on each chip, with 20 injected deferrals per chip, zero
natural deferrals and zero remaining images. Worst queued disposal was one sweep,
0.339 seconds on ARM and 0.616 seconds on Intel. These remain command fixtures,
not a Hermes model-turn soak or a physical reboot test.

An Intel forensic lane instead failed during image creation: `hdiutil create`
returned busy with a partial attachment, which eventually detached. A live
creator now records `preparation_completed_no_turn` when synchronous setup
returns a StorageError before any seat is launched. It binds a surviving partial
image's identity and permits verified cleanup or durable deferred disposal while
preserving the setup failure for the caller. A missing backing file still
requires exact-image attachment absence before deletion. Timeouts, interrupted
creators and recovered preparation receipts cannot supply this proof; those
remain retained and block admission. This does not establish eventual recovery
of a naturally exhausted normal turn eject.

## Dedicated eject forensics

An additional `--stress-timing` queued lane now runs on each chip alongside the
eight forensic lanes. It keeps the original native disk-command implementation,
the stress harness's eight one-MiB fsynced writes, and native eject diagnostics.
It adds no helper sampling, first-busy collector log writes, verbose/sync/unmount
interventions or background OS log reader between retries. Exact-image event
observation and durable queue proof remain required; fresh workers still must
verify disposal within five sweeps/five minutes. Artifacts keep cycle/eject
events, receipts and worker outcomes, with system-history collected afterwards.
This is a timing comparison, not proof that forensic sampling causes the race;
pool ownership paths and receipt writes still differ from the legacy stress test.
The latest eight-lane run caught transient busy ejects but none exhausted the
policy; separate Intel stress lanes did exhaust it. No cleanup error is injected.

The fourth eventual-recovery run captured an Intel sync-unmount image whose
five normal ejects and final force all returned busy. Production recovery
durably queued it, but the collector incorrectly stopped before retrying:
recovery reconstructs a base `Workspace`, bypassing the test subclass's event
list and busy flag. The collector now observes that reconstructed workspace's
exact root/image ejects during initial disposal, preserving production ownership
checks and queue decisions. Regression coverage follows the actual reconstruction,
records all six failures, reaches the fresh cleanup worker and excludes other
images. This captured case establishes genuine exhaustion and durable queuing,
not eventual disposal. Subsequently, the Intel standard lane in
[run 38301555861](https://github.com/coe0718/diaktoros/actions/runs/38301555861/job/114985424530)
captured five failed normal ejects and a failed force with no injection, durably
queued the image, and verified exact-image removal in one fresh sweep at 29.454
seconds from initial disposal eligibility. Its pool was empty afterwards. This
meets the five-sweep/five-minute target for that single natural case; repeatability
and an ARM exhausted-eject recovery capture remain open.

Queued recovery now also runs the baseline forensic matrix: standard, verbose,
sync-delay and sync-unmount on both ARM and Intel. `--forensic-precondition`
preserves the baseline hdiutil-help and recovery-test ordering before host-only
image churn. The original read-only-after-failure forensic workflow remains a
comparison lane; the queued variant necessarily uses private pool-owned paths
and receipts. Initial detach keeps each lane's instrumentation. Subsequent
sweeps use fresh processes and the ordinary cleanup implementation, without
repeating the test-only sync/unmount preparation or verbose snapshots.
Every result records its mode and precondition suite. No natural exhausted
eject means inconclusive, a failed precondition prevents churn, and verified
recovery must still meet five sweeps/five minutes from initial disposal eligibility.

`native-eject-forensics.yml` runs eight independent Mac lanes: ARM and Intel,
each with standard, verbose, sync-plus-delay or sync-plus-forced-unmount instrumentation. The test-only collector streams
Disk Arbitration, disk-image, APFS, Spotlight and filesystem-event logs before
running the recovery precondition and up to 64 fresh idle/metadata/fsync images.
It preserves complete create/attach/detach stdout and stderr (bounded to 2 MiB
per file), timestamps, raw exact-image attachment metadata, and command outcomes.
A failed recovery precondition is reported separately and keeps the job red.

The standard lane preserves detach flags and snapshots disk state only after
cleanup exhausts its normal retry/force policy. The verbose lane adds `-verbose`
and takes a snapshot at its first busy eject, so its instrumentation can change
timing. Snapshots include block/raw device holders, disk/APFS metadata, I/O
registry media state and read-only stacks of disk-image helpers found by lsof.
A holder name or stack is an observation, not process ownership or proof of
causality; no discovered holder receives a termination signal.

Snapshots also archive the IOService registry and extract owned media paths,
their provider ancestors, and registry-visible user clients including creator
properties when published. This retains sibling clients under a media provider,
which a media-only descendant snapshot can miss. A complete parsed relationship
file is saved separately from the capped raw archive. Missing, malformed or
oversized archives are explicit diagnostic errors. These are read-only registry
queries; the probe does not open user clients or signal their reported creators.
Registry visibility is not a census of all IOKit connections. No lsof file
descriptor match cannot exclude a user-client connection, and a shared provider
path does not prove that a client holds the media open.

The sync-plus-delay lane uses standard detach flags and executes system-wide
`/bin/sync` followed by two seconds of waiting after workload writes and before
the first detach. It repeats the same idle/metadata/fsync workloads and retry
policy as the standard control. It does not alter production teardown. Compare
busy images and persistent failures per completed cycle and architecture, not
individual retry counts. A difference supports a settling hypothesis but cannot
separate flushing from extra elapsed time or prove a specific APFS mechanism;
the system-wide flush can also affect other volumes on that runner.

The `--sync-unmount` lane adds `diskutil unmountDisk force` after that same sync
and two-second wait, before the existing normal-detach retries and final force.
Each target must still be rediscovered under the exact private image immediately
before unmount; disappeared targets are skipped. Failed experimental unmounts
are reported as failed interventions while normal workspace cleanup still runs.
This is a host-only experiment, not authority to force unmount a live turn.
Alternate eject commands and non-APFS formats remain separate future comparisons;
combining them here would obscure the effect of the pre-unmount step.

[Completed run 38143441748](https://github.com/coe0718/diaktoros/actions/runs/38143441748)
finished all six original lanes, 64 cycles each, with no persistent failures.
ARM standard/sync-delay had 0/3 busy images respectively; Intel standard/sync-delay
had 1/1. Each verbose lane had one busy image. This sample provides no evidence
that sync-plus-delay reduces failures and is too small to establish harm.
Both verbose registry extractions failed because the archive root was a dictionary,
not an array. The parser now accepts a single registry root as well as an array;
the failed extractions are not evidence that user clients were absent.

Forensic workflow updates no longer cancel active captures. Earlier CPU run
38142871655 was interrupted by a PR update and its Intel artifacts lacked final
result events; those partial artifacts cannot supply complete failure-rate or
give-up comparisons. A completed run requires final result events in each lane.

For each image that encounters busy eject, both lanes record helper cumulative
CPU time, process state, command and start time at first failure and at successful
cleanup or give-up. This small lsof/ps observation adds timing overhead even in
the standard lane. Deltas require matching diagnostic identity fields and a
nondecreasing counter; disappearance, changed identity and decreasing counters
yield an unknown delta, not zero. BSD ps start time has only second precision,
so these fields cannot exclude PID reuse or authorize cleanup. Zero displayed CPU
growth can mean an ordinary wait or sub-resolution work and does not prove a
hang. CPU growth does not prove that the work advances detach. No visible device
holder likewise does not establish kernel-only causality.

After persistent failure, the collector issues no additional eject or force.
It retains the image and records passive snapshots immediately and after
5/15/30 seconds of additional waiting, measured between snapshot commands (the
commands themselves add elapsed time). The system stream retains its first and
last 16 MiB, and each snapshot queries the last two minutes of lifecycle and
rejection logs. Routine callback traffic is filtered out. Diagnostic timeouts and truncation
are explicit. CI uploads evidence even on failure; backing images and seat
contents are excluded. These experiments do not change production storage policy.

### Captured failures, 2026-10-11

[Forensics run 38141334211](https://github.com/coe0718/diaktoros/actions/runs/38141334211)
captured busy ejects on both architectures. ARM verbose cycle 24 was idle,
unmounted successfully, failed its first eject and recovered on the next normal
attempt about nine seconds later, including snapshot overhead. Intel standard
cycle 57 was also idle: all five normal attempts and the final forced attempt
failed. Its image remained attached and unmounted through the final passive
snapshot. Neither cycle launched a turn process.

Full-name lsof queries found only `diskimages-helper` holding the backing image,
with no reported block/raw-device holders. Sampled helper threads waited in the
run loop and `DI_kextDriveGetRequest`; this does not establish a helper deadlock.
ARM's sampled media reported `Open = No`. Intel's persistent case instead showed
the whole physical disk `Open = Yes` in the first three snapshots while its
partition and synthesized APFS media reported `Open = No`; the physical disk
changed to `Open = No` by the last snapshot. I/O registry busy counts were zero.
This suggests a physical-device open worth investigating, but snapshots followed
diskutil metadata queries, which can themselves affect device state. Verbose
failures occurred even when `IOServiceWaitQuiet` returned in
microseconds. Those later observations do not identify the state at the exact
failed eject, nor exclude a transient holder.

The first collector exhausted its stream cap before these failures, and its
15-minute retrospective queries could time out before reaching the failure.
Consequently these artifacts do not distinguish client approval rejection from
kernel eject rejection. The narrowed filter, retained stream tail and short
snapshot queries address that evidence gap. The persistent Intel case also
means neither an ARM-only explanation nor twenty seconds of backoff is sufficient
to close the blocker.

[Follow-up run 38142229822](https://github.com/coe0718/diaktoros/actions/runs/38142229822)
preserved an untruncated Intel verbose stream with genuine busy ejects. Cycle 8
records three approved eject-approval responses at 13:14:07.255–.258 UTC,
followed by eject ongoing, failure, and status `0x00000010` (`EBUSY`) at
13:14:07.259. This distinguishes that failure from a client approval veto.
The whole physical disk also reports `Open = Yes` in the registry snapshot
taken before diskutil metadata queries. Cycle 8 and three later busy cycles
eventually recovered under the existing cleanup policy.

These messages match the device-eject callback in Apple's published
[Disk Arbitration source](https://github.com/apple-oss-distributions/DiskArbitration/blob/main/diskarbitrationd/DARequest.c).
That worker opens the raw device and calls `DKIOCEJECT`, returning errno on
failure. The source mapping is an inference, not a syscall trace of the runner:
it narrows the observed route to device eject but does not establish whether
the error originated in open or ioctl, identify the physical-device opener, or
prove that the earlier persistent failure has exactly the same cause. The
collector also retains open-failure messages to discriminate those paths.

### Investigation conclusion and operating policy

The same follow-up run contains 16 failed detach commands across the Intel
standard and verbose lanes. Timestamp-matched device-eject logs show three
approved client responses followed by `EBUSY` for each failure. Neither complete
stream contains an eject-approval dissent. Mount-approval dissents elsewhere in
the stream are a different operation and do not explain these eject failures.

Treat this as an observed macOS APFS/DiskImages device-eject quirk, consistent
with a framework/kernel teardown race. No application cleanup defect or client
veto was identified in the captured cases. The exact internal cause remains
unconfirmed; the earlier persistent case lacks a complete failure-time system
stream, so it cannot be assigned the same cause with certainty. Idle host-only
reproduction establishes that turn descendants are not required for the failure,
not that every possible userspace hold has been excluded.

The operating response is the existing bounded patient retry policy, followed
by one force attempt and retention if exact-image detachment cannot be verified.
Never delete an attached backing image or kill a discovered helper to make eject
appear successful. Keep diagnostics for retained storage and report cleanup
failure. Further retry increases or turn-process hunting are not justified by
these captures. Helper CPU and stack measurements remain corroborating evidence,
not automatic hang detection. Persistent storage retention remains an operational
limitation to account for before enabling the production backend.
