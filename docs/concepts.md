# How Diaktoros works

[Documentation map](README.md) · Next: [Accounts](accounts.md) and [Getting started](getting-started.md)

The plugin reviews eligible GitHub PRs with an isolated reviewer. If you opt into unattended pushes, an isolated fixer answers changes and requests another review. Python—not a model—decides eligibility, capacity, handoff, retry and escalation. Merging remains your decision.

**The default is review automation, not unattended fixing.** A changes-requested verdict is held for you while fixer pushes are off. The fixer-push policy is independent of hook arming.

## Glossary

### Roles and identities

| Term | Meaning | Consequence |
| --- | --- | --- |
| Loop | One repository's configuration and state, named by an id | Commands select it with `--loop ID`; default id is the repository name |
| Seat | An agent role with a bounded capability | A seat is not the reader or a notification destination |
| GitHub login | Account a seat's writes appear under | Distinct reviewer/fixer logins and mapped token files are required |
| Hermes profile | Host-side model/provider configuration and authentication | Reviewer/fixer profiles and actual homes must differ; a profile name does not create a GitHub account or a separate subscription |
| Agent display name | `seats.<seat>.agent`, a prompt/attribution label | It grants no permissions and is not the account identity |
| Reader | Explicit `read_token` login for trusted GitHub reads | Must be a third account/file, not inferred from the first mapping |
| Hook admin | Login whose mapped token manages repo hooks | Defaults to reader for hook-editing commands; not an additional mandatory seat |
| Allowlists | `fixers` and `reviewers` GitHub logins | Limit eligible authors/events and reviews that count; seat logins must belong to the corresponding list |
| Token file | One account's private PAT file; config stores its path | Contents remain on the trusted host, not in the agent's environment |

The **four-identity rule** requires reader, reviewer, fixer and optional adjudicator comment login to resolve to distinct GitHub accounts with distinct files. Without an adjudicator comment login, three suffice. Offline checks compare names/files; `selftest` and broker authorization resolve actual principals. Different filenames containing tokens for one account do not satisfy the rule.

Distinct models are a useful review strategy, but source validation requires distinct profiles/homes and GitHub identities, not distinct model ids. [Accounts](accounts.md) covers repository access and permissions.

| Role | Work | Broker capability / limit |
| --- | --- | --- |
| Reviewer | Inspect exact-head code/diff and run tests | One APPROVE or REQUEST_CHANGES review; plain COMMENT reviews are not permitted |
| Fixer | Answer a current changes-requested verdict | With push opt-in: one bounded push, then answers/review-request handoff; partial-view restrictions can limit it to answers only |
| Adjudicator (optional) | Rule after the changes-requested budget is exhausted | One ACCEPT / REJECT / RESPEC ruling; optional distinct account posts a comment; no merge, push or review |
| Triage (optional) | Label allowlisted issues from a configured label set | Bounded labels and optional comment; defaults to reviewer login, never reader |
| Issue fixer (optional) | Fix a maintainer-labelled issue | Uses fixer profile/login; bounded issue branch/PR handoff or explanatory issue comment |
| Observer (optional, **not a seat**) | Deliver notices of already-made transitions | Delivery-only route, no agent, no PR lock, no model turn |

A basic PR loop does not create the initial code or PR for you. You or your coding workflow opens it under a fixer-allowlisted login. Issue fixing is a separate opt-in workflow that can open a PR.

## Event path: hook → route → gate → worker

1. A **repository hook** on GitHub sends `pull_request` events to the reviewer route or `pull_request_review` events to the fixer route. Issue automation adds an issues hook. Newly created hooks are paused unless explicitly armed.
2. A **gateway route** binds the webhook URL/secret, serving profile and gate script. The public origin belongs to your gateway; its profile-qualified URL is part of seat identity.
3. A **gate** reads live GitHub facts, checks eligibility and either holds/declines the event or queues an isolated turn.
4. A host **worker** claims the queued turn, rechecks the live PR, prepares an exact-head snapshot and launches the seat inside bubblewrap.
5. The seat accesses inference and writes through bounded host capabilities, not direct credentials/network.

