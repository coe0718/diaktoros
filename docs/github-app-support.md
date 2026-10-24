# GitHub App support (M0 feasibility spike)

What three GitHub App identities (reader, reviewer, fixer) can actually do on GitHub, observed on
2026-10-21 against a throwaway repository. This page is the evidence for issue #1164 and gates the
GitHub App authentication work tracked in #1163. Nothing here is a feature yet: loops still use
the distinct accounts and tokens described in [Accounts](accounts.md).

## How it was measured

- A throwaway public repository with `main` protected (one required approval, stale approvals
  dismissed) and a second branch protected with no required approvals.
- Three GitHub Apps registered by hand, each installed on that repository only, each with its
  webhook active. Their webhook URL pointed at an address that refuses deliveries: GitHub still
  records every delivery, and the harness reads them back through each App's
  `GET /app/hook/deliveries` API, so no receiver was needed.
- Permissions: reader — contents, pull requests, checks, commit statuses and issues, all read;
  reviewer — contents read, pull requests and issues read and write (and checks write, for 9);
  fixer — contents, pull requests and issues read and write. Both writers need issues: the
  reviewer files issues and labels for triage; the fixer comments on issues it fixes.
- [`tools/github_app_spike.py`](../tools/github_app_spike.py) repeats every step below on fresh
  branches and prints one JSON line per step. Each run agreed with the one before.

## Results

| # | Question | Observed | Consequence for App mode |
| --- | --- | --- | --- |
| 1 | Do the fixer App's PR and pushes send webhooks? | **Yes.** `pull_request` `opened` and `synchronize`, and `push`, with `sender` = `<fixer-slug>[bot]`, `type: Bot`. | The reviewer's trigger exists: a fixer push is a `synchronize`. |
| 2 | Can the reviewer App submit real reviews at an exact commit? | **Yes.** `REQUEST_CHANGES` with an inline comment and `APPROVE`, both at the requested `commit_id`, by `<reviewer-slug>[bot]`, `type: Bot`. The `pull_request_review` `submitted` payload carries `review.state`, `review.user` (login, `type: Bot`, numeric id), `review.commit_id` and `installation`. | Verdicts and the fixer's trigger work as today. |
| 3 | Can an App bot be requested as a reviewer? | **No, silently.** `POST …/requested_reviewers` with `<reviewer-slug>[bot]` or `<reviewer-slug>` answers **201**, but `requested_reviewers` stays empty and no `review_requested` event is sent. A human reviewer on the same PR is requested normally, and DELETE then POST fires `review_request_removed` and `review_requested`. | App mode must not use review requests at all: the 201 would read as success. The host delivers the fixer's request to the reviewer gate itself (#1203); `hermes dk review` asks by hand. |
| 4 | Does an App's review count for branch protection? | **No, either way.** After the App's `APPROVE` the PR stays `reviewDecision: REVIEW_REQUIRED`, `mergeStateStatus: BLOCKED`. Its `REQUEST_CHANGES` is not counted either. | GitHub will not block a merge on the reviewer's verdict, nor allow one on its approval. Only Diaktoros's own `ready` and `explain` say what may merge; the merge stays a person's. |
| 5 | Does each App's webhook see the other Apps' activity? | **Yes, all of it.** With all three webhooks active, each recorded the same deliveries, so every event arrives three times. | One designated App receives events; the others keep their webhooks off. |
| 6 | The reviewer App on a person's PR, and a push after its approval | The App's approval on a person's PR is accepted. After the person pushes, GitHub does **not** dismiss the App's approval, even with stale-approval dismissal on: it stays `APPROVED` at the old commit. | Fine for Diaktoros, which counts only a verdict at the exact head; never rely on GitHub dismissing it. |
| 7 | How are the fixer App's commits attributed? | Through the contents API: author `<fixer-slug>[bot]` `<<bot-id>+<fixer-slug>[bot]@users.noreply.github.com>`, committer GitHub, **verified** (GitHub signs them). Through `git push` over HTTPS (`x-access-token` user, the token via `GIT_ASKPASS`): the commit links to `<fixer-slug>[bot]` when that noreply address is the author, but is **unsigned**. | The fixer's git identity is the bot's noreply address. Signed commits would need the API path or a signing key. |
| 8 | What does an installation token look like? | `ghs_` prefix, **390 characters**, valid one hour, scoped to the repositories and permissions it was minted for. | Treat tokens as opaque strings; never assume a length. Mint on the host per write, not per turn. |

