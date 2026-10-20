# Issue triage and issue fixes

Two opt-in features start before a PR: triage chooses labels for a new issue; a maintainer
can hand an issue to the fixer to propose a PR. Neither feature merges code or handles
arbitrary ongoing issue conversations.

## Contents

- [Behavior and boundaries](#what-each-one-does-and-who-can-start-it)
- [Prerequisites](#before-you-start)
- [Enable triage](#turn-on-issue-triage)
- [Enable issue fixes](#turn-on-issue-fixes)
- [Issues the reviewer files](#issues-the-reviewer-files)
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

1. Have a working review loop and a private `$HERMES_HOME/diaktoros-runtime.json`.
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
hermes dk triage --loop "<loop-id>" --enable --triage-profile "<triage-profile>" --login "<triage-login>" --author "<trusted-author-login>" --labels bug,docs,question --max-labels 3 --comment off --daily-turns 20 --admin-token "<hook-admin-login>" --dry-run
```

| Option | Meaning |
| --- | --- |
| `--enable` | Write/enable the triage block and route |
| `--triage-profile` | Existing Hermes profile whose model performs triage |
| `--login` | GitHub writer identity; omit to use current/default reviewer identity |
| `--author` | Repeatable trusted issue-author login; a supplied list replaces the previous list |
| `--labels` | Comma-separated allowed labels; supplied vocabulary replaces the previous one |
| `--max-labels` | Maximum selections per turn, 1–10; default 3 |
| `--comment off` | Labels only (default); `on` allows an optional comment, at most 1000 characters |
| `--daily-turns` | Per-loop triage starts per local day; 0 removes the cap |
| `--admin-token` | Mapped account that can create the repository hook |
| `--dry-run` | Validate and print planned settings without changing state |

If the writer is not mapped, add `--token "<triage-login>=<absolute-token-file>"` to the
same command. `--token` is repeatable and refers to files, never literal credentials. It applies the same
checks as `set --token`: an absolute path to a regular file you own that group/other cannot
read (`chmod 600`), and never a seat or the reader login (their files move through the plugin
settings, `apply` and `set --read-token`). A refusal writes nothing.

Run the preview command again **without `--dry-run`**. Configuration, route and shim
are installed; with `--admin-token`, the issues hook is created **paused**. Without that
option, install the hook later:

```bash
hermes dk apply --loop "<loop-id>" --hooks --admin-token "<hook-admin-login>"
hermes dk arm --loop "<loop-id>" --admin-token "<hook-admin-login>"
hermes dk doctor --loop "<loop-id>"
hermes dk triage --loop "<loop-id>"
```

`--hooks` asks apply to ensure repository hooks; review the other settings apply may
change before running it. `arm` activates **every loop hook**, not just triage, and
verifies signed pings. Doctor checks profile/model, route/hook and credential file;
a local credential check does not prove issues-write permission. Triage with no
`--enable`/`--disable` prints settings without updating them.

From an allowlisted author, open a new issue. Check labels/comment on GitHub and the
ledger below. The model may choose no labels and optionally a comment; `nothing` means neither
labels nor comment. The observer's `triaged` notice reports every result, `nothing` included; `fixing`, `fixed` and `failed` cover issue fixes (see [observer](observer.md)).
A rejected author never reaches the model and costs no triage turn.

## Turn on issue fixes

Triage must already be enabled. This feature **publishes a commit and opens a PR without
another confirmation**, and shares the off-by-default fixer push policy.

```bash
hermes dk fixer-push --loop "<loop-id>" --enable --acknowledge-pr-race
hermes dk triage --loop "<loop-id>" --enable --fix-label "<fix-label>" --maintainer "<maintainer-login>" --dry-run
hermes dk triage --loop "<loop-id>" --enable --fix-label "<fix-label>" --maintainer "<maintainer-login>"
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
2. Pushes one commit to new branch `diaktoros/issue-N` from the captured base SHA.
   The branch must be absent; an existing branch is never overwritten.
3. Opens a PR against the configured base, with `Fixes #N` in its description.
4. Requests the configured reviewer. The opened PR then follows the regular review loop.

The file-write boundary adds/replaces whole files; no deletes, renames or `.github/`
changes. Attribution is added unless disabled. If a fix is not possible, the turn can
post one explanation on the issue instead of opening a PR. It is not guaranteed to
solve the issue or complete all stages of the PR sequence.

The issue is a work order, not a confirmed bug: the fixer checks the claim against the code
first, and comments instead of "fixing" a claim that does not hold.

### When the fixer asks a question or needs a plan

An issue-fix turn may answer on the issue instead of opening a PR: one question, or a plan. Each
ask costs a fixer turn, an issue gets at most three (questions and plans together), and an open
ask blocks another until it is answered.

- A **question** resumes on the first comment after it from a loop maintainer
  (`triage.maintainers`). Anyone else's reply is ignored.
- A **plan** resumes only on a comment that is exactly `/loop approve` (case-insensitive).
- After the reply, the watchdog sweep queues a fresh fix turn from the base branch. Until then,
  `status` and `explain` say the issue is waiting for a maintainer.

An issue is **plan-first** when it carries the `plan-first` label or its body is longer than
4,000 characters. It must have an approved plan before any PR: the broker refuses `open_pr` until
a maintainer posts `/loop approve`, and the fixer's prompt says so. The approval is remembered per
issue, so later fix turns may publish.

The host does not merge. GitHub's `Fixes #N` closing behavior applies when the PR is
merged into the appropriate default-branch context, not merely because a PR was opened.

### Reproduction proof

The fixer names its reproduction test(s) with `open_pr --repro module.Class.test_name ...`
(unittest ids). Before the write, the host runs only those tests in the sandbox against the
original head with the fix's test files applied (they must fail) and against the fix (they must
pass). The result is stored in the ledger (`repro_proofs`) and the reviewer is shown it as a host
fact: `PROVEN`, `WEAK` (a named test passes on the original head, so it does not cover the fix),
`UNPROVEN`, `NONE NAMED`, or `NOT RUN`.

**Hunk check (#1043).** When the fix passes its named tests, the host also reverts each changed
hunk of its non-test Python source **on its own** and runs the named tests again. A hunk whose
revert still passes them all is **unguarded**: a behaviour change no test notices. The reviewer
and the shadow see a guarded, unguarded and not-checked count, and the file and lines of the
first 20 unguarded hunks (the rest are counted). They're told to treat an unguarded behaviour change as a missing test, unless the hunk
changes no behaviour.

The check is bounded:
- source means every changed `.py` file outside test paths (`tests/`, `test/`, `spec/`, `test_*`,
  `*_test.*`) and hidden directories, wherever it lives in the repository;
- comment- and blank-only hunks are skipped;
- a source file the fix deletes is checked too, as one hunk, labelled by its original lines;
- the largest 12 hunks are checked, inside the fixer's turn; no new revert starts after 240 s, so the
  check ends within that plus one run's own sandbox timeout (300 s), plus, when no test is named,
  the first run of the changed tests that comes before it;
- when the fixer names no reproduction test, the test files the fix changed are run instead; with
  none of those either, every source hunk is listed unguarded;
- `not checked`, never "guarded", covers a hunk past the cap or the budget, a revert that stops
  the test module importing, and a run that didn't finish (a timeout). One hunk's run failing
  never discards the rest of the proof;
- compiled bytecode is cleared before each run, so a same-size revert is never run as the fix's
  stale code.

`hermes dk report` totals the unguarded hunks across the window's issue fixes.

What cannot fire: the proof never blocks the PR. It runs only where the host has a sandbox
(otherwise `NOT RUN`), only for Python `unittest` ids, and covers the fix commit, not later
pushes to the PR. It is shown to the reviewer of an issue-fix PR only.

### When a hand-off waits for a PR

A fix turn starts from the base branch, so the hand-off waits when the work isn't there yet.
The wait is posted once as a comment, and the watchdog sweep queues the turn when the PR merges:

- **An issue the reviewer filed** waits for the PR it came from while that PR is open: "waiting
  for #N to merge — this finding is about code only on that branch". If that PR closes without
  merging, the finding is moot and the hand-off is dropped (the label stays).
- **An issue a person opened** waits only for an open PR it *declares* it depends on: "Depends
  on #N", "Blocked by #N", "Builds on #N", "Follow-up to #N" or "Requires #N" (also `PR #N`,
  `owner/repo#N` or the PR's URL). The comment quotes those words. A PR the issue only mentions,
  as an example or a link, is never waited for (#686). If the PR closes without merging, the
  hand-off is dropped with a comment saying so.

- **An issue you made depend on another** (`hermes dk depends --issue B --on A`, #671) waits
  until A's implementation is merged into the base: A's own fix PR merged there, or A closed by
  a commit the base contains. Closing A any other way keeps B waiting until you waive it
  (`--waive A`). See [`depends`](commands.md#depends).

To release a wait that no longer applies, edit the issue so it no longer declares the PR, then
remove and re-apply the fix label: the hand-off is decided again, and the old wait is cleared.
An issue an open PR already closes (`Fixes #N` in its description) is not handed over at all.

## Issues the reviewer files

When a review finds something real that doesn't block (issue-tier, usually P2/P3), the reviewer
files it as an issue itself, before submitting its review, instead of listing it for you to copy
(#247). Each issue:

- is filed as the **reviewer seat's own account**, so the history shows a machine filed it;
- carries only a title, a body and labels from this loop's triage list (`triage.labels`); with no
  list configured, it has no labels;
- ends with a host-written line naming the PR it came from and its **lineage depth**: 1 for a
  finding on a person's PR, one more for a finding on a PR that fixed a filed issue. Past depth 1
  the issue says a person decides whether it goes back to the fixer.

At most three per review, and one per title per PR, so a retried or later review never files the
same finding twice. The review lists them under **Issues filed** with their numbers.

A filed issue is an ordinary issue. If its author (the reviewer account) is in `triage.authors`
and it already carries an allowed label, triage leaves it alone. It reaches the fixer only when a
maintainer applies the fix label, so you stay in the loop.

## Caps and costs

Triage uses `seats.triage` concurrency/budget overrides (default concurrency one), and
`--daily-turns` pacing. Over-cap work waits until local midnight without spending an
error retry. Runtime/model/provider failure may prevent a scheduled turn from starting.
See [configuration](configuration.md) for exact budget inheritance.

Issue fixes are serialized (concurrency one), use the loop-wide `turn_budget_s` (default
900 seconds), and are **always capped per day**: `triage.fix_daily_turns`, default 10
(`triage --fix-daily-turns N`). Every fix opens a new PR, so the per-PR verdict cap never limits
how many happen; this cap does. A successful proposal adds the costs of the
ordinary PR review loop. Removing/reapplying the fix label at the same base commit
cannot create a second independent turn. At a new base commit it can create fresh work,
but an existing `diaktoros/issue-N` branch still prevents publication. Repeated
labeling is not a safe recovery procedure after a possible write.

## Inspect results

Use the exact ledger path from your active Hermes home. These queries require `sqlite3`
and use read-only mode. Replace `<issue-number>` with a positive integer before running.

```bash
sqlite3 -readonly "<ledger-path>" "SELECT r.id,r.pr,r.state,r.error,t.state,t.error,t.comment_id FROM runs r LEFT JOIN triage_results t ON t.run_id=r.id WHERE r.seat='triage' AND r.pr=<issue-number> ORDER BY r.created DESC;"
sqlite3 -readonly "<ledger-path>" "SELECT r.id,r.pr,r.state,r.error,f.kind,f.state,f.branch,f.pr_number,f.comment_id,f.error FROM runs r LEFT JOIN issue_fixes f ON f.run_id=r.id WHERE r.seat='issue_fixer' AND r.pr=<issue-number> ORDER BY r.created DESC;"
```

`<ledger-path>` is `$HERMES_HOME/state/diaktoros-runs.sqlite`. The left join keeps runs
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

After the push, the host reads the new branch back and waits out GitHub's lag (re-reads
after 1, 2, 4, 8 and 8 seconds — about 23 seconds in all) before it calls the push unknown. When the push's or the
PR's outcome is still unknown, the run itself ends `uncertain` (`post-write push
quarantine: unknown` or `…: pr_create_unknown`), never `succeeded`: `run_supervisor
status` lists it, and after you inspect the branch and issue, `reconcile` releases it.

## Turn it off

```bash
hermes dk triage --loop "<loop-id>" --disable --admin-token "<hook-admin-login>"
hermes dk triage --loop "<loop-id>" --enable --fix-label ''
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
seat's eligible problem runs at the selected ledgered head/base. An issue fix cancelled
because issue fixes went off says which condition failed (triage off, no fix label,
unattended fixer pushes off), and `retry` refuses it with the same reason until it is
fixed. Superseded cancellations,
uncertain runs and reconciled runs cannot be retried. **Any** `triage_results` or
`issue_fixes` row prohibits re-arm, even if its state is `nothing`, `skipped` or `denied`;
other review/push/ruling write evidence also prohibits replay. Inspect the queries above
and actual GitHub state rather than treating a failed supervisor row as proof of no write.

An intentional new issue-fix attempt also requires remote `diaktoros/issue-N` to be
absent. Inspect the earlier run, branch, PR, comments and review request first. Only
when you have established a safe new attempt—not an ambiguous or already published
result—deliberately remove that inspected remote branch, then have an authorized
maintainer reapply the label for eligible fresh work (a new base for a distinct durable
turn), or use `retry` for an eligible pre-write run. No branch is automatically deleted
or overwritten. Deleting a branch cannot make a recorded/uncertain run retryable;
never delete receipts or safety history to bypass this boundary.

## A burst of issues over the gateway's rate limit

The Hermes webhook gateway answers HTTP 429 to the deliveries over its `rate_limit` per route
per minute (30 unless set), and GitHub does not redeliver. A burst of new issues (55 filed in a
minute) can therefore lose triage turns. Two things cover it:

- **Recovery sweep.** Each armed watchdog sweep lists the open issues and queues the triage of
  each one the gate would have queued: open, not a pull request, by a `triage.authors` login,
  with no label from `triage.labels` (or `triage.fix_label`) and **no triage row in the run
  ledger**. It skips an issue newer than 5 minutes (the delivery may still come) or older than 3
  days (a backlog from before the loop is not triaged in bulk), and queues at most 10 per sweep,
  oldest first. The listing is read newest first and is bounded to 5 pages of 100 (open pull
  requests share it); past that the sweep still queues from the newest ones it read and raises
  an alert that older issues in the window were not checked. `status` lists them as `late: issue #N triage queued late: the webhook delivery
  was missed`, until that issue's triage run has succeeded (the next sweep then drops the line;
  a failed, cancelled or still-running one stays listed). The sweep needs the same private runtime the gate does, and changes nothing for a
  loop without `triage`.
- **Doctor.** `gate:rate_limit` reads the gateway's `rate_limit` and warns below 60. Raise it in
  the gateway's `config.yaml` (`platforms.webhook.extra.rate_limit: 120`) and restart that
  gateway. It is a gateway-wide setting: a `rate_limit` key on a route entry is never read, so
  `setup` and `arm` cannot raise it for you.

A dropped review or fix wake (a burst of PR events) has no such backstop yet: only the doctor
warning covers it.

## Current limits

- `trace` replays `pull_request`, `pull_request_review` and `issues`; `explain` evaluates PRs,
  not issues, so use it on the PR after a successful issue fix.
- Observer events cover triage and issue fixes (`triaged`, `fixing`, `fixed`, and `failed`
  with the issue link). With no observer configured, or those events filtered out, failed
  isolated runs may still reach the watchdog operator outbox; absence of chat output proves
  nothing about issue success.
- Status lacks a dedicated triage-settings/pacing summary; use `triage --loop` and
  ledger queries. Generic run diagnostics are not a complete issue-results view.
- Triage reads one bounded issue without repository code or a duplicate-issue search feed.
- Fixes can only add/replace files through the broker and cannot overwrite the deterministic
  branch. No automated reconciliation of a partial push/open/request sequence exists.
- Adjudicator/triage gate failures are reported but are not automatically re-driven like
  reviewer/fixer gate failures. Use repository Recent Deliveries only after inspecting state.
