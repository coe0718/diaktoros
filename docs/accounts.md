# Accounts and tokens

[Documentation map](README.md) · Previous: [Concepts](concepts.md) · Next: [Getting started](getting-started.md)

Prepare GitHub identities before installing a loop. A **PAT** (personal access token) acts as its issuing GitHub account. A **token file** contains one PAT; the loop stores only its path. A Hermes profile chooses inference, not GitHub identity.

## Choose the accounts

The reader, reviewer and fixer must be three distinct GitHub accounts with three distinct files. If an adjudicator posts rulings as comments, that login/file must be a fourth. This is the source's **four-identity rule**; the fourth role is optional. Separate tokens for the same account are not separate principals. Symlink/hardlink aliases of the same file do not create distinct files.

| Role | Account placeholder | Purpose | Configuration |
| --- | --- | --- | --- |
| Reader | `"<reader-login>"` | Trusted PR/review/ref/hook reads; normally the repository owner on a user-owned repo | `--read-token` plus its file mapping |
| Reviewer | `"<reviewer-login>"` | Post bounded review verdicts | Reviewer allowlist/seat plus its file mapping |
| Fixer | `"<fixer-login>"` | Publish authorized fixes and request review; issue fixer reuses it | Fixer allowlist/seat plus its file mapping |
| Adjudicator comment account (optional) | `"<adjudicator-login>"` | Post an isolated ruling as a PR comment | Adjudicator route, distinct profile, login and file mapping |
| Hook admin | `"<admin-login>"` or reader | Create/arm/pause/delete repository hooks | `--admin-token`; file must already be mapped |
| Triage (optional) | Reviewer by default | Apply bounded issue labels, optionally comment | Issue configuration; never the reader |

The owner-reader often also serves as hook admin. That PAT then has hook-write permission even though gate decisions use it for reads. “Reader” describes its role, **not an assertion that the token is always read-only**. For strict read-only reader credentials, use a separately mapped admin where supported, or create/toggle hooks manually.

A distinct adjudicator profile can rule without a fourth GitHub token: its ruling is recorded and reported to the operator, not posted as a comment. An observer feed uses a chat destination, not a GitHub identity or review seat.

## Create accounts and grant repository access

1. Choose logins for the roles above. Use dedicated automation accounts with narrow repository access where possible. Check GitHub's current account/automation terms before creating additional accounts.
2. Create each needed account in a separate browser session. Use its own email, strong password and required 2FA; store recovery codes securely. Do not share passwords, PATs or verification codes in chat.
3. As repository administrator, open the repository's **Settings → Collaborators / access management** and invite the seat accounts.
4. Sign in as each invited account and **accept the invitation**. An unaccepted invitation does not grant access.
5. For an organization, assign roles that support the required operations and obtain any token approval/SSO authorization required by its policy.

| Repository ownership | Practical access model |
| --- | --- |
| Personal/user-owned | Seats are collaborators. A read-only collaborator role is not available; owner commonly fills reader/admin roles. Do not assume a collaborator can administer hooks |
| Organization-owned | Dedicated reader can use Read; reviewer/fixer need sufficient access for reviews/contents. Hook administration requires an authorized role, independently of the seat's token scope |

Repository role, token type/scope and organization policy all matter. A permission selected on a PAT cannot grant access that its account lacks.

## Choose PAT type and permissions

The plugin uses token files and Python REST calls; **`gh auth login` is not required** and is not the credential source for gates or broker writes.

| Role | User-owned repository | Fine-grained permissions when supported for an organization/resource owner |
| --- | --- | --- |
| Owner-reader | Fine-grained selected-repo token is appropriate | `contents: read`, `pull_requests: read`, `repository_hooks: read` |
| Reviewer | Collaborator seats commonly require classic `repo` | `pull_requests: write` |
| Fixer | Classic `repo` for collaborator seat | `contents: write`, `pull_requests: write` |
| Adjudicator comment login | Classic `repo` for collaborator | `pull_requests: write` |
| Hook admin | Classic `repo` or `admin:repo_hook`, or fine-grained owner token | `repository_hooks: write` (Webhooks: Read and write) |
| Reader with issue automation | Add issue-reading access | Add `issues: read` |
| Triage account | Classic `repo` covers issue writes | Add `issues: write` |
| Issue fixer | Classic `repo` covers issue/branch/PR operations | `contents: write`, `pull_requests: write`, `issues: write` |

Fine-grained PATs have contribution restrictions for repository/outside collaborators; moving a repo into an organization does not automatically make every outside collaborator eligible. Verify account membership and current GitHub token policy. Dedicated accounts with classic `repo` should have access only to repositories the loop needs: classic scope extends to every repo that account can access.