Gates answer `[SILENT]` so Hermes does **not** launch a normal gateway agent for the PR. An ignored-script webhook response is therefore not proof of failure or success. Inspect `status`, `explain` or `trace`. An adjudicator route is a configuration switch for isolated adjudication; the legacy adjudicator gateway gate stays silent rather than running a credential-owning agent.

## One PR, start to finish

| Step | Event / decision | Result |
| --- | --- | --- |
| Open, reopen or mark ready | Eligible non-draft, same-repository PR, allowlisted author, configured base; current head unreviewed and under cap | Reviewer turn may queue |
| Run review | Worker and broker recheck the exact live head | Reviewer posts one real review; stale/closed work is cancelled rather than silently retargeted |
| Approve | Current-head approval | No fixer turn; you decide whether to merge |
| Request changes below cap | Current latest allowlisted changes-requested verdict | Default: held for you. With push opt-in: fixer turn may queue |
| Fix and hand off | Authorized fixer push followed by answers and explicit review request | Request frees handoff and may queue review of the new head |
| Push without requesting | `synchronize` alone | No new review; explicitly request the configured reviewer |
| Reach cap | Configured number of counted changes-requested verdicts | No additional fix; breach marker and optional isolated adjudicator turn |
| Rule | Adjudicator route configured and current situation still eligible | Record/report ACCEPT, REJECT or RESPEC; optional comment; human decides |
| Close or merge | Closure event and cleanup | Cancel/reconcile work and clean owned local artifacts; no automatic merge |

Explicit `review_requested` events must name the configured reviewer seat and come from an allowlisted fixer/reviewer sender, or from a loop maintainer (`triage.maintainers`). A maintainer's request asks for a fresh review at the current head; unlike the fixer's own request, it is not a hand-back, so it never frees a fixer that still holds the PR (the review queues behind it). Anyone else's request is ignored. Without maintainers, toggling the PR to draft and back starts a review too (`ready_for_review`). Out-of-scope authors, fork PRs, draft PRs and wrong-base PRs do not qualify for the ordinary loop. Stacked PR/retarget handling has additional reconciliation rules; see [architecture](architecture.md), not assumptions based only on this simplified lifecycle.

### Verdict budget, not a timer

`cap` defaults to 3 and must be at least 2. The gate counts **CHANGES_REQUESTED** reviews by allowlisted reviewers from GitHub history. Approvals end the relevant situation; a comment neither counts nor wakes the fixer. With cap 3, the ordinary path permits three changes-requested verdicts and at most two intervening fixes. The third escalates instead of buying a third fix. A new commit does not erase the accumulated review budget.

Reviewer findings are graded P0–P3 and classified as blocking or issue-tier. Blocking findings request changes; an approval may list nonblocking issues to file. This does not automatically file those issues. An adjudicator ruling is not a GitHub approval or a merge instruction enforced by the plugin.

## The switches

| Switch | Purpose | When absent/off |
| --- | --- | --- |
| Loop config and routes | Bind repository, allowlists, profiles and gates | No valid installed event path |
| Private runtime file | Provide working Hermes/venv/Python/Rust paths | Eligible work is held with its reason |
| Active matching repo hooks | Deliver events and authorize armed queue draining | Paused loop does not drain or report ordinary stalls; unknown hook state is not treated as armed |
| Unattended fixer-push policy | Explicit per-loop host authorization to publish fixes | Changes-requested work is held for operator decision |
| Watchdog schedule | Poll for stalls, drain eligible work, retry safe failures, repair owned routes | No scheduled sweep; events/manual operations must drive eligible work |
| Adjudicator route | Enable isolated ruling at the cap | Marker and human decision only |
| Observer route | Send transition notices to a configured chat | No notification feed; not a blocker for turns |
| Triage / issue-fix settings | Allow issue labeling or maintainer-triggered fixing | No issue automation; issue fixing also needs unattended-push authorization |

Setup writes runtime paths and can install paused hooks, but does not enable adjudication, issue automation or fixer pushes. The [getting-started guide](getting-started.md) explains the checks before arming.

```bash
hermes dk fixer-push --loop ID --enable --acknowledge-pr-race
hermes dk arm --loop ID --pause
```

