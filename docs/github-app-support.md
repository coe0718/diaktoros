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
  reviewer — contents read, pull requests read and write; fixer — contents and pull requests
  read and write. Minted tokens for the reviewer and fixer also carried `issues: write`.
- [`tools/github_app_spike.py`](../tools/github_app_spike.py) repeats every step below on fresh
  branches and prints one JSON line per step. It was run twice; both runs agreed.

## Results

| # | Question | Observed | Consequence for App mode |
| --- | --- | --- | --- |
| 1 | Do the fixer App's PR and pushes send webhooks? | **Yes.** `pull_request` `opened` and `synchronize`, and `push`, with `sender` = `<fixer-slug>[bot]`, `type: Bot`. | The reviewer's trigger exists: a fixer push is a `synchronize`. |
| 2 | Can the reviewer App submit real reviews at an exact commit? | **Yes.** `REQUEST_CHANGES` with an inline comment and `APPROVE`, both at the requested `commit_id`, by `<reviewer-slug>[bot]`, `type: Bot`. The `pull_request_review` `submitted` payload carries `review.state`, `review.user` (login, `type: Bot`, numeric id), `review.commit_id` and `installation`. | Verdicts and the fixer's trigger work as today. |
| 3 | Can an App bot be requested as a reviewer? | **No, silently.** `POST …/requested_reviewers` with `<reviewer-slug>[bot]` or `<reviewer-slug>` answers **201**, but `requested_reviewers` stays empty and no `review_requested` event is sent. A human reviewer on the same PR is requested normally, and DELETE then POST fires `review_request_removed` and `review_requested`. | App mode must not use review requests at all: the 201 would read as success. The handoff is `synchronize`, plus `hermes dk review` by hand. |
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

## Recommendation: go, with a synchronize-based handoff

The core loop works with App identities: PRs, pushes, exact-commit reviews and their webhooks all
behave as Diaktoros needs. The two native features that do not work for Apps are not ones the
loop depends on for safety:

- **Reviewer requests (3):** replace with the `synchronize` event and the existing
  `hermes dk review`; the review-request paths stay PAT-only.
- **Branch protection (4):** document that GitHub neither counts nor enforces the App's verdicts;
  merging stays a person's decision, guided by `ready`.

Also carried into #1163: one designated App for events (5), numeric bot ids rather than logins
(2, 7), opaque tokens (8), and the broker as the only enforcement of what each seat may do.