GitHub UI labels are **Contents**, **Pull requests**, **Webhooks** and **Issues**; Metadata read is normally implicit for fine-grained PATs. A reader also used as hook admin needs Webhooks **Read and write**, not just Read-only. Classic `admin:repo_hook` allows hook deletion for rollback/removal; the narrower `write:repo_hook` is not sufficient for that lifecycle. No `workflow`, `admin:org` or `delete_repo` scope is required by the loop; protected-path changes remain a human task.

[Operations](operations.md) contains the detailed role/scopes rationale. Organization approval, SSO and branch protections can still reject an operation after token creation; validate them with the actual repository.

### Create each token as its own account

1. Sign in as the role's account; verify the login before creating a token.
2. Open **Settings → Developer settings → Personal access tokens**.
3. For **fine-grained**: generate a token, choose the correct resource owner, select only the target repository and set the permissions above. Obtain organization approval if needed.
4. For **classic**: generate a classic token with the required scope (normally `repo` for collaborator seats). Authorize SSO if your organization requires it.
5. Choose an expiration you can operationally manage; schedule rotation before expiry.
6. Copy the once-shown PAT directly into its private token file using your local editor/secure credential workflow. Do not put the value in shell history, a command argument, a screenshot or a checked-in file.

## Store token files privately

These examples assume the operator Hermes home is `~/.hermes`; change paths consistently for another home. Replace the quoted account placeholders in filenames.

```bash
(umask 077; mkdir -p "$HOME/.hermes/keys")
(umask 077; "$EDITOR" "$HOME/.hermes/keys/<reader-login>-pat")
(umask 077; "$EDITOR" "$HOME/.hermes/keys/<reviewer-login>-pat")
(umask 077; "$EDITOR" "$HOME/.hermes/keys/<fixer-login>-pat")
chmod 600 "$HOME/.hermes/keys/<reader-login>-pat" \
  "$HOME/.hermes/keys/<reviewer-login>-pat" \
  "$HOME/.hermes/keys/<fixer-login>-pat"
```

| Shell element | Explanation |
| --- | --- |
| `umask 077` | Newly created files/directories deny group/other access; does not repair permissions of existing files |
| `mkdir -p` | Create the token directory if missing; use `chmod 700` on an existing directory if necessary |
| `"$HOME/…"` | Expands to an absolute path while preserving spaces and literal placeholder angle brackets |
| `"$EDITOR"` | Your chosen local editor executable; set it first. If it contains arguments, invoke the editor explicitly rather than treating the whole value as one executable |
| `chmod 600` | Set only the named account files to owner read/write, not unrelated credential files |

Paste one PAT per file, with no login, JSON or shell assignment; a trailing newline is fine. For an optional adjudicator/admin, create its own file by the same procedure. Token-path settings validate absolute paths (after `~` expansion), existing regular files, ownership, restrictive permissions and non-empty contents; a symlink's target must meet the checks. Supply mode 600 for predictable behavior.

Do **not** copy GitHub tokens into seat `.env` files or put PAT values into plugin settings, loop JSON or runtime JSON. `GH_TOKEN` in a profile is not a substitute for the broker's mapped file. The installed host plugin/broker reads secrets when needed; “not mounted into the sandbox” does not mean the trusted host never opens a token file.

## Map accounts to the loop

For a first installation, follow the full [getting-started examples](getting-started.md), not a partial `init` command missing repository/profile/host arguments.

| CLI surface | Login | Token path |
| --- | --- | --- |
| `setup` reader | `--read-token "<reader-login>"` | `--read-token-file "<absolute-reader-token-path>"` |
| `setup` reviewer | `--reviewer "<reviewer-login>"` | `--reviewer-token "<absolute-reviewer-token-path>"` |
| `setup` fixer | `--fixer "<fixer-login>"` | `--fixer-token "<absolute-fixer-token-path>"` |
| `setup` separate admin | `--admin-token "<admin-login>"` | `--admin-token-file "<absolute-admin-token-path>"`; unused if login already maps to reader/seat |
| `init` | `--read-token`, `--reviewer`, `--fixer` | Repeated `--token "<login>=<absolute-token-path>"` |
| `init` optional adjudicator | `--adjudicator-login "<adjudicator-login>"` plus adjudicator route/profile | Its own `--token` mapping |

`--token` is a mapping **argument**, never the secret itself. The desktop form supplies reviewer/fixer/adjudicator login and token-path defaults; it has no reader field. Explicit flags normally win over defaults, but invalid stored token-path settings are still refused. An adjudicator comment login only works when an adjudicator route is configured.