`--loop ID` selects one loop. `--enable` opts into unattended fixer publication; `--acknowledge-pr-race` explicitly acknowledges non-atomic GitHub metadata checks. The second command's `--pause` deactivates matching existing repo hooks; add the mapped `--admin-token "<admin-login>"` if reader lacks hook write. These switches do not undo a completed write. Read [security](security.md) before enabling publication.

## Capacity, time and recovery

- **One PR, one seat:** reviewer/fixer activity is serialized per PR even when other PRs run in parallel. The handoff—not a model's assertion—releases the next role.
- **Per-seat concurrency:** each seat has its own slots. Above one, a local clone is required; every turn still gets an independent exact-head export, not a shared working checkout.
- **Turn budget:** default 900 seconds, builds/tests included; valid range 60–14400. The agent is asked to wrap up at 80%; the sandbox is terminated shortly after the limit. Budget-exhausted work is not automatically retried unchanged.
- **Run ledger:** shared SQLite state deduplicates turns, tracks leases/capacity and records writes. Failed pre-write turns can retry after 2, 4 and 8 minutes (four total attempts). A lost worker or ambiguous write can become `uncertain`; it is not automatically replayed.
- **Pacing:** provider usage-window holds and optional daily turn caps limit starts without treating every quota wait as a spent retry. Profiles alone do not isolate provider quota.
- **Watchdog:** one shared cron job reads GitHub, reports stalls, drains eligible work, repairs owned routes and warns about reader-token expiry when GitHub supplies it. It uses no model; it is not purely read-only.

[Operations](operations.md) covers reconciliation, raising budgets, retry/drain and pacing. Do not treat a queued run, webhook 200 or agent summary as proof of a completed external write.

## Trust boundary and limits

The agent sees the staged checkout/diff, disposable environment and capability sockets, not PAT files, profile auth stores or a model key. Direct network access is unavailable. Trusted dependency prefetch can provide pinned Rust dependencies; this is not general Internet access for arbitrary package installation. Sandbox visibility is bounded, not simply a promise that the agent will behave.

The host broker constrains operations, principals, paths and live-head authorization. Sensitive changes such as `.github/`, `.gitmodules`, `.gitattributes` and `CODEOWNERS` are blocked by push policy. A ref lease rejects concurrent branch movement, **but cannot atomically enforce PR state**. A PR can close, become draft or retarget after the last API read and before Git updates the ref. Readback may detect a problem without undoing the published commit.

A reviewer can still post a wrong verdict, and an authorized fixer can still publish a bad bounded change. Kernel/bubblewrap escapes and trusted-host plugin compromise are outside the credentialless-turn guarantee. The plugin never supplies a merge capability. [Security](security.md) is the detailed authority for the boundary.

## Where things live

Paths below assume `HERMES_HOME=~/.hermes`; configuration describes supported overrides. Angle brackets in paths are placeholders, not literal names.

| Data | Location / ownership |
| --- | --- |
| Loop config | `~/.hermes/review-loops.d/<id>.json`; one repository's explicit settings |
| Gateway registry | `~/.hermes/webhook_subscriptions.json`; shared with other plugins |
| Runtime | `~/.hermes/review-loop-runtime.json`; private host directories, not the basic model selection |
| Per-loop state | `~/.hermes/state/review-loops/<id>/`; queues, locks, markers, audits, route intent, artifacts |
| Run ledger | `~/.hermes/state/review-loop-runs.sqlite`; shared isolated-run state |
| Credentials | Private absolute PAT files, conventionally `~/.hermes/keys/<login>-pat`; host only |
| Serving-profile scripts | Gate shims in the corresponding profile's `scripts/` directory |
| Watchdog shim | `~/.hermes/scripts/review-loop-watchdog.py`; shared cron entry calls it |

Use `status` and `explain` instead of editing state/ledger files. `doctor` checks installation; `selftest --no-model` verifies runtime/sandbox and actual GitHub identities without inference. Neither an offline suite nor a no-model selftest proves a real provider turn succeeded.

## Continue

[Accounts](accounts.md) → [Getting started](getting-started.md) → [Operations](operations.md). For exact flags use [commands](commands.md); for a symptom use [troubleshooting](troubleshooting.md); for every page use the [documentation map](README.md).
