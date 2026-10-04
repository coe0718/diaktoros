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

The review-loop boundary is not a general sandbox for every Hermes gateway agent. Only turns launched through the contained path receive this boundary. A same-UID host process is not isolated by the fetcher or file modes; a malicious host process can interfere with trusted state. The native DirectSDK helper is host-side trusted code, not a second bubblewrap-contained agent.

## Sandbox and export policy

`contained.command` rejects host networking and uses `bwrap --unshare-all`, `--die-with-parent` and `--new-session`. It has an allowlisted mount layout, not a caller-provided arbitrary bind list:

- System executable/library trees, the configured Python runtime/venv, staged Hermes source and Rust toolchain are read-only.
- `/etc` is staged with one user/group entry; only `/etc/alternatives` is additionally bound read-only. The host's ordinary `/etc` is not mounted wholesale.
- `/proc` and a private `/dev` are created; root and `/dev` are remounted read-only after mounting.
- Writable `/work` is a tmpfs populated from a read-only export. Adjudicator/triage `/work` is read-only; `/target` is a separate sized build target in that case.
- `/tmp` is a sized tmpfs; Cargo registry caches are read-only mounts and `CARGO_NET_OFFLINE=true` is always set.
- `/home/agent` is a writable, private per-turn **host bind**, not a disk-quota-enforced tmpfs.
- The only broker/inference directories mounted into the sandbox contain their respective socket. Staged review diff and client files are read-only.

The parent environment is rebuilt from scratch. The sandbox model configuration uses a placeholder credential for its local bridge, not a real upstream token. `contained._run` caps each captured stdout/stderr stream at 256 KiB, bounds wall time and kills the process group on timeout, output overflow or other parent-side failure.

Default tmpfs data caps are 2 GiB scratch and 8 GiB checkout/build target. `REVIEW_LOOP_SCRATCH_SIZE_GIB` and `REVIEW_LOOP_CHECKOUT_SIZE_GIB` accept integer GiB values from 1 through 1024; invalid values are reported and ignored in favor of defaults. These are host environment settings inherited by workers, not per-loop permission controls.

### Two export checks, with different purposes

**Hermes source:** `trusted_turn` exports permitted regular blobs from committed `HEAD`, verifies object hashes and bounds the snapshot. Hidden/excluded paths and credential-container names are omitted; credential-shaped names/content are additionally filtered where that will not break imported code. Code/templates are deliberately not removed merely because they contain credential-shaped text. `exported_secrets` reports such code text as advisories. This is a containment aid, **not a complete secret scanner**. Keep the source and venv secret-free.

**Reviewed repository:** `trusted_fetch` verifies distinct reader/reviewer/fixer mappings and actual principals, checks live repository/ref/head, validates tree metadata and hashes tarball files against the Git blob IDs. It rejects unsafe paths, `.git`, `.gitmodules`, symlinks, submodules, devices, hard links, duplicate/extra/missing files and truncated trees. Its 10,000-entry and 100-MiB bounds do not mean every repository is supported. Tarball redirects are manually limited to one HTTPS hop to `codeload.github.com`, without forwarding the reader's Authorization header; further redirects are refused.

Repository export is an integrity check, not sanitization of source content. Any secret already committed to an accepted repository file is readable by the sandbox and may reach inference or a published body.

## Scoped write policy

The Unix socket identifies one host-created `RunScope`: repository, number, head, branch, role and host run metadata. The sandbox cannot supply a replacement URL, token, principal, reviewer or destination. Unknown request fields, wrong roles/operations, oversized frames and invalid payloads are refused. General REST frames are limited to 16 KiB and text bodies to 12 KiB; fixer/issue-fixer manifest frames have a separate 196-KiB ceiling.

For ordinary PR writes, `broker.authorize` verifies:

1. The repository equals the configured repository; PR number and SHA have the expected types/formats.
2. The role/operation pair is permitted.
3. Explicit reader/reviewer/fixer mappings resolve to distinct token files and distinct verified GitHub principals (`/user`).
4. The live PR has the exact number, is open and explicitly non-draft, has the configured base repository/branch and scoped head/branch, and is not a fork head.
5. Fixer writes additionally require an allowlisted PR author and a current effective `CHANGES_REQUESTED` review where required. The post-push handoff checks the new head without requiring a verdict that cannot yet exist there.

| Role | Accepted effect | Important restriction |
|---|---|---|
| `reviewer` | One review, `APPROVE` or `REQUEST_CHANGES`, with nonempty body | `COMMENT` is not a verdict; commit ID is pinned; production uses a host receipt |
| `fixer` | One bounded push followed by one review request; optional answers comment attached to the handoff | Normally requires confirmed push first; host policy is reloaded at write time |
| Partial-view `fixer` | Answers-only comment | Cannot push or request review of a nonexistent new push |
| `adjudicator` | One durable `ACCEPT`, `REJECT` or `RESPEC` ruling | Optional notice/comment delivery is separate; no code edits, push or merge authority |
| `triage` | Configured labels, at most configured `max_labels`, and optional allowed comment | Live open issue, allowlisted author and triage policy are rechecked |
| `issue_fixer` | New branch/PR/review-request sequence, or one explanation comment | Live issue/fix-label/author/maintainer policy; unattended issue fixing must be enabled |