### Change the reader or add an admin later

```bash
hermes review-loop set --loop ID \
  --read-token "<reader-login>" \
  --token "<reader-login>=$HOME/.hermes/keys/<reader-login>-pat"
hermes review-loop set --loop ID \
  --token "<admin-login>=$HOME/.hermes/keys/<admin-login>-pat"
```

| Option | Meaning |
| --- | --- |
| `--loop ID` | Select installed loop |
| `--read-token` | Assign a distinct reader; also repairs a loop lacking explicit `read_token` |
| `--token LOGIN=PATH` | Map that reader in the same command, or an extra hook admin; cannot move a reviewer/fixer seat's file through `set` |

Seat logins/file paths move through plugin settings and `apply`; preview first with `apply --loop ID --dry-run`. A changed seat path is an identity change and is refused while that seat is busy unless explicitly overridden. The running turn keeps its starting identity. [Settings](settings.md) and [configuration](configuration.md) explain this staging.

If the reader is changed, default hook-editing commands now act as the new reader. Give it hook write or explicitly pass a mapped `--admin-token "<admin-login>"` to each hook operation. Naming an admin does not bypass repository access checks.

## Verify files, accounts and access

```bash
hermes review-loop doctor --loop ID
hermes review-loop selftest --loop ID --no-model
```

| Check | Evidence | Does not establish |
| --- | --- | --- |
| `doctor` credential/token checks | Mapped file exists, private/non-empty; reader/seat/configuration wiring | Actual identity of every PAT or successful future broker write |
| `selftest --no-model` GitHub identities | `/user` resolves each mapped role to the expected distinct principal; reader can read repository | A model completion; future token validity; every organization/branch policy |
| Hook lines | Matching live hooks and armed/paused state where listing is permitted | Unknown is not paused; seat access does not imply hook administration |
| Production broker | Revalidates principals and live situation before bounded writes | Atomic coupling of PR metadata and Git ref publication |

`--loop ID` selects the installed loop. `--no-model` skips inference requests but still performs GitHub reads and sandbox/runtime checks. It is not offline. Fix every mismatch/refusal before arming.

### Common refusals and their fixes

| Symptom | Action |
| --- | --- |
| Reader is also a seat | Choose a third account/file and change reader with the command above |
| Files shared, including aliases | Give each required identity its own file; verify actual principals too |
| Token belongs to a different login | Replace contents with the expected account's PAT, or stage the intended identity change |
| File missing/empty/public/relative | Fix path/content/ownership; use absolute path and mode 600 |
| `/user` authentication fails | Check expiry/revocation, then replace PAT for that account |
| Repository inaccessible | Accept invitation, choose correct resource owner/repo, check approval/SSO and permissions |
| Hooks unknown or edit refused | Verify hook-admin account/permission and reader hook-read access; pass explicitly mapped admin |
| Write still refused | Inspect exact-head eligibility, policy and scope; do not weaken identity checks to make a test green |

See [troubleshooting](troubleshooting.md) for exact diagnostic messages and recovery.

## Rotate or revoke

For ordinary rotation, keep the same path and replace its contents with a new PAT from **the same account**. Loop config/settings/routes name the path, so no `apply` is needed when identity/path stays unchanged.

1. Create the replacement with the required repository access, scopes and policy approvals.
2. Replace the existing file privately and enforce mode 600.
3. Run `selftest --loop ID --no-model` to check the mapped principal/access.
4. Revoke the old PAT after the replacement is verified. For a suspected leak, revoke first even if that causes downtime.

Token files are read at use time; running work can encounter either credentials or an authentication gap depending on when it calls the host broker. Rotation is not an atomic transaction with all in-flight requests. Do not claim verification proves an already-running write was undone. A failed pre-write run may be retried; an ambiguous write must be reconciled rather than replayed.

## Least privilege and remaining risk

Read-only fine-grained reader credentials limit compromise to readable repo data; adding hook write also permits webhook mutation. Classic `repo` exposes every repository accessible to the issuing account. A user-owned collaborator reviewer/adjudicator may have push authority even though the broker exposes no push to that seat.

The defense is both narrow account access and the [host/sandbox/broker boundary](security.md): PATs never enter the seat sandbox, and bounded host operations decide how they can be used. Installed plugins remain trusted host code. Fine-grained permissions are useful where supported, not a replacement for isolation, distinct identities or the explicit fixer-push decision.

Continue with [Getting started](getting-started.md), or return to the [documentation map](README.md).
