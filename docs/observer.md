# Watching from your phone (the observer feed)

The operator-facing guide to the observer feed. Every `observer` key is in the
[configuration reference](configuration.md#the-observer-feed); how the feed stays out of the loop is
in [architecture](architecture.md#the-observer-feed-read-only-never-a-seat).

The two seats drive each other through GitHub; the observer lets you watch without taking part.
Give a loop an **observer** and it sends one short notice per transition to a chat you choose —
Telegram, Discord, wherever that Hermes profile already talks. An `opened`, `handoff` or `verdict`
notice says the next turn is **queued** (it runs as an isolated turn when its seat is free) or held
for you, not that a reviewer or fixer has already started. A turn is held instead of run only while
the private runtime file is missing, or, for the fixer, while unattended fixer pushes are off.
A notice looks like this:

```
🔧 [widgets] #7 `aaaaaaa` fix pushed · review requested (dev-fixer) · round 2/3 · next: reviewer queued
https://github.com/acme/widgets/pull/7
```

That is the whole payload: the loop, the PR, the head at the recorded transition, the seat and
event, the outcome, an optional next-turn hint, and a direct link to the PR. Delayed retries and
digests omit next-turn hints because the PR or review may have changed since the transition.
Never a token, an HMAC secret, a private diff, or a review body. Escalation reaches the observer
after the cap marker is durable. On a loop with an adjudicator route it is sent before the
adjudicator turn is enqueued and says `next: adjudicator delivery pending`, not that the
adjudicator received it; without a route the next turn is `you`. A `ruling` notice carries the verdict
and counts, never the adjudicator's reason text (that goes to the watchdog outbox and, when an
adjudicator identity is configured, the PR).

Turn it on at init, or add it to a loop that is already running:

```bash
hermes review-loop init --repo owner/name ... --observer-profile arbiter   # one flag turns it on
hermes review-loop set --loop widgets --observer-profile arbiter           # or add it later
hermes review-loop set --loop widgets --observer-events verdict,escalation,closed
hermes review-loop set --loop widgets --observer-digest-min 30          # batch instead of pinging
hermes review-loop set --loop widgets --observer-mute                   # quiet, config kept
hermes review-loop set --loop widgets --observer-disable                # stop/remove route; retain owed ledger and old destination binding
```

The flags write this block into the loop file, the only place the feed is configured:

```json
"observer": {
  "route": "widgets-observe",
  "profile": "arbiter",
  "deliver": "telegram",
  "events": ["opened", "handoff", "verdict", "approved", "escalation", "ruling", "stall", "closed"],
  "digest_min": 30
}
```

`init` and `set` write the keys you asked for and nothing else: `mute: true` for a muted feed,
`digest_min` above zero to batch, `events` to narrow the feed (leave it out for all of them).

With `--observer-profile` (or `--observer-route`), `init` also installs the route (`<id>-observe`)
through the seats' own mechanism — the same signed POST at the same gateway — but with
`deliver_only: true` and a two-line prompt, because the notice is *already written*: nothing wakes
an agent, and there is no third seat to hold a lock or take a turn. The eight transitions are:

* `opened`: a new PR needs its first look.
* `handoff`: a fix was pushed and review requested.
* `verdict`: a changes-requested verdict landed. The next turn is `fixer queued`, or, while
  unattended fixer pushes are off (the default), `you — fixer held: unattended fixer pushes are
  off`, followed by the command that turns them on.
* `approved`: the reviewer approved.
* `escalation`: the cap is spent. Next is the adjudicator when the loop has an adjudicator route,
  otherwise you.
* `ruling`: an isolated adjudicator's ruling was recorded.
* `stall`: the watchdog decided a quiet PR is worth reporting.
* `closed`: merged or abandoned; cleanup was attempted, but this notice does not confirm the disk
  was reclaimed. It is sent for every PR closed in the repository, not only the ones the loop
  worked on.

Issue triage and issue-fix turns send no observer notices. When one fails, the watchdog's operator
outbox reports it, like any other failed isolated run.

Four rules keep the feed from becoming a gate:

* **Emitted from state, not from prose.** A notice is written by the gates and the watchdog at the
  transition they just made, and a `ruling` notice by the host broker when it records the ruling.
  Seats run no host scripts, so nothing is ever parsed out of an agent's summary or sent on the
  strength of an agent's claim.
* **One transition, one notice.** The ledger key is loop + PR + head + event + verdict/round
  identity, so a redelivered webhook, a re-run gate or a retried sweep cannot produce a duplicate.
* **Ambiguous delivery is not replayed.** A missing route or secret is a definite pre-POST failure
  and the watchdog retries it (up to three attempts). A timeout or 5xx after posting may have sent
  the notice; it remains `uncertain` in `status` for manual reconciliation, never automatically
  retried. Legacy `failed` receipts without proof of a pre-POST failure are quarantined the same
  way. Neither outcome consumes a seat or blocks the queue.
* **Off means off.** No observer, a muted feed, an event filtered out: the loop behaves exactly as
  it would with no observer at all. A misconfigured feed (a route that was never installed, or a
  bare profile with no route) never refuses a loop — the seats keep running and `status` says what
  is wrong with the feed.

Private PRs are safe to watch this way: the link goes to the chat the operator configured for that
profile, and nowhere else.