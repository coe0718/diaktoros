# Observer feed

The observer delivers short transition notices to an operator's configured chat. It is
**not a review seat**: the delivery-only route starts no model conversation, holds no
seat lock, and does not authorize a GitHub write. The review loop continues if the feed
is absent, muted or broken.

## Contents

- [Enable and tune](#enable-and-tune-the-feed)
- [Events and meaning](#events-and-meaning)
- [Privacy and destination binding](#privacy-and-destination-binding)
- [Receipts and retries](#receipts-and-retries)
- [Digests](#digests)
- [Mute, disable and restore](#mute-disable-and-restore)
- [Troubleshoot](#troubleshoot-the-feed)
- [Source evidence](#source-evidence)

Replace quoted angle-bracket placeholders, including brackets, with your own values.
`--loop` selects a saved loop ID. The settings live in its `observer` block; see
[configuration](configuration.md) and [commands](commands.md) for the reference.

## Enable and tune the feed

Configure the desired platform/chat on the destination Hermes profile first, then:

```bash
hermes dk set --loop "<loop-id>" --observer-profile "<observer-profile>" --observer-deliver telegram
hermes dk set --loop "<loop-id>" --observer-events verdict,escalation,ruling,closed
hermes dk set --loop "<loop-id>" --observer-digest-min 30
hermes dk status --loop "<loop-id>"
hermes dk doctor --loop "<loop-id>"
```

| Option | Effect |
| --- | --- |
| `--observer-profile` | Profile that owns the authorized destination; enables the feed |
| `--observer-deliver telegram` | Gateway delivery platform; default Telegram, another configured adapter such as Discord can be selected |
| `--observer-events` | Comma-separated event filter; blank means all events |
| `--observer-digest-min 30` | Batch window in minutes for **routine** events (see [tiers](#urgent-and-routine-tiers)); 0 means per-transition notices |
| `--observer-urgent-route` | A second delivery-only route that receives urgent notices only; blank returns to one feed |
| `--observer-urgent-profile` | Profile that owns the urgent destination (default: the feed's profile) |
| `--observer-urgent-deliver` | Gateway platform for urgent notices (default: the feed's `--observer-deliver`) |

The default route is `<loop-id>-observe`. Use `--observer-route "<observer-route>"` if
you need a distinct route name. Installing this route uses the gateway's signed POST
mechanism but sets `deliver_only: true`; `observe.py` returns an already composed notice
rather than waking a model. It is not an extra repository webhook and does not need
GitHub `issues` events. Profile, platform, prompt/script, host and destination overrides
must match the authorized delivery-only contract.

Enabling does not promise backfill of events that happened while the feed was absent,
muted or filtered. Verify one eligible **new** transition in your intended chat.

## Events and meaning

A notice contains loop/PR/head identity, event, bounded outcome, optional next-turn hint,
and a PR link. It does not contain the diff, review body, GitHub credentials or HMAC secret.

| Event | Meaning | Not a promise that… |
| --- | --- | --- |
| `opened` | Eligible PR needs first review | A reviewer has started |
| `handoff` | Fix published and review requested | The new review has completed |
| `verdict` | Changes requested | The fixer will run when pushes are off |
| `approved` | Approval transition at a recorded head | Approval is still live or the PR can safely be merged now |
| `escalation` | Durable cap marker | Adjudicator received/finished its turn |
| `ruling` | Host recorded ACCEPT/REJECT/RESPEC | Code merged or the cap was reset |
| `stall` | Watchdog decided a stall warrants reporting | The watchdog repaired the underlying defect |
| `closed` | PR merged/closed and cleanup attempted | Cleanup reclaimed disk (cleanup runs for every closed PR, but the notice is sent only for PRs this loop worked on) |
| `triaged` | Issue triage ended: labels applied, none fit, skipped, denied or uncertain (links the issue) | A person agrees with the labels |
| `fixing` | A maintainer's fix label handed the issue to the fixer, or the handoff was held and why | The fix turn has started |
| `fixed` | The issue-fix write ended: PR opened (and review requested), a could-not-fix comment, or an uncertain write | The PR passes review |
| `failed` | An isolated run's first failed attempt (with the retry time) and its terminal `failed`/`uncertain` state | The cause is transient, or a retry will work |
| `held` | A run is waiting without spending a retry: its seat's daily cap is reached, or its provider's usage window is closed. Says when it resumes and, for a cap, the flag that raises it and the `retry` that runs it sooner | It will run the moment the hold lifts (capacity and pacing still apply) |
| `ci_failed` | A required check is red at a loop PR's current head, once per head, naming the failed check(s) and linking the run (#306). Found by the watchdog sweep, with no model | With `fix_ci` and unattended fixer pushes on, on a fixer's PR: one fixer turn for that head, which gets the failing jobs' log tails as data. The notice says which CI fix it is ("CI fix 2 of 3"), or that the CI-fix budget is spent and the reviewer reviews the red head (#539). Otherwise you fix it |
| `main_red` | A required check is red at the head of the loop's base branch, once per main head, naming the failed check(s) and the PRs merged since main's last green head. Found by the watchdog sweep, with no model and no GitHub write. GitHub's "Require branches to be up to date before merging" protection prevents this class of breakage | You fix or revert main |
| `conflict` | A loop PR no longer merges into its base (GitHub reports a merge conflict), once per head (#303) | With unattended fixer pushes on, a resolving fixer turn: the host merges the base into the head and the fixer resolves the conflicted files (a whole-file conflict, or a base that changed workflow files, ends that run as "needs a person"). With them off, you merge the base by hand. A new head that still conflicts is reported again |
| `updated` | With `review_only_update` on, the host merged the base into a review-only PR's branch and pushed it (clean merge only) | Nothing: review resumes at the new head through the normal gate |

The later events (the last rows of the table) were added after the first eight. A feed with an explicit `events` list
does not get them until they are added to it (`set --observer-events …`); a feed with no list
gets every event.

Opened/handoff/verdict next hints describe queue admission or holds, not child start.
Missing runtime and disabled fixer pushes are common holds; concurrency, pacing and
live eligibility can still delay admitted turns. Escalation with an adjudicator route
says delivery is pending until enqueue; without one, the operator owns the next step.
Ruling notices omit the adjudicator's reason text; the operator outbox and optional
PR ruling comment carry it.

Approval's `next: you merge` hint is revalidated immediately before sending against
current review identity, head/base and post-write holds. If verification fails, the
hint is omitted. A later state change remains possible: inspect GitHub before merging.
Retries and digests omit next-turn hints because the recorded transition may be stale.
Closed notices fire only for PRs this loop worked on (the author is a reviewed author, or the loop holds breach, transition, queue or observer-ledger state for the PR). Other repository PR closures are cleaned up silently.

Issue work does produce observer transitions when the host records an outcome: `triaged`
after the triage result is recorded (including a no-label/no-comment result), `fixing` when a
maintainer handoff is queued or held, and `fixed` when the issue-fixer write ends. These are
outcome notices, not a transcript of the model turn; see the event table above. A PR opened
by an issue fix then becomes a normal loop PR and can produce later PR notices. The watchdog's
failed-run operator outbox is a separate delivery path.

## Privacy and destination binding

A private PR link and repository identifiers still disclose metadata to chat recipients.
Choose a destination whose membership/access policy matches the repository. The feed
is not a confidentiality guarantee merely because it omits diffs.

The route registry is mutable transport state, not authority to redirect private links.
The host checks the configured gateway host, profile/platform, delivery-only contract
and `deliver_extra` against loop-authorized values. Changes to a profile/chat/platform
cannot silently deliver old owed notices to a new destination: unsettled receipts retain
their destination binding and configuration changes can be refused while notices are owed.
Inspect existing debt before moving a feed. Use the specific `doctor` remedy; do not
hand-edit the registry or receipts to bypass a destination mismatch.

## Receipts and retries

Receipts live in `<state_dir>/observations.json`. Logical keys include loop, PR, head,
event and event identity (review/round where applicable); repeated gates/deliveries do
not create another notice for the same transition. Delivery IDs are retained for safe
retry so the gateway can also deduplicate a logical send.

| Receipt | Meaning | Action |
| --- | --- | --- |
| `queued` | Waiting for a digest window/sweep | Check watchdog and batching |
| `digesting` | Member attached to an in-progress batch | Inspect batch receipt, do not send member again |
| `pending` | Sender claimed delivery before POST | Wait; stale claims become uncertain |
| `delivered` | Delivery success recorded | Check intended destination if not visible |
| `failed` with proven pre-POST failure | Route/secret/destination unavailable before send | Fix route; bounded watchdog retry |
| `uncertain` | POST may have landed, or sender died before receipt | Inspect chat/gateway logs manually; never blind replay |

Only definite pre-POST failures retry automatically, up to three attempts. A timeout,
5xx or other unconfirmed POST may have sent the message and is quarantined rather than
replayed. Legacy failure receipts without proof of pre-POST failure become uncertain.
These failures never consume a review seat or block PR admission, but may prevent an
unsafe observer destination rebind.

`status` aggregates delivered and owed records rather than proving end-user visibility.
Inspect receipts for the exact error and batch membership when diagnosing a notice.
There is no general observer replay/reconcile CLI: reconcile chat history and gateway
records manually, preserve evidence, and do not delete receipt files to manufacture a
new notice. Supervisor reconciliation does not reconcile observer receipts.

## Urgent and routine tiers

The tier is fixed per event in `diaktoros/observer.py` (`URGENT_EVENTS`):

| Tier | Events | With a digest set |
| --- | --- | --- |
| Urgent | `failed` (first attempt and final), `held`, `escalation`, `ruling`, `stall`, `conflict`, and any notice whose outcome is `uncertain` | Sent immediately, never batched |
| Routine | `opened`, `handoff`, `verdict`, `approved`, `triaged`, `fixing`, `fixed`, `closed` | Queued for the digest |

With no `digest_min`, nothing changes: every event is sent at once. With
`observer.urgent_route` set, urgent notices go to that route (own profile and platform via
`urgent_profile`/`urgent_deliver`) and everything else, including digests, stays on the main
route. The urgent route is a second delivery-only route held to the same contract as the main
one. Unset means one feed gets everything.

## Digests

A positive `digest_min` queues routine transitions until the oldest entry has aged past the
window. The watchdog flushes them on a sweep; it is **not an independent timer** and
30 minutes does not guarantee delivery at minute 30. No scheduled/working watchdog,
no regular digest flush. Known-paused loop hooks skip the loop sweep, so queued batches
can remain owed until normal sweeps resume. That pause also skips route/shim self-heal,
observer retries and pre-write worker retries, not just digest flush/PR scanning.
The digest groups by PR or issue, in event order, one line each, keeping outcomes only where
they matter (changes vs approve), at most 25 lines and then "…and N more":

```
🗂 [<loop-id>] last 30m — 2 PRs, 1 issue
#312 opened → reviewed (changes) → fixed → approved · https://github.com/…/pull/312
#316 opened → approved · …/pull/316
#318 (issue) handed to fixer · …/issues/318
```

Batch claims precede POST; uncertain batches
keep members attached rather than emitting duplicates separately.

## Mute, disable and restore

```bash
hermes dk set --loop "<loop-id>" --observer-mute
hermes dk set --loop "<loop-id>" --observer-unmute
hermes dk set --loop "<loop-id>" --observer-disable
```

These are separate choices: `--observer-mute` keeps configuration but stops new sends
and retries/flushes; `--observer-unmute` resumes that same feed. `--observer-disable`
removes the live observer configuration/route, retaining owed history and the old
destination binding. It does not declare debt delivered or erase uncertain sends.
Restoring the original destination can resume safe owed delivery; moving to another
chat is not permission to reroute old debt. Events suppressed while muted/disabled
are not a guaranteed replay backlog.

## Troubleshoot the feed

| Symptom | Likely cause | Safe action |
| --- | --- | --- |
| No notices at all | Muted/absent/filter excludes event | Inspect status and filter; test a new included transition |
| Route warning | Missing route/secret, unauthorized destination or contract mismatch | Run doctor and follow its route-specific remedy |
| Digest late | No watchdog sweep, hooks paused, oldest entry not old enough | Inspect watchdog scheduling and receipts; don't resend members |
| Notice owed after timeout | Outcome uncertain | Search intended chat and gateway logs; preserve receipt |
| Destination change refused | Old owed/uncertain notices bound to prior chat | Inspect old debt; don't bypass binding by deleting state |
| No notice for issue labels or proposal | Feature unsupported | Use issue/result ledger and GitHub, not observer expectation |
| Closed notice but disk still used | Cleanup attempted, not confirmed | Use [disk troubleshooting](troubleshooting.md#disk-usage-keeps-growing) |

The feed is emitted from host transitions, not agent prose. Do not treat an agent summary
or an observer message as evidence of a completed write, a successful build, or merge safety.

## Source evidence

Implementation boundaries: `diaktoros/observer.py` (`route_contract`, `_target`,
`notify`, `_deliver`, `retry`, `flush`), `scripts/observe.py`, CLI observer setters,
`diaktoros/state.py` receipt paths, and `scripts/watchdog.py` sweep callers.
For run recovery rather than feed recovery, see [operations](operations.md#when-an-isolated-run-fails).

### `stale_approval` (#473)

| `stale_approval` | A loop PR is approved at head H, and a required check at H is then red, cancelled, or never reported within 30 minutes of the approval. One notice per PR and head ("approval at H is stale: <check> is red"); `explain` shows it as a blocker. Found by the watchdog sweep; writes nothing to GitHub | You: re-run the check or push a fix. A new head clears it. A PR that is not approved at its head is never flagged. "Never reported" can only fire when `required_checks` names checks (with none, every reported check gates and none can be missing). The ready-to-merge queue (#479) is not in this repository yet; it should skip a PR for which `stale_approval.is_stale(watch, pr, head)` is true |
