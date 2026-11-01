# Security

[Documentation index](README.md) · [Architecture](architecture.md) · [Development](development.md) · [Operations](operations.md)

## Contents

- [Threat model and trust boundary](#threat-model-and-trust-boundary)
- [Sandbox and export policy](#sandbox-and-export-policy)
- [Scoped write policy](#scoped-write-policy)
- [Push policy and the PR race](#push-policy-and-the-pr-race)
- [Inference policy and DirectSDK](#inference-policy-and-directsdk)
- [Ledger and ambiguous outcomes](#ledger-and-ambiguous-outcomes)
- [Limitations and operator responsibilities](#limitations-and-operator-responsibilities)
- [Security evidence](#security-evidence)

## Threat model and trust boundary

PR/issue text, repository files, build scripts, tool output and model-generated operations are untrusted input. They may persuade the model to run commands or submit unwanted content. Prompt instructions help define behavior, but the enforced boundary is the contained process tree plus host-owned capability policy—not the model following instructions.

| Component | Trust and authority |
|---|---|
| Operator configuration, Hermes runtime/source/venv, plugin and host OS | Trusted inputs; compromise can invalidate containment |
| Gateway routes and gates | Host-side admission and webhook integration; not the credentialless agent |
| Supervisor, fetcher, broker, inference proxy | Trusted control plane; can read credentials and contact external services |
| Sandbox Hermes and descendants | Untrusted execution; receive scoped sockets, staged data and a disposable home, not real credentials |
| Model provider / native subscription client | External/host transport sees model input; not a confidentiality boundary for exported code |
| Observer destinations | Operational notification transports; neither access-control enforcement nor guaranteed audit storage |

The Diaktoros boundary is not a general sandbox for every Hermes gateway agent. Only turns launched through the contained path receive this boundary. A same-UID host process is not isolated by the fetcher or file modes; a malicious host process can interfere with trusted state. The native DirectSDK helper is host-side trusted code, not a second bubblewrap-contained agent.

## Sandbox and export policy

`contained.command` rejects host networking and uses `bwrap --unshare-all`, `--die-with-parent` and `--new-session`. It has an allowlisted mount layout, not a caller-provided arbitrary bind list:

- System executable/library trees, the configured Python runtime/venv, staged Hermes source and Rust toolchain are read-only.
- `/etc` is staged with one user/group entry; only `/etc/alternatives` is additionally bound read-only. The host's ordinary `/etc` is not mounted wholesale.
- `/proc` and a private `/dev` are created; root and `/dev` are remounted read-only after mounting.
- Writable `/work` is a tmpfs populated from a read-only export. Adjudicator/triage `/work` is read-only; `/target` is a separate sized build target in that case.
- `/tmp` is a sized tmpfs; Cargo registry caches are read-only mounts and `CARGO_NET_OFFLINE=true` is always set.
- The root and `/dev` are remounted read-only. `/dev/shm` is a separate 256 MiB tmpfs, writable, so tests that use POSIX semaphores or shared memory (Python `multiprocessing`) run inside a turn. Nothing from the host is mounted there.
- `/home/agent` is a writable, private per-turn **host bind**, not a disk-quota-enforced tmpfs.
- The only broker/inference directories mounted into the sandbox contain their respective socket. Staged review diff and client files are read-only.

The parent environment is rebuilt from scratch. The sandbox model configuration uses a placeholder credential for its local bridge, not a real upstream token. `contained._run` caps each captured stdout/stderr stream at 256 KiB, bounds wall time and kills the process group on timeout, output overflow or other parent-side failure.

Default tmpfs data caps are 2 GiB scratch and 8 GiB checkout/build target. `DIAKTOROS_SCRATCH_SIZE_GIB` and `DIAKTOROS_CHECKOUT_SIZE_GIB` accept integer GiB values from 1 through 1024; invalid values are reported and ignored in favor of defaults. These are host environment settings inherited by workers, not per-loop permission controls.

### Two export checks, with different purposes

**Hermes source:** `trusted_turn` exports permitted regular blobs from committed `HEAD`, verifies object hashes and bounds the snapshot. Hidden/excluded paths and credential-container names are omitted; credential-shaped names/content are additionally filtered where that will not break imported code. Code/templates are deliberately not removed merely because they contain credential-shaped text. `exported_secrets` reports such code text as advisories. This is a containment aid, **not a complete secret scanner**. Keep the source and venv secret-free.

**Reviewed repository:** `trusted_fetch` verifies distinct reader/reviewer/fixer mappings and actual principals, checks live repository/ref/head, validates tree metadata and hashes tarball files against the Git blob IDs. It rejects unsafe paths, `.git`, devices, hard links, duplicate/extra/missing files and truncated trees; symlinks, submodules and `.gitmodules` are left out of the export and named as not exported, so the turn continues without them (#645). Its 10,000-entry and 100-MiB bounds do not mean every repository is supported. Tarball redirects are manually limited to one HTTPS hop to `codeload.github.com`, without forwarding the reader's Authorization header; further redirects are refused. CI-fix job logs (`fix_ci`) come through GitHub's redirect from `/actions/jobs/{id}/logs` to its log storage; the host follows it over HTTPS only and drops the Authorization header whenever the host changes.

Repository export is an integrity check, not sanitization of source content. Any secret already committed to an accepted repository file is readable by the sandbox and may reach inference or a published body.

## Scoped write policy

The Unix socket identifies one host-created `RunScope`: repository, number, head, branch, role and host run metadata. The sandbox cannot supply a replacement URL, token, principal, reviewer or destination. Unknown request fields, wrong roles/operations, oversized frames and invalid payloads are refused. General REST frames are limited to 16 KiB and text bodies to 12 KiB; fixer/issue-fixer manifest frames have a separate 768-KiB ceiling (a diff of up to 512 KiB, base64-encoded).

For ordinary PR writes, `broker.authorize` verifies:

1. The repository equals the configured repository; PR number and SHA have the expected types/formats.
2. The role/operation pair is permitted.
3. Explicit reader/reviewer/fixer mappings resolve to distinct credential files (a PAT file, or a GitHub App's private key) and distinct verified GitHub principals (`/user`, called with an App's installation token).
4. The live PR has the exact number, is open and explicitly non-draft, has the configured base repository/branch and scoped head/branch, and is not a fork head.
5. Fixer writes additionally require an allowlisted PR author and a current effective `CHANGES_REQUESTED` review where required. The post-push handoff checks the new head without requiring a verdict that cannot yet exist there.

| Role | Accepted effect | Important restriction |
|---|---|---|
| `reviewer` | One review, `APPROVE` or `REQUEST_CHANGES`, with nonempty body | `COMMENT` is not a verdict; commit ID is pinned; production uses a host receipt |
| `reviewer` (issues, #247) | Up to 3 new issues from its issue-tier findings, filed as the reviewer's own login | Title (one line, 120 chars), body (6 KiB) and labels from the loop's triage list only — no assignee, mention target, milestone or edit; re-authorized against the live PR like a review; recorded before the POST; one title per PR, so a retry never refiles; signed by the host with the PR and lineage depth; an unknown POST outcome is `uncertain`, never replayed |
| `fixer` | One bounded push followed by one review request; optional answers comment attached to the handoff | Normally requires confirmed push first; host policy is reloaded at write time |
| Partial-view `fixer` | Answers-only comment | Cannot push or request review of a nonexistent new push |
| `adjudicator` | One durable `ACCEPT`, `REJECT` or `RESPEC` ruling | Optional notice/comment delivery is separate; no code edits, push or merge authority |
| `triage` | Configured labels, at most configured `max_labels`, and optional allowed comment | Live open issue, allowlisted author and triage policy are rechecked |
| `issue_fixer` | New branch/PR/review-request sequence, or one explanation comment | Live issue/fix-label/author/maintainer policy; unattended issue fixing must be enabled |

The optional Desktop page's backend routes (`loops`, `stats`, `now`, `overview`, `pr`, `health`, `settings`) are read-only: they read the loop's config, the run ledger and — for `health` — the installation's own files (profiles, the cron store, the venvs), and none writes. The Desktop page's `health` read runs `doctor`'s checks **with GitHub probes automatically** (see below) — still no write, and no credential value is returned; `pr` carries the host's own next-step line and what the ledger recorded, never a run's error text or a turn's output. The ledger is opened through the same read-only connection policy the other read-only views use (`run_supervisor.views.read_only_connect`): no migration, and no `-wal`/`-shm` created for a ledger that has none. **The probed health read is network-capable, automatic and local-only:** the page sends `health?loop=<id>&probe=1` when a loop is selected, and it reads the loop's own tokens and makes GitHub reads for the hook, expiry and scope checks (the same set `hermes dk doctor` reads). The Desktop panel discloses this and offers an explicit Refresh GitHub. The read is deduplicated/cached and never part of a poll or the local event-invalidation namespace, so selection cannot become a doctor/API sweep. It never writes to GitHub or the repository. It answers only a caller on this machine — a non-loopback peer, or any forwarding header at all (`Forwarded`, `X-Forwarded-For`, `X-Forwarded-Host`, `X-Real-IP`; presence counts, so an empty value refuses too) is refused with 403 before a token is opened — because the dashboard server is the surface Caddy exposes off-host. **Only the probed health request is local-only; offline health remains available to authenticated remote callers.** After a 403, Desktop falls back to the validated offline read and says “GitHub not probed from this caller”. Transport/503 probe failures likewise try offline health and say “GitHub probe unavailable”, not that GitHub is down. These are local checks only: unprobed network checks are not passing. Both reads use the same stable ten-minute cache entry, with no automatic retry, polling or socket invalidation; explicit Refresh GitHub retries the probe first. If both reads fail, the read is unavailable rather than a healthy empty result. See [Desktop read diagnostics](desktop-read-diagnostics.md). Its renderer half imports only the Desktop SDK and React (Hermes's catalog lint enforces this).

**Desktop invalidation socket (#647):** `/api/plugins/diaktoros/events` is a
read-only WebSocket, explicitly admitted with Hermes's `_ws_auth_ok` before
accepting or starting its watcher. Native grants and token credentials are
validated by the host; no new credential, URL construction, or transport is
introduced. Admission failures close with policy violation. The watcher reads
bounded loop config JSON solely for state-root discovery and filesystem metadata
for ledger/WAL/config/state changes; it opens no database or token file, performs
no network request and makes no writes. Hints contain only a fixed event/version,
never filenames, contents, errors or credentials. Authenticated clients receive
an initial hint and coalesced change hints, not authoritative data. The renderer
invalidates existing server queries and uses their validated REST results;
existing polling, stale disclosures and unavailable semantics remain intact.
This does not authorize any operation or change write policy, navigation or
notification behavior. OAuth and all host-isolated user plugin APIs currently
cannot use this SDK socket and retain polling/on-demand reads; the host proxy
forwards HTTP only, not WebSocket upgrades. In-process plugin APIs support it.

**The page's ledger write (#371):** `POST /retry` re-arms the run `hermes dk retry` would re-arm. It is not a second decision engine — the selection is `retry_action`'s (the newest head that still has an offerable row, the same rule the CLI runs) and the re-arm itself is `Supervisor.retry`'s, including its write-evidence and push-policy refusals. The route is behind the gateway's own auth, like every other route; it reads the body as `{loop, pr, seat, head}`, where `head` is the **confirmation** the operator saw, never a claim about the run. It re-reads the ledger rather than trusting any client state, and refuses with 409 and one bounded line in the host's words anything `retry` refuses: an uncertain run, a run with any write-ahead record, a run that is not failed/waiting/push-policy-cancelled, a superseded cancellation, a PR the watchdog's last complete listing does not show open, or a confirmation whose head has moved (it refuses rather than re-arming a different head). A retry that succeeds records the operator action and asks the host to start a worker, exactly as the CLI does. It never calls GitHub and never opens a token file: the PR-head tiebreaker and the #723 live PR read are CLI-only, so the page uses the ledger-only answer the CLI itself falls back to when its PR read fails. The page offers the control only on rows the host marks retryable, asks for confirmation every time, and shows the host's answer and the state read back from the ledger.

**Confirmed webhook control (#645):** `GET /hooks` reads GitHub through `arm`'s default
host token resolution (the loop's mapped reader login); `POST /hooks` accepts only
`{loop, action, confirmation}` and calls the shared `arm_action.perform` used by the CLI,
including resume ping. The reader token must be allowed to administer webhooks. A separate
mapped admin can still be selected through CLI `--admin-token`; the page never accepts a
credential or token path. Both routes use gateway auth and are intentionally not local-only:
this expands the authenticated Desktop surface to credentialed GitHub reads and writes.
No token contents, paths, URLs or raw transport errors are returned. The opaque HMAC
confirmation binds loop config, route targets and owned hook IDs/wiring/state; the host
re-reads these before writing and refuses a stale or unavailable confirmation with 409.
Restarting the backend invalidates old confirmations. Missing, mixed and unreadable states
are unknown, never inferred paused. Pause requires verified ownership and a complete all-active
hook set, not correct resume wiring; the armed miswired state carries an explicit repair warning,
not a delivery-health claim. Resume of a paused miswired set remains refused. A partial
write/readback or failed resume ping reports
`ok:false`, retains truthful observed hook status and withholds automatic follow-up controls.
Pausing stops webhook delivery, not running turns; sweeps may still recover events. This is
not a global stop, drain, rollback or merge permission. Diaktoros never merges.

**Confirmed one-shot control (#644):** `GET /actions` reads a PR using the host's existing
credential selection and returns only a per-target offer. `POST /actions` accepts exactly
`{loop, action, pr, seat, confirmation}`, never a token, path, arbitrary reason, grant or command.
Both use gateway auth, not the local-only probe restriction. `one_shot_action` owns selection
and execution for CLI and Desktop; CLI commands are I/O wrappers, not a second rulebook.
Currently only escalation is offered: the confirmation names the exact head being parked
against further reviews/fixes and the adjudicator turn being spent. Its process-local HMAC
binds config, PR, effective reviews, marker and ledger identity; a backend restart invalidates
it. Revalidation occurs again at the breach reservation under its existing lock, and a changed
identity vetoes the reservation rather than applying the latest identity. GitHub observations
are not a globally atomic remote transaction; existing core claim/publish guards still apply.
The host reads back the exact operation marker, including its operation ID. A verified marker
means parked awaiting adjudication, not that a ruling completed; delivery-pending is partial,
and missing, replaced or unreadable read-back is unknown. No success from launch alone.
The renderer consumes the preview on submission and requires a new safe preview for follow-up.
Errors use bounded fixed categories, not transport diagnostics or configured token paths.

The reviewer gate runs as a call (`one_shot_action.review`, #1046): it takes the PR, a
confirmed head and any grant as arguments, and changes nothing process-wide (no stdin or stdout
swap, no signal handler, no process budget or decision recorder; its GitHub budget and recorder
are the call's). Given a confirmed head that the PR has left, nothing is asked and nothing is
written. Desktop still refuses re-review until its confirmed control is wired to that entry
point; `--another-round` and `--reconsider` stay host-CLI only. The drain's
entry point (`one_shot_action.drain_confirmed`, #1047) binds a confirmed queue snapshot: if the
seat's live queue is not that queue, nothing is started, dropped or pruned. Each queued run's
outcome is reported, and a partial or uncertain one is not success. Desktop still refuses drain
until its confirmed control is wired to that entry point. No route invokes a subprocess CLI or
monkeypatches the host to make a button work. Unconfirmed drain completion exits 2, not success. The
selector also refuses corrupt markers and uncertain/quarantined ledger outcomes for escalation.
Existing retry, pause/resume and seat configuration are unchanged. Diaktoros never merges.

Host-recorded incomplete change views block reviewer approval and fixer pushes before writing. So does CI: the broker re-reads the head's check runs and commit statuses and refuses an APPROVE while one has failed or CI cannot be read. The review receipt claim repeats the approval check in its transaction. The same live read that authorizes the write refuses an APPROVE on a PR GitHub reports as conflicting, ending the turn with nothing claimed or posted (#1243). A reviewer may still request changes. A no-write reviewer broker exists for the live selftest: its host-only constructor flag records the proposed verdict and runs authorization reads without a POST. A socket request cannot enable or disable that mode.

Capability consumption and write-ahead records precede external writes. Invalid input can be rejected before consumption; an attempted external write with a lost reply cannot simply be replayed. Sandbox replies contain only acceptance/safe status, not arbitrary GitHub response fields or host exception details. These restrictions do not determine whether review prose is accurate or appropriate.

## Push policy and the PR race

Unattended fixer push is off until explicitly enabled with acknowledgement of the PR race. It must also have been admitted for that run: enabling policy later does not retroactively authorize an old worker or legacy row. The host push-policy lock spans the final policy read and ref operation; disabling policy does not return while an already-authorized push is still in progress. See [commands](commands.md) for operator syntax.

`safe_push._manifest` accepts exactly `base_head`, `message` and `files`, with these restrictions:

- 1–24 unique whole-file entries; each has exactly `path`, `content_b64` and `sha256`.
- Either whole files (up to 64 KiB decoded each, 128 KiB in total, 24 files) or one unified diff against the scoped head (up to 512 KiB decoded, 64 changed paths); valid base64 and matching SHA-256 required.
- A diff is applied by Git to the index of a fresh copy of the exact scoped head (`git apply --cached`: no worktree, no hooks; Git refuses a hunk that does not fit, a path outside the tree and a path beyond a symlink). Every path it changed is then checked with the same rules as a whole file, and only regular files (`100644`/`100755`) may be added, changed or deleted: a symlink, a submodule or any other mode on either side is refused.
- A nonblank, NUL-free commit message of at most 240 UTF-8 bytes before host attribution is appended.
- Relative paths of at most 512 characters with 1–128-character `[A-Za-z0-9_.-]` segments; no empty, `.`/`..` or case-insensitive `.git` segments.
- No top-level `.github` content; no `.gitmodules` or `.gitattributes` segment anywhere; no root `CODEOWNERS` or `docs/CODEOWNERS` (case-insensitive).
- No duplicate path or file/directory conflict. Tracked ancestor files/symlinks, directory replacement and nonregular-file replacement are rejected.

This is an add/replace-file interface, not deletion, renaming, arbitrary Git commands, workflow editing or executable-mode creation. Existing executable regular files retain their mode; new files are plain regular files.

The host uses a fresh bare Git repository and isolated config/environment. Hooks, credential helpers, signing and unapproved transports are disabled. It fetches an advertised ref, verifies the scoped head, constructs a single direct-child commit and pushes with an exact-SHA `--force-with-lease`. A conflict resolution (#303) is the one other shape: the host merges a pinned base commit into the scoped head itself (`git merge-tree`, no worktree), re-checks that the merge is still the tree the resolving turn was given, applies only the seat's resolution on top (every changed path checked as for any push, so no control file), refuses it if a conflict marker is left in a conflicted file, and commits it with exactly the parents (head, base) under the same exact-head lease. Only content conflicts (both sides changed a text file and Git wrote marker blocks) are resolved unattended: a whole-file conflict (a modify/delete, a rename, a binary file) leaves no markers, so it is refused and left to a person, and every conflicted file must be one the seat explicitly rewrote. Changes the base itself brings in are the base's, not the seat's; a base that changed workflow files cannot be pushed by a token without the `workflow` scope, so such a PR is left to a person too. With `review_only_update` on (off by default; `--acknowledge-branch-push` to opt in) the host makes one more kind of write, to a branch the loop does not own: for a review-only author's same-repository PR it merges the base into the head in the same isolated merge and pushes the merge commit, as the account that makes host merge pushes, under an exact-head lease. Only a clean merge with no workflow change is pushed; a real conflict, a whole-file conflict, a workflow change or a fork gets a notice only, and an author push in between fails the lease and overwrites nothing. A published merge cannot be undone by the loop. The push can fire only for a PR GitHub reports as `behind`, which it does only when the base requires branches to be up to date; on a base without that setting the opt-in never pushes and only the notice applies (`doctor` notes it). Issue fixes instead require the new branch to be absent. The lease syntax is not permission to overwrite unrelated history: the old SHA and parent relation are checked.

PR and branch checks are repeated immediately before the mutation; an attempt is durably audited first. Afterward, the host independently reads the ref, rechecks live PR authorization and records the outcome. A published ref with unverified PR state is quarantined (`published_pr_unverified`), not acknowledged as an ordinary successful push. When the git transport errors but the ref read-back shows the new head and the PR is verified at it, the outcome is `published`: the run is still held for the operator, the hold records `published`, and the seat is told the push was published (not that its outcome is unknown). An issue fix's branch push is treated the same way: its hold records `published`, and the seat is told the branch was published and that no PR was opened from that turn.

**Residual race:** GitHub does not atomically couple the PR state/base/draft check to Git receive-pack. A PR can close, become draft or retarget after the final check without changing the leased branch SHA. Post-write checks can detect an unauthorized publication but cannot prevent or undo it. This is why opt-in requires acknowledgement and why unattended push may be unsuitable for a repository.

Blocking control files is also not a guarantee that allowed code is harmless: ordinary source/test files can execute in existing CI workflows with that CI's permissions and secrets. Repository CI policy remains an operator responsibility.

## Inference policy and DirectSDK

`inference_proxy.InferenceCapability` owns authentication and host-selected upstream/model. It enforces bounded request/response framing (8,000,000 and 4,000,000 bytes; every refusal it makes is recorded host-side with its reason), a per-turn model-call quota that follows the seat's agent-step cap (`config.model_calls`: by default 60 steps → 75 calls for the reviewer, 40 → 50 for the adjudicator, 24 → 32 for triage, 80 → 100 for the fixer and issue fixer; settable per seat with `max_steps`, 8–200, never above the proxy's 250-call ceiling) and at most 8 concurrent connections. The turn budget bounds every seat's wall clock regardless. On a reviewer or fixer turn it also appends one host-written message to the model request five steps before that cap, telling the agent to submit what it has verified now; the sandbox never sees it in its own history, and no other seat is nudged (#554). Authentication/framing headers are not accepted from the sandbox; allowed noncredential headers depend on the wire contract. HTTP upstream redirects are not followed. OAuth refresh and its retry happen in the host.

| Wire mode | Local endpoint | Output policy |
|---|---|---|
| `chat_completions` | `/v1/chat/completions` | 16384-token cap; larger limits clamped; malformed/ambiguous limits rejected; no multiple completions |
| `codex_responses` | `/v1/responses` | 16384-token cap; larger limits clamped; malformed limits rejected; background responses rejected |
| `anthropic_messages` | `/anthropic/v1/messages` | 16384-token cap; larger native limits clamped; thinking budget kept below output limit |

**Codex subscription exception:** for the ChatGPT Codex backend, the proxy validates output-cap fields and then removes them because that backend rejects them; it also removes certain unsupported sampling/cache fields and sets `store=false`. Therefore the 16384 figure is not a provider-enforced output-token guarantee on that path. Response bytes, calls and wall-time limits still apply. These mechanisms are not an exact monetary spending cap.

**DirectSDK:** `claude-subscription-directsdk-experimental` uses a host-only process backend behind the chat-completions capability, not native authentication copied into the sandbox. `directsdk_child` loads the chosen host profile's environment, selects the installed provider and native CLI, and sends only an allowlisted environment to the native process. Request data cannot choose its executable, config path, plugin or arbitrary client kwargs.

`directsdk_guard` is installed before provider import/client creation. It validates the reviewed native launch grammar: empty native tools/settings sources, one turn, `dontAsk`, stream-json input/output, strict MCP config, disabled slash commands and session persistence. Private settings/system/tool files and working directory are checked; shell/executable overrides and unknown/repeated options are rejected. The inventory-only MCP script must match the hard-coded SHA-256. A provider script change requires review/update; a mismatched hash fails closed.

The host process reply is bounded to 120 seconds and the proxy response-byte ceiling; disconnect/close terminates the helper, whose handler closes the native client, with process-group kill fallback. This guard is not an OS isolation boundary around the native CLI or a proof about all provider versions. DirectSDK regression tests use disposable fixtures and fakes, not a live subscription login. Keep it labeled experimental and validate the installed combination separately.

## Ledger and ambiguous outcomes

The supervisor commits launch intent before spawning/entering execution and maintains active occupancy for `claimed`, `launching`, `running` and `uncertain` states. Unique delivery/turn keys deduplicate admission. Review receipts, push intents, answers, rulings and issue results establish host-owned write boundaries. `review_receipt.submit` claims before POST, reads the exact returned review ID and re-resolves generation before confirmation.

Expired pre-launch claims can be reclaimed. Ambiguous launches or writes are quarantined; retry logic cannot infer “nothing happened” from a nonzero exit or exception string. Confirmed pre-write failures have bounded backoff/rearm paths; turn-budget kills and policy holds have distinct handling. Uncertain work is not automatically replayed. Operators must inspect GitHub and host records before reconciliation; see [operations](operations.md).

The ledger is local SQLite, not a replicated transaction coordinator. Workers vet host state and fail closed on missing/invalid production ledger/state rather than recreating it themselves. Losing the ledger still loses evidence; local durability assumes the filesystem and trusted host behave as expected.

Notice delivery uses durable claim-before-send. Explicit delivery failure may be retried; crashes around sending/acknowledgement remain ambiguous. Neither broker capabilities nor the outbox establish exactly-once external delivery across all failure cases.

## Limitations and operator responsibilities

- **Host and same-UID trust:** root, malicious same-UID processes, modified trusted code, an unsafe interpreter/venv or provider can defeat the boundary. Mode 0700/0600 is not isolation from that user.
- **Kernel dependence:** Linux bubblewrap/user-namespace support is required. This is not a VM, kernel exploit defense, seccomp policy or separate-UID service.
- **Resource limits are partial:** tmpfs `size=` bounds file data, not inode metadata. The host-bound turn home is not quota-capped; CPU/memory/process consumption is not fully bounded here. The code does not install a systemd cgroup policy, disk quotas, `MemoryMax` or `IOWeight`. Add outer resource controls if needed; do not claim they already exist.
- **Secrets in readable inputs:** source filters are incomplete by design; reviewed files, venv packages and system mounts remain readable. Never stage live secrets there. Inference can transmit repository content to the chosen provider; broker bodies can publish content to GitHub.
- **Semantic risk:** scoped destinations, hash checks and receipts cannot prove a model finding, fix, label or comment is correct. Prompt injection can affect content within permitted authority.
- **Support limits:** exports leave out symlinks, submodules and `.gitmodules` (named as not exported) rather than failing the turn; dependency prefetch is Rust/crates.io-specific. Missing builds must be reported, not portrayed as verified. Root-base review receipts do not establish a verified stacked-parent chain.
- **No automatic merge guarantee:** a review or adjudicator ruling is not a merge transaction. GitHub branch protection, token permissions and repository CI remain separate policies.
- **Tests are evidence, not certification:** local-fake success does not establish live credential permissions, provider behavior, webhook reachability or an independent security audit. Scanner output is version/input-dependent; no fixed finding count is asserted here.

Use [doctor and selftest guidance](troubleshooting.md), [account setup](accounts.md) and the [development lanes](development.md) to check the environment actually in use. Do not weaken user-namespace restrictions globally just to make a test skip disappear without considering the host's security policy.

## Security evidence

Implementation links and targeted tests are collected in the [architecture source map](architecture.md#source-map). Additional adversarial/regression sources:

- [`test_boundary.py`](../tests/test_boundary.py), [`test_sandbox_identity.py`](../tests/test_sandbox_identity.py), [`test_sandbox_limits.py`](../tests/test_sandbox_limits.py): containment and mount/resource boundaries.
- [`test_snapshot_secrets.py`](../tests/test_snapshot_secrets.py), [`test_trusted_fetch.py`](../tests/test_trusted_fetch.py): export filtering, tree/tarball/identity checks.
- [`test_partial_view_no_approve.py`](../tests/test_partial_view_no_approve.py), [`test_ci_gate.py`](../tests/test_ci_gate.py), [`test_push_policy_boundaries.py`](../tests/test_push_policy_boundaries.py), [`test_post_write_quarantine.py`](../tests/test_post_write_quarantine.py), [`test_push_quarantine_regressions.py`](../tests/test_push_quarantine_regressions.py): incomplete views, admission/policy and post-write uncertainty.
- [`test_directsdk_backend.py`](../tests/test_directsdk_backend.py), [`test_directsdk_child.py`](../tests/test_directsdk_child.py), [`test_directsdk_guard.py`](../tests/test_directsdk_guard.py): offline native-adapter contracts; not live subscription evidence.
- [`test_home_guard.py`](../tests/test_home_guard.py), [`test_leakguard_children.py`](../tests/test_leakguard_children.py), [`test_ledger_close.py`](../tests/test_ledger_close.py): test isolation and resource lifecycle.
- [CI workflow](../.github/workflows/ci.yml), [plugin guard wrapper](../.github/scripts/./plugin_guard.py): executed lanes and install-scanner policy. Refer to actual run logs for results, skips and findings.