Host-recorded incomplete change views block reviewer approval and fixer pushes before writing. The review receipt claim repeats the approval check in its transaction. A reviewer may still request changes. A no-write reviewer broker exists for the live selftest: its host-only constructor flag records the proposed verdict and runs authorization reads without a POST. A socket request cannot enable or disable that mode.

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

The host uses a fresh bare Git repository and isolated config/environment. Hooks, credential helpers, signing and unapproved transports are disabled. It fetches an advertised ref, verifies the scoped head, constructs a single direct-child commit and pushes with an exact-SHA `--force-with-lease`. Issue fixes instead require the new branch to be absent. The lease syntax is not permission to overwrite unrelated history: the old SHA and parent relation are checked.

PR and branch checks are repeated immediately before the mutation; an attempt is durably audited first. Afterward, the host independently reads the ref, rechecks live PR authorization and records the outcome. A published ref with unverified PR state is quarantined (`published_pr_unverified`), not acknowledged as an ordinary successful push.

**Residual race:** GitHub does not atomically couple the PR state/base/draft check to Git receive-pack. A PR can close, become draft or retarget after the final check without changing the leased branch SHA. Post-write checks can detect an unauthorized publication but cannot prevent or undo it. This is why opt-in requires acknowledgement and why unattended push may be unsuitable for a repository.

Blocking control files is also not a guarantee that allowed code is harmless: ordinary source/test files can execute in existing CI workflows with that CI's permissions and secrets. Repository CI policy remains an operator responsibility.

## Inference policy and DirectSDK

`inference_proxy.InferenceCapability` owns authentication and host-selected upstream/model. It enforces bounded request/response framing (1,000,000 and 4,000,000 bytes), a per-turn model-call quota that follows the seat's agent-step cap (`config.model_calls`: by default 60 steps → 75 calls for the reviewer, 40 → 50 for the adjudicator, 24 → 32 for triage, 80 → 100 for the fixer and issue fixer; settable per seat with `max_steps`, 8–200, never above the proxy's 250-call ceiling) and at most 8 concurrent connections. The turn budget bounds every seat's wall clock regardless. Authentication/framing headers are not accepted from the sandbox; allowed noncredential headers depend on the wire contract. HTTP upstream redirects are not followed. OAuth refresh and its retry happen in the host.

| Wire mode | Local endpoint | Output policy |
|---|---|---|
| `chat_completions` | `/v1/chat/completions` | Default 4096 tokens; over-cap/ambiguous limits rejected; no multiple completions |
| `codex_responses` | `/v1/responses` | Default 16384 tokens; over-cap limits rejected; background responses rejected |
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
- **Support limits:** exports reject symlink/submodule trees; dependency prefetch is Rust/crates.io-specific. Missing builds must be reported, not portrayed as verified. Root-base review receipts do not establish a verified stacked-parent chain.
- **No automatic merge guarantee:** a review or adjudicator ruling is not a merge transaction. GitHub branch protection, token permissions and repository CI remain separate policies.
- **Tests are evidence, not certification:** local-fake success does not establish live credential permissions, provider behavior, webhook reachability or an independent security audit. Scanner output is version/input-dependent; no fixed finding count is asserted here.

Use [doctor and selftest guidance](troubleshooting.md), [account setup](accounts.md) and the [development lanes](development.md) to check the environment actually in use. Do not weaken user-namespace restrictions globally just to make a test skip disappear without considering the host's security policy.

## Security evidence

Implementation links and targeted tests are collected in the [architecture source map](architecture.md#source-map). Additional adversarial/regression sources:

- [`test_boundary.py`](../tests/test_boundary.py), [`test_sandbox_identity.py`](../tests/test_sandbox_identity.py), [`test_sandbox_limits.py`](../tests/test_sandbox_limits.py): containment and mount/resource boundaries.
- [`test_snapshot_secrets.py`](../tests/test_snapshot_secrets.py), [`test_trusted_fetch.py`](../tests/test_trusted_fetch.py): export filtering, tree/tarball/identity checks.
- [`test_partial_view_no_approve.py`](../tests/test_partial_view_no_approve.py), [`test_push_policy_boundaries.py`](../tests/test_push_policy_boundaries.py), [`test_post_write_quarantine.py`](../tests/test_post_write_quarantine.py), [`test_push_quarantine_regressions.py`](../tests/test_push_quarantine_regressions.py): incomplete views, admission/policy and post-write uncertainty.
- [`test_directsdk_backend.py`](../tests/test_directsdk_backend.py), [`test_directsdk_child.py`](../tests/test_directsdk_child.py), [`test_directsdk_guard.py`](../tests/test_directsdk_guard.py): offline native-adapter contracts; not live subscription evidence.
- [`test_home_guard.py`](../tests/test_home_guard.py), [`test_leakguard_children.py`](../tests/test_leakguard_children.py), [`test_ledger_close.py`](../tests/test_ledger_close.py): test isolation and resource lifecycle.
- [CI workflow](../.github/workflows/ci.yml), [plugin guard wrapper](../.github/scripts/./plugin_guard.py): executed lanes and install-scanner policy. Refer to actual run logs for results, skips and findings.
