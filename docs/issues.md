# Issue triage and issue fixes

Two opt-in features start before a PR: triage chooses labels for a new issue; a maintainer
can hand an issue to the fixer to propose a PR. Neither feature merges code or handles
arbitrary ongoing issue conversations.

## Contents

- [Behavior and boundaries](#what-each-one-does-and-who-can-start-it)
- [Prerequisites](#before-you-start)
- [Enable triage](#turn-on-issue-triage)
- [Enable issue fixes](#turn-on-issue-fixes)
- [Costs and scheduling](#caps-and-costs)
- [Inspect results](#inspect-results)
- [Disable](#turn-it-off)
- [Recovery](#when-it-doesnt-work)
- [Current limits](#current-limits)

Replace quoted angle-bracket placeholders with your values, brackets included.
`--loop` always selects a saved loop ID. `--admin-token` names a mapped hook-admin login,
not a token value. See [commands](commands.md) for the complete CLI reference.

## What each one does, and who can start it

| | Triage | Issue fix |
| --- | --- | --- |
| Trigger | `issues` webhook action `opened` | Action `labeled` with configured fix label |
| Authorization | Author in `triage.authors` | Sender in `triage.maintainers`, author in `triage.authors` |
| Live eligibility | Open issue, not PR, no configured triage label already present | Open issue, not PR, fix label still present, pushes enabled |
| Model environment | Triage profile, empty read-only `/work` | Fixer profile/model/account, base commit checkout |
| Write | Add allowlisted labels and optionally one short comment | One new-branch PR sequence or one cannot-fix comment |
| Principal | Configured triage login, default reviewer login | Fixer login |

The host re-reads eligibility at the gate, before launch and before writing. Issue text
is untrusted data: title is bounded and body clipped at 8000 characters. No model holds
a GitHub credential. Broker policy restricts labels and comment permission, not merely
prompt instructions. Triage never removes labels. If any label from the triage list is
already present, it writes nothing—even if an earlier automation, not a human, added it.

The fix label cannot be a triage label. Only an allowlisted sender's labeling event
triggers a fix; the feature does not inspect whether that sender is physically a human.
Changing/reopening an issue or editing its text does not launch triage. A durable
triage key deduplicates repeat opened deliveries; proven pre-write failures may re-arm,
but recorded write outcomes are never blindly replayed.

## Before you start

1. Have a working review loop and a private `$HERMES_HOME/review-loop-runtime.json`.
   Run [doctor and selftest](operations.md#preflight-doctor).
2. Choose an existing triage Hermes profile. It can be the reviewer profile: triage
   does not require a distinct model profile, but the selected model must resolve.
3. Choose a triage writer with **issues write** permission and a mapped private token
   belonging to that account. It cannot be the read/control account. The default is
   the reviewer login, not the author who opened the issue.
4. Choose trusted issue authors and a bounded label vocabulary. Label names are
   validated (1–50 characters; no commas, braces, backticks or control characters;
   up to 100 configured labels). Create repository labels ahead of time if you need
   predictable colors/descriptions.
5. Choose a mapped login able to manage hooks. The triage hook subscribes to `issues`;
   route installation alone cannot receive GitHub events.

## Turn on issue triage

Preview the complete policy before enabling:

```bash
hermes review-loop triage --loop "<loop-id>" --enable --profile "<triage-profile>" --login "<triage-login>" --author "<trusted-author-login>" --labels bug,docs,question --max-labels 3 --comment off --daily-turns 20 --admin-token "<hook-admin-login>" --dry-run
```

| Option | Meaning |
| --- | --- |
| `--enable` | Write/enable the triage block and route |
| `--profile` | Existing Hermes profile whose model performs triage |
| `--login` | GitHub writer identity; omit to use current/default reviewer identity |
| `--author` | Repeatable trusted issue-author login; a supplied list replaces the previous list |
| `--labels` | Comma-separated allowed labels; supplied vocabulary replaces the previous one |
| `--max-labels` | Maximum selections per turn, 1–10; default 3 |
| `--comment off` | Labels only (default); `on` allows an optional comment, at most 1000 characters |
| `--daily-turns` | Per-loop triage starts per local day; 0 removes the cap |
| `--admin-token` | Mapped account that can create the repository hook |
| `--dry-run` | Validate and print planned settings without changing state |

If the writer is not mapped, add `--token "<triage-login>=<absolute-token-file>"` to the
same command. `--token` is repeatable and refers to files, never literal credentials.

Run the preview command again **without `--dry-run`**. Configuration, route and shim
are installed; with `--admin-token`, the issues hook is created **paused**. Without that
option, install the hook later:

```bash
hermes review-loop apply --loop "<loop-id>" --hooks --admin-token "<hook-admin-login>"
hermes review-loop arm --loop "<loop-id>" --admin-token "<hook-admin-login>"
hermes review-loop doctor --loop "<loop-id>"
hermes review-loop triage --loop "<loop-id>"
```

`--hooks` asks apply to ensure repository hooks; review the other settings apply may
change before running it. `arm` activates **every loop hook**, not just triage, and
verifies signed pings. Doctor checks profile/model, route/hook and credential file;
a local credential check does not prove issues-write permission. Triage with no
`--enable`/`--disable` prints settings without updating them.

From an allowlisted author, open a new issue. Check labels/comment on GitHub and the
ledger below. The model may choose no labels and optionally a comment; `nothing` means neither
labels nor comment. There is no triage observer notice.
A rejected author never reaches the model and costs no triage turn.

## Turn on issue fixes

Triage must already be enabled. This feature **publishes a commit and opens a PR without
another confirmation**, and shares the off-by-default fixer push policy.

```bash
hermes review-loop fixer-push --loop "<loop-id>" --enable --acknowledge-pr-race
hermes review-loop triage --loop "<loop-id>" --enable --fix-label "<fix-label>" --maintainer "<maintainer-login>" --dry-run
hermes review-loop triage --loop "<loop-id>" --enable --fix-label "<fix-label>" --maintainer "<maintainer-login>"
```

Read [push policy](operations.md#first-run) and [security](security.md) first.
`--acknowledge-pr-race` acknowledges host-operator policy and the residual PR-metadata/ref
race, not verified GitHub owner consent. Enabling also permits ordinary PR fixer turns.
`--fix-label` identifies the separate handoff label; create it in the repository so
maintainers can select it. `--maintainer` is repeatable; supplying it replaces the
maintainer list, so include every intended sender. Unspecified existing triage settings
are preserved. `--dry-run` previews without writing.

Test by applying the label from an allowlisted maintainer to an open issue by an
allowlisted author. The gate reads the configured base branch's current commit and
queues one `issue_fixer` turn keyed by issue and base commit. The turn uses the normal
fixer profile and account, not a separate issue-fixer profile.

If a fix is possible, the broker:

1. Records the intent before external writes.
2. Pushes one commit to new branch `review-loop/issue-N` from the captured base SHA.
   The branch must be absent; an existing branch is never overwritten.
3. Opens a PR against the configured base, with `Fixes #N` in its description.
4. Requests the configured reviewer. The opened PR then follows the regular review loop.

The file-write boundary adds/replaces whole files; no deletes, renames or `.github/`
changes. Attribution is added unless disabled. If a fix is not possible, the turn can
post one explanation on the issue instead of opening a PR. It is not guaranteed to
solve the issue or complete all stages of the PR sequence.

The host does not merge. GitHub's `Fixes #N` closing behavior applies when the PR is
merged into the appropriate default-branch context, not merely because a PR was opened.

## Caps and costs

Triage uses `seats.triage` concurrency/budget overrides (default concurrency one), and
`--daily-turns` pacing. Over-cap work waits until local midnight without spending an
error retry. Runtime/model/provider failure may prevent a scheduled turn from starting.
See [configuration](configuration.md) for exact budget inheritance.

Issue fixes are serialized (concurrency one), use the loop-wide `turn_budget_s` (default
900 seconds), and have **no daily cap**. A successful proposal adds the costs of the
ordinary PR review loop. Removing/reapplying the fix label at the same base commit
cannot create a second independent turn. At a new base commit it can create fresh work,
but an existing `review-loop/issue-N` branch still prevents publication. Repeated
labeling is not a safe recovery procedure after a possible write.

## Inspect results

Use the exact ledger path from your active Hermes home. These queries require `sqlite3`
and use read-only mode. Replace `<issue-number>` with a positive integer before running.

```bash
sqlite3 -readonly "<ledger-path>" "SELECT r.id,r.pr,r.state,r.error,t.state,t.error,t.comment_id FROM runs r LEFT JOIN triage_results t ON t.run_id=r.id WHERE r.seat='triage' AND r.pr=<issue-number> ORDER BY r.created DESC;"
sqlite3 -readonly "<ledger-path>" "SELECT r.id,r.pr,r.state,r.error,f.kind,f.state,f.branch,f.pr_number,f.comment_id,f.error FROM runs r LEFT JOIN issue_fixes f ON f.run_id=r.id WHERE r.seat='issue_fixer' AND r.pr=<issue-number> ORDER BY r.created DESC;"
```

`<ledger-path>` is `$HERMES_HOME/state/review-loop-runs.sqlite`. The left join keeps runs
whose broker result does not exist yet. If multiple repos share that issue number,
add a repository predicate using your own value; correlate with the loop's repo.
`r.state` is supervisor state; the result-table state is the broker write stage.
Triage broker acceptance means its result was durably recorded, **not that labels or
a comment definitely appeared**: `_triage` returns acceptance even when the subsequent
live delivery is skipped, denied or uncertain. Inspect `triage_results` and GitHub.
Labels and an optional comment are separate API calls; labels can land while the
comment outcome remains unknown.

| Triage result | Meaning |
| --- | --- |
| `recorded`, `posting` | Durable record / external write in progress; investigate stale stage |
| `posted` | Applied result recorded |
| `skipped` | Live eligibility changed or a triage label was already present |
| `nothing` | No labels and no comment selected; no write |
| `denied` | Authorization refused; inspect error |
| `uncertain` | Some write may have landed; never replay |

Issue fixes record `recorded`, `pushed`, `opened`, `requested` for PR stages, or `posted`
for the comment path; `denied` and `uncertain` expose refused/unknown outcomes. A PR
can exist even though requesting review failed. Inspect its branch, PR and request
separately; a nonterminal record is not proof that nothing was published.

## Turn it off

```bash
hermes review-loop triage --loop "<loop-id>" --disable --admin-token "<hook-admin-login>"
hermes review-loop triage --loop "<loop-id>" --enable --fix-label ''
```

The first command disables triage **and issue fixes**, removing configuration, route
and shim, and with hook-admin permission removing the repository issues hook. Without
`--admin-token`, the hook stays and receives 404s; remove it on GitHub. The second
command is an alternative: an empty `--fix-label` disables only issue fixes and preserves
triage. Already posted labels/comments, opened PRs, ledger history, token mappings,
`seats.triage` settings and fixer push permission remain. Revoke push permission separately
with `fixer-push --loop "<loop-id>" --disable`. Do not assume disabling reverses an
already accepted write or stops an in-flight child.

## When it doesn't work

- [New issue has no labels](troubleshooting.md#an-issue-opened-and-nothing-was-labelled)
- [Fix label produced no PR](troubleshooting.md#a-fix-label-was-applied-and-no-pr-came)
- [Uncertain/post-write recovery](troubleshooting.md#a-run-says-uncertain)

No run row usually means the gate rejected the event or could not enqueue; verify
runtime, hook delivery and exact author/sender. If durable work exists, inspect it
before redelivery. Proven pre-write `failed`/`waiting` work can use `retry --pr` with
the issue number; `--seat triage` or `--seat issue_fixer` restricts the retry to that
seat's eligible problem runs at the selected ledgered head/base. Superseded cancellations,
uncertain runs and reconciled runs cannot be retried. **Any** `triage_results` or
`issue_fixes` row prohibits re-arm, even if its state is `nothing`, `skipped` or `denied`;
other review/push/ruling write evidence also prohibits replay. Inspect the queries above
and actual GitHub state rather than treating a failed supervisor row as proof of no write.

An intentional new issue-fix attempt also requires remote `review-loop/issue-N` to be
absent. Inspect the earlier run, branch, PR, comments and review request first. Only
when you have established a safe new attempt—not an ambiguous or already published
result—deliberately remove that inspected remote branch, then have an authorized
maintainer reapply the label for eligible fresh work (a new base for a distinct durable
turn), or use `retry` for an eligible pre-write run. No branch is automatically deleted
or overwritten. Deleting a branch cannot make a recorded/uncertain run retryable;
never delete receipts or safety history to bypass this boundary.

## Current limits

- Trace supports `pull_request` and `pull_request_review`, **not `issues`**.
- Explain evaluates PRs, not issues; use it on the PR after a successful issue fix.
- No observer events for triage/issue fixes. Failed isolated runs may reach the
  watchdog operator outbox; absence of chat output proves nothing about issue success.
- Status lacks a dedicated triage-settings/pacing summary; use `triage --loop` and
  ledger queries. Generic run diagnostics are not a complete issue-results view.
- Triage reads one bounded issue without repository code or a duplicate-issue search feed.
- Fixes can only add/replace files through the broker and cannot overwrite the deterministic
  branch. No automated reconciliation of a partial push/open/request sequence exists.
- Adjudicator/triage gate failures are reported but are not automatically re-driven like
  reviewer/fixer gate failures. Use repository Recent Deliveries only after inspecting state.