### The reviewer App's permissions are wider than its seat

With `Pull requests: write` and `Contents: read`, the reviewer App was refused a contents push
(403) and a merge (403), but it **could** edit the PR's title, add labels, and close and reopen
the PR (200). GitHub permissions cannot express "reviews only", so in App mode, as today, the
host broker decides which operations each seat may perform, and no App credential or token
enters a seat's sandbox.

## A merge gate that GitHub does enforce: a required check

App reviews don't count for branch protection (4). A **check run** posted by the reviewer App
does, when the branch requires it. Measured on a base branch that requires the check
`diaktoros/review` **pinned to the reviewer App's id** (and no approvals), with the reviewer App
given `Checks: write` (harness `--check-branch`):

| # | State of `diaktoros/review` at the PR head | Merge state |
| --- | --- | --- |
| 9a | none yet | `BLOCKED` |
| 9b | `in_progress` (pending) | `BLOCKED` |
| 9c | `failure` | `BLOCKED` |
| 9d | `failure`, plus a person's commit **status** of the same name set to `success` | `BLOCKED`: the pin to the App ignores it |
| 9e | the fixer App tries to create the check (no `Checks` permission) | refused, 403; still `BLOCKED` |
| 9f | `success` | `CLEAN` |
| 9g | a new push after `success` | `BLOCKED`: the check belongs to the old commit |
| 9h | the repository admin merges while the check is missing, with *include administrators* on | refused: "Required status check "diaktoros/review" is expected." |
| 9i | the same with *include administrators* off | merged: admins can bypass |

So a check that follows the loop's own merge gate (pending while a review or the shadow is out,
failure on blocking changes, a shadow hold or a REJECT ruling, success only when approved at this
exact head with nothing holding it) gives back a block the merge button honours, and a stricter
one than the review alone: it covers everything `ready` knows and resets on every new head. With
*include administrators* on, no one can click past it. It works in PAT mode as well (#753 already
proposes posting turn state as a commit status).

**As built (#1163 M2b):** the host posts this check with the reviewer App, on by default
(`merge_check: auto`). It is written after every verdict, shadow review and ruling, and on every
watchdog sweep, only when the head, state or summary changes; the summary lists what holds the
head. GitHub's own `blocked` is not counted (with the check required, GitHub reports `blocked`
*because* the check is not green). A required check that has not reported for 15 minutes after the
head was seen (or, for a head already open when the loop was armed, since arming) counts as a
failure, not a wait. Don't list `diaktoros/review` in `required_checks`: the loop never reads its
own check as CI, so `required_checks` refuses it. An escalated head with no adjudicator route is a
failure (a person decides). A PAT loop can opt in with `merge_check: on` and gets a commit
status, which branch protection cannot pin to one identity. To make it the block, run
`hermes dk protect --loop ID --admin-token LOGIN` (#1163 M2c): it adds a ruleset of the loop's
own that requires `diaktoros/review`, pinned to the reviewer App, with no bypass. `doctor`'s
`merge-check` line says whether the base requires the check and from which App, and flags a base
that still requires approvals (an App's never count, so each PR would need a person's), and one
that requires the check while the loop does not post it, or from an App that is not the reviewer (a PAT reviewer's commit status never satisfies a pinned check): every PR would stay blocked. A base it could only half read is never reported as verified.

## Recommendation: go, with a synchronize-based handoff

The core loop works with App identities: PRs, pushes, exact-commit reviews and their webhooks all
behave as Diaktoros needs. The two native features that do not work for Apps are not ones the
loop depends on for safety:

- **Reviewer requests (3):** never sent for an App reviewer; the host delivers the fixer's
  request to the reviewer gate itself (#1203), the watchdog's sweep backstops it, and
  `hermes dk review` asks by hand.
- **Branch protection (4):** GitHub neither counts nor enforces the App's reviews, but it does
  enforce a required check pinned to the reviewer App (9). App mode posts `diaktoros/review`
  from the loop's merge gate (#1163 M2b, the `merge_check` setting); `hermes dk protect` (or
  setup's step 5) requires it, pinned to the reviewer App, and `doctor` checks it (M2c).

Also carried into #1163: one designated App for events (5), numeric bot ids rather than logins
(2, 7), opaque tokens (8), and the broker as the only enforcement of what each seat may do.
