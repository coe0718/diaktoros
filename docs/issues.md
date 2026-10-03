# Issue triage and issue fixes, step by step

A review loop starts at a pull request. Two optional features let it start one step earlier, at an
issue:

- **Issue triage.** When someone you trust opens an issue, a sandboxed triage turn reads it and
  adds labels from a list you choose (for example `bug`, `docs`, `P2`). It can also leave one
  short comment, if you allow that.
- **Issue fixes.** When a maintainer adds a special label (for example `agent-fix`) to an issue,
  the fixer seat gets one turn to fix it. If it can, it opens a PR, and the normal review loop
  takes over from there. If it can't, it explains why in one comment on the issue.

Both are off until you turn them on, per loop, with one command: `hermes review-loop triage`.
Issue fixes are part of triage: they need triage turned on first.

This page walks through both. The examples use the repo `owner/name`, the loop id `name`, the fixer
account `dev-account`, the reviewer account `rev-bot`, the reader account `reader-bot`, and the
Hermes profiles `coder` (fixer), `critic` (reviewer) and `arbiter` (triage). Replace `you` with your own
GitHub login. Every command and flag is also in the [command reference](commands.md#triage).

## What each one does, and who can start it

| | Issue triage | Issue fixes |
| --- | --- | --- |
| Starts when | an issue is **opened** | a label is **added** to an open issue |
| Who can start it | the issue's author must be in `triage.authors` (`--author`) | the person who adds the label must be in `triage.maintainers` (`--maintainer`), the label must be `triage.fix_label` (`--fix-label`), and the issue's author must also be in `triage.authors` |
| Runs as | the triage profile (`--profile`) | the fixer seat: its profile, model and GitHub account |
| What it can write | labels from `triage.labels` (at most `max_labels`, default 3), plus one comment of at most 1000 characters only with `--comment on` | either one PR from a new branch `review-loop/issue-N`, or one comment on the issue |
| Writes as | `triage.login` (default: the reviewer seat's account, `rev-bot`) | the fixer seat's account (`dev-account`) |
| Extra opt-in | none | unattended fixer pushes must be on for the repository |

A few rules hold for both:

- **The issue text is untrusted.** Anyone can type anything into an issue, including instructions
  aimed at a model. So the gate drops an issue from anyone outside `triage.authors` before any model
  sees it. The issue's title and body reach the model as data (the body is clipped at 8000
  characters), and the model's single write goes through the host broker, which enforces the rules
  above whatever the model asks for. The model never holds a GitHub token.
- **The broker enforces the label list.** A label not in `triage.labels`, more than `max_labels`
  of them, or a comment while comments are off is refused before anything is written.
- **People win.** Triage only ever *adds* labels; it never removes one. If the issue already
  carries any label from `triage.labels` (a person got there first), triage writes nothing. That is
  checked by the gate, again just before the turn starts, and again by the broker right before it
  writes.
- **The fix label can't be a triage label.** `normalize` refuses a `fix_label` that is also in
  `labels`. So the triage turn can never hand an issue to the fixer: only a person can.
- **One write per turn, recorded first.** Each triage and each issue fix is written to the host run
  ledger before the GitHub write. A write whose outcome is unknown is marked `uncertain` and is
  never replayed.

## Before you start

You need:

1. **A working review loop for the repository.** `doctor` should be clean and the hooks armed.
   If you don't have one yet, start with [`setup`](commands.md#setup).
2. **The private runtime file**, `~/.hermes/review-loop-runtime.json`. Triage and issue-fix turns
   are isolated turns like reviews, so without this file nothing can start. `setup` writes it;
   `hermes review-loop selftest --loop name --no-model` checks it.
3. **An account to label as.** By default triage labels as the reviewer seat's account (`rev-bot`),
   which already has a token file mapped. To use another account, pass `--login LOGIN` and map its
   token with `--token LOGIN=/path/to/pat`. Either way:
   - the token needs `issues: write` on the repository (a classic `repo` token has it);
   - it can never be the reader (`reader-bot`): `triage --enable` refuses that;
   - the broker checks that the token really belongs to that login before every write.
4. **A Hermes profile for the triage model.** Any existing profile works. Triage has no
   distinctness rule, so it can be the reviewer's profile (`critic`) or another one (`arbiter`). The
   profile must exist; `triage --enable` refuses a profile name it can't find. A cheap, fast model
   is usually enough: triage reads one issue and picks labels.
5. **The labels.** Decide the list triage may choose from. If a label doesn't exist in the
   repository yet, GitHub normally creates it (grey, with no description) the first time it is
   added. If you want colors and descriptions, create the labels on the repository's **Labels**
   page first (`https://github.com/owner/name/labels`). Label names can be 1 to 50 characters
   with no commas, braces, backticks or control characters, and you can list up to 100.
6. **A login that can manage repo hooks** (`--admin-token`). Triage gets its own repo hook, on the
   `issues` event. Creating it needs hook write access. The login must already have a token file
   mapped on the loop (or pass `--token LOGIN=/path/to/pat` in the same command). The examples use
   `reader-bot`, the repo owner's account in the README setup.

## Turn on issue triage

1. **Preview the change.** `--dry-run` validates everything and prints the settings it would
   write, and writes nothing:

   ```bash
   hermes review-loop triage --loop name --enable --profile arbiter --author you --labels bug,feature,docs,question,P0,P1,P2,P3 --admin-token reader-bot --dry-run
   ```

   `--author` is repeatable: pass it once for each login whose issues should be triaged. Add
   `--comment on` to allow one short comment with the labels, `--max-labels N` (1 to 10) to change
   the limit of 3, and `--login LOGIN` to label as an account other than the reviewer seat.

2. **Turn it on.** The same command without `--dry-run`:

   ```bash
   hermes review-loop triage --loop name --enable --profile arbiter --author you --labels bug,feature,docs,question,P0,P1,P2,P3 --admin-token reader-bot
   ```

   It writes the `triage` block into the loop config, a new webhook route `name-triage` (with its
   own secret) and its gate shim, and, because you passed `--admin-token`, a repo hook on the
   `issues` event. **The hook is created paused.** Without `--admin-token`, create the hook later
   with `hermes review-loop apply --loop name --hooks --admin-token reader-bot`.

3. **Arm it.** `arm` turns on every hook the loop has, the triage hook included, then pings each
   one and reports whether the gateway accepted its signature:

   ```bash
   hermes review-loop arm --loop name --admin-token reader-bot
   ```

4. **Check the install.**

   ```bash
   hermes review-loop doctor --loop name
   ```

   With triage on, `doctor` adds these lines. Each should be ✅:

   - `profile:triage`: the triage profile exists;
   - `credential:triage`: the triage login has a non-empty, private token file (it checks the file,
     not the token's permissions);
   - `model:triage`: the model the triage turn will use, from the triage profile;
   - `route:name-triage`: the route is in the gateway's registry and wakes the right profile;
   - `hook:name-triage`: the repo hook posts to that route on `issues`, and is active.

5. **Look at the settings any time:**

   ```bash
   hermes review-loop triage --loop name
   ```

   It prints the route, profile and login, the authors, the labels, whether comments are allowed,
   the daily cap, and whether issue fixes are on.

6. **Test it.** From an account in `--author`, open a new issue on `owner/name`. Within a few
   minutes (one isolated turn) you should see:

   - labels from your list added to the issue, by the triage login (`rev-bot`);
   - with `--comment on`, possibly one short comment from the same account, ending in the loop's
     footer `🤖 Automated by hermes-review-loop · issue triage`. The model may also apply no label
     at all, if none fits.

   Triage sends **no observer notice** (#231), so nothing arrives in your chat. To see what
   happened, read the run ledger, as shown in
   [troubleshooting](troubleshooting.md#an-issue-opened-and-nothing-was-labelled). Each triage is
   one row in `triage_results`, with the state `posted`, `skipped` (a person labelled it first),
   `nothing` (no label fitted), `denied` (the broker refused it), or `uncertain` (the write's
   outcome is unknown).

An issue opened by anyone not in `--author` is dropped at the gate, and costs nothing. Editing,
reopening or relabelling an issue does not start triage: only `opened` does. Each issue is
triaged at most once.

## Turn on issue fixes

An issue fix pushes a commit and opens a PR without you, so it needs the same opt-in as the fixer
answering review verdicts on its own.

1. **Turn on unattended fixer pushes**, if they are not on already. Read the
   [push policy](security.md#unattended-fixer-push-policy-host-operator-not-github-owner-consent)
   first: it is a host-operator decision, and it accepts a small race between the last check and
   the push.

   ```bash
   hermes review-loop fixer-push --loop name --enable --acknowledge-pr-race
   ```

   This also lets the fixer answer changes-requested reviews on its own, if it doesn't already.

2. **Create the fix label** on the repository's **Labels** page (for example `agent-fix`), so a
   maintainer can pick it from the issue's sidebar. It must not be one of your triage labels.

3. **Name the label and the maintainers.** Triage must already be on (the step above). Settings
   you don't pass are kept:

   ```bash
   hermes review-loop triage --loop name --enable --fix-label agent-fix --maintainer you --dry-run
   hermes review-loop triage --loop name --enable --fix-label agent-fix --maintainer you
   ```

   `--maintainer` is repeatable. `hermes review-loop triage --loop name` then shows
   `issue fixes: label 'agent-fix' by you hands an issue to the fixer`. If pushes are off, the same
   line ends `OFF until unattended fixer pushes are on` and names the command.

4. **Test it.** Open an issue from an account in `--author` (or use one that is already open), then
   add the `agent-fix` label from an account in `--maintainer`. What happens:

   1. The gate re-reads the issue: it must still be open, still by an allowlisted author, and still
      carry the fix label. It reads the head commit of the loop's base branch (`main`) and queues
      one issue-fix turn from that commit.
   2. The turn runs in the same sandbox as every other seat, as the **fixer seat**: the fixer's
      profile (`coder`) and model. `/work` holds the base branch at that commit, and the issue's
      title and body are passed as data. It may build and run tests, like a normal fixer turn.
   3. If it fixed the issue, the host pushes one commit to a **new** branch `review-loop/issue-N`.
      The push requires the branch not to exist, so it never overwrites anything. The commit
      carries the `Automated-By: hermes-review-loop (…)` trailer. Like every fixer push, it can add
      or replace whole files only (no deletes or renames) and nothing under `.github/`.
   4. The host opens a PR from that branch against the base, as the fixer account (`dev-account`),
      with the fixer's description followed by `Fixes #N`, and requests a review from the reviewer
      seat (`rev-bot`).
   5. From here it is an ordinary loop PR: the reviewer reviews it, the fixer answers, and so on.
      When it is merged into the default branch, GitHub closes the issue (the `Fixes #N` line).

   If the fixer can't fix the issue (it is unclear, too large, or needs a decision), it opens no
   PR. It posts **one comment** on the issue instead, as the fixer account, saying why.

How issue-fix turns are run:

- they use the fixer's profile and GitHub account (`dev-account`); there is no separate seat to
  configure;
- **one at a time** (concurrency 1);
- each turn gets the loop's `turn_budget_s` (900 seconds unless you changed it), not the fixer
  seat's own budget;
- **no daily cap** applies to them;
- each label application at a new base commit queues a new turn, but the second push is refused
  while `review-loop/issue-N` still exists. Delete the old branch first if you want a new attempt.

## Caps and costs

- **Triage** costs one turn for each new issue from an allowlisted author, and nothing for anyone
  else's. Cap it per day:

  ```bash
  hermes review-loop triage --loop name --enable --daily-turns 20
  ```

  Past the cap, triage turns wait until local midnight without failing. `--daily-turns 0` removes
  the cap. The cap counts per loop and resets at midnight. The triage seat's turn budget and
  concurrency (default 1) are set in the loop file under `seats.triage` (`turn_budget_s`,
  `concurrency`); see [configuration](configuration.md#issue-triage-triage).
- **Issue fixes** cost one full fixer turn (often with a build and tests) per label application,
  plus a normal review loop for the PR it opens. Only your maintainers can start one.

## Turn it off

```bash
hermes review-loop triage --loop name --disable --admin-token reader-bot
```

This removes the `triage` block from the loop config (so issue fixes stop too), the `name-triage`
route, its gate shim, and, with `--admin-token`, the repo hook on `issues`. Without
`--admin-token`, the hook stays on GitHub and gets 404s from now on: delete it under
**Settings → Webhooks**, or run the command again with `--admin-token`.

What stays: labels and comments already written, PRs already opened, the run ledger's records,
the token mapping, any `seats.triage` settings in the loop file, and the unattended fixer push
setting (turn that off separately with `hermes review-loop fixer-push --loop name --disable`).

To turn off only issue fixes and keep triage, clear the fix label:

```bash
hermes review-loop triage --loop name --enable --fix-label ''
```

## When it doesn't work

- [An issue opened and nothing was labelled](troubleshooting.md#an-issue-opened-and-nothing-was-labelled)
- [A fix label was applied and no PR came](troubleshooting.md#a-fix-label-was-applied-and-no-pr-came)

## Current limits

- **`trace` can't replay `issues` deliveries yet** (#230). It handles `pull_request` and
  `pull_request_review` deliveries only. Use the run ledger and GitHub's *Recent Deliveries* page
  instead (see the troubleshooting sections above).
- **No observer notices** for triage or issue fixes (#231). The PR an issue fix opens is an
  ordinary loop PR, so it gets the usual notices from then on.
- **`explain` is for PRs only.** It can't explain an issue. Once an issue fix has opened its PR,
  `explain --pr N` works on that PR.
- **`status` doesn't show triage.** Use `hermes review-loop triage --loop name` for the settings.
  `status`'s pacing line counts the reviewer and fixer seats only.
