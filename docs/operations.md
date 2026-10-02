# Operating a loop

Installing, checking and running a review loop once it is configured: what `init` writes, the
day-to-day verbs, the `doctor` preflight, the `selftest` of the isolated turn path, `explain` for a
PR that is not moving, and how a burst of PRs is queued. The keys themselves are in the
[configuration reference](configuration.md); the design behind each tool is in
[architecture](architecture.md). The quick install is in the [README](../README.md#install).

## What `init` writes

When the plugin settings name the seats (`fixer_profile`, `reviewer_profile`, `reviewer_login`,
`fixer_login`, `adjudicator_profile`), the seat flags of the [`init` example](../README.md#install) become optional — the form supplies them
and an explicit flag still wins. `--dry-run` prints the whole plan (who serves each seat, the route
URLs, what would be written) and stops there, which is the way to check a form before it reaches a
running loop.

`init` writes exactly four things, all of them visible and reversible:

1. one loop config — `~/.hermes/review-loops.d/<id>.json`
2. three webhook routes — `<id>-review`, `<id>-fix`, `<id>-breach` — into the gateway's own
   `webhook_subscriptions.json` (generated prompts, generated secrets, file left at 0600)
3. two GitHub hooks, on `pull_request` and `pull_request_review`, pointing at those routes —
   created **paused**, so nothing fires until `arm` (after `doctor` and `selftest`); `--arm` creates
   them live instead
4. one cron job plus a 5-line shim in `~/.hermes/scripts/` that forwards to the plugin's watchdog
   (`--schedule`). It is **one shared job for every loop**: `hermes cron create --script` takes a
   filename and no arguments, so a job cannot carry `--loop <id>`, and the shim runs the watchdog
   with no `--loop` — it sweeps every configured loop exactly once per tick. A later loop's
   `init --schedule` sees the job and does not create a second one (#60). If `hermes cron create`
   fails, `init` prints the scheduler's error and the exact
   command to run yourself (shell-quoted, pasteable as printed), skips the "Next:" list, and exits
   **1** — the config, routes and hooks above are in place; only the job is missing

Route edits are serialized only among cooperating review-loop plugin processes, using a sibling
lock file and atomic replacement. Native Hermes CLI and dashboard subscription edits do **not**
take that lock (issue #1; upstream fix pending in NousResearch/hermes-agent#120964), so the plugin
mitigates the race from its side — it does not close it:

* **Optimistic writes.** Each plugin edit records the registry's inode, mtime, size and content
  hash when it reads, re-checks them immediately before `os.replace`, and re-reads and re-applies
  its edit (up to 5 attempts, then `RegistryConflictError` with nothing published) if a native
  write landed in between — so the plugin no longer overwrites a concurrent native change.
* **Intent record + self-heal.** Every route the plugin installs or rebinds is also copied,
  secret included, to `<state_dir>/route-intent.json` (0600, atomic). Every armed watchdog sweep
  compares this loop's routes with it and restores any route a native writer erased or changed
  (secret, script, prompt, events, profile, `deliver_only`, host) with the **same secret**, so
  GitHub's hook keeps authenticating, and says what it restored in its cron output. Other routes
  are never touched; a name now held by a non-review-loop script is reported, not overwritten; a
  malformed registry is never overwritten. Change or remove routes through `set`/`apply`/
  `uninstall` — they update the record — or self-heal will put a native edit back.
* **What remains.** A few syscalls between the final identity check and the rename, and a native
  writer that read *before* a plugin publish and writes *after* it, can still drop a plugin edit
  (or a native one). The plugin's lost routes come back on the next armed sweep; a native edit
  the plugin overwrote in that window does not. Between sweeps a broken route can miss
  deliveries. See [docs/issue-1-route-self-heal.md](issue-1-route-self-heal.md).

If directory sync
fails after replacement, the plugin raises `RegistryDurabilityError(published=True)`: the new
registry is visible, but crash durability is unconfirmed; do not assume the operation rolled back.

## First run

1. `init` the loop (above), then give each seat's profile its token file.
2. `hermes review-loop doctor --loop name` until every line is ✅ (or a ⚠️ you have decided on).
3. **Decide the fix leg.** A new loop has unattended fixer pushes **off**, and while they are off a
   changes-requested verdict starts **no** fixer turn — a turn that cannot publish would only spend
   a model conversation and fail. The verdict is held for you instead: the fixer queue entry, the
   observer's `verdict` notice (`next: you — fixer held …`), `explain` (next: `operator decision`),
   `doctor` (`⚠️ fixer-push off — the fix leg cannot run`) and the watchdog (one `fixer held` stall
   per head) all say so and name the command. To let the fixer answer verdicts:

   ```bash
   hermes review-loop fixer-push --loop name --enable --acknowledge-pr-race
   ```

   Read the PR-metadata/ref race it acknowledges ([README](../README.md), and
   [issue-16-boundary](issue-16-boundary.md#unattended-fixer-push-policy-host-operator-not-github-owner-consent))
   first. A verdict that was held before you opted in needs no new review: the next watchdog sweep
   (or `hermes review-loop drain --loop name --seat fixer`) re-checks that it is still the live
   latest verdict at the PR's current head and starts a fix run, admitted under the policy as it
   is *now*. Held verdicts never create a run-ledger row, so this is a fresh admission, not a later
   opt-in upgrading an older run. Or keep pushes off and answer verdicts by hand: push the fix and
   re-request review.
4. `hermes review-loop arm --loop name` — flipping a hook needs hook *write* access, as the
   reader unless `--admin-token <login>` names another. On a user-owned repo only the owner can
   manage hooks, and the reader is usually the owner, so give its file `repository_hooks: write`
   (or leave hooks to the web UI and keep it read-only); on an org repo, `--admin-token` can name a
   separate admin login mapped at `init`.

## Everyday commands

```bash
hermes review-loop list                 # what is configured
hermes review-loop status --loop name   # seats, profiles, routes, live runs, queue, breaches
hermes review-loop explain --loop name --pr 123   # why that PR is not moving, and what is next
hermes review-loop trace --loop name --delivery <id> --admin-token LOGIN   # dry-run one webhook through its gate
hermes review-loop doctor --loop name   # preflight the install: profiles, seat models, tokens, routes, hooks, cron
hermes review-loop models --seat reviewer --loop name   # what that seat's profile's provider offers (read-only)
hermes review-loop settings             # the plugin-level defaults, and where each came from
hermes review-loop init --repo owner/name --read-token reader-bot --token reader-bot=~/.hermes/keys/reader-bot-pat --dry-run   # preview a loop: seats, routes, nothing written
hermes review-loop apply --loop name    # push those defaults onto an existing loop (--dry-run)
hermes review-loop apply --loop name --while-busy     # rebind even while a seat has a run out
hermes review-loop set --loop name --reviewer-concurrency 2   # two reviews at once, one fix at a time
hermes review-loop set --loop name --fixer-turn-budget 1800   # let a fix run (build + tests) for 30 minutes
hermes review-loop set --loop name --attribution off   # stop signing what the loop posts (on by default)
hermes review-loop arm --loop name      # arm/pause by flipping the repo hooks (--admin-token LOGIN)
hermes review-loop arm --loop name --pause
hermes review-loop drain --loop name --seat reviewer
hermes review-loop fixer-push --loop name --enable --acknowledge-pr-race   # let the fixer publish (off by default)
hermes review-loop retry --loop name --pr 123   # re-arm a run that failed before any GitHub write
hermes review-loop cleanup --loop name --dry-run   # every closed PR; --pr N for one
hermes review-loop uninstall --loop name   # deletes its repo hooks and cron job first, then routes and config
hermes review-loop uninstall --loop name --admin-token LOGIN --purge   # hook-admin token; also the default state dir
```

`arm` and `arm --pause` never report what they asked for — after each PATCH they read the hook back
and print the state GitHub shows (`hook 12 → paused (read back)`, or `hook 12 is still active, not
paused: PATCH failed (HTTP 403 …)`), and a `fix:` line for each thing that failed (a run that
succeeds prints none). They exit **0** only when both seats'
hooks (reviewer and fixer) exist and every one was observed in the requested state (a hook whose
listing shows that state as a real true/false counts), **1** on a refused or unconfirmed PATCH, a
read-back that disagrees, an unreadable hook listing, no loop hooks on the repo, or one seat's hook
missing — named per seat as `hook:<route> ABSENT (fixer seat)`, the way `doctor` names it, since a
loop armed halfway is not armed — and **2** when the loop cannot be loaded: no loop of that name,
none configured, or a loop file `normalize` refuses (a `ConfigError` on load — a hand-edited file
missing `seats.reviewer.route`, say, a `cap` that is not a whole number, or both seats on one
route — printed as `cannot arm: …`).
A hook is a seat's only when it posts to exactly that seat's route URL as the route registry
(the gateway's subscription file) serves it — `/p/<profile>/webhooks/<route>`, or
`/webhooks/<route>` for the default profile, on the loop's gateway (scheme and host compared
case-insensitively, the path exactly: the gateway registers no other shape, so a trailing slash is
a 404) — and that registry entry binds the seat's own profile. The gateway binds a
route to its profile by that URL and answers any other profile's URL 404, so a hook at another
profile, another path or another origin (a retired gateway, another install) is listed with that
cause, never flipped, and leaves its seat ABSENT; so does a seat whose route is missing from the
registry, or bound to another profile ("the wake would run the wrong agent"), in `doctor`'s
words — and `arm` will not arm a hook at the seat's URL that subscribes to the wrong event, does
not post JSON, or ends in a trailing slash (`doctor`'s MISMATCH), since it would never wake the
seat; pausing and `uninstall` still recognise such a hook as this install's (and stop or delete it). Pausing is stricter about
*whose* hook it is and looser about the route: `arm --pause` stops every hook this install made —
at the route's registry URL, or the URL the loop config gives it — even once the route is gone
from the registry (as `uninstall` leaves it), and still never touches another install's. The `fix:` advice is per hook: a
failed PATCH that looks transient (a timeout, a 5xx) gets "retry `arm`" first, a refusal (401,
403, 404, or a PATCH that did not stick) the token-scope line, each naming its hooks. Without `--admin-token` the PATCH
goes out as the loop's `read_token`; the `fix:` line names the scope that login's file needs
(`repository_hooks: write`, `admin:repo_hook` or classic `repo`) and, when it is the reader, the
owner case above.

`uninstall` deletes the loop's repo hooks and its watchdog job *before* it removes the routes and
the config, and reads both back. If it cannot (a token without `admin:repo_hook`/`repo`, an API
failure, a job the scheduler will not remove) it refuses, changes nothing else, and prints the
exact `gh api -X DELETE …` / `hermes cron remove …` commands; `--keep-hooks` is the explicit
opt-out. `uninstall` deletes only hooks at the loop's own route URLs; a hook on the same route
name at another origin, profile or path is reported with that cause and left alone. It concludes
"no hooks left" only from a listing read after its DELETEs (or after finding none), so a hook
created meanwhile is caught and refused; when a listing cannot be read or confirmed it says so
and prints commands to *look*, never DELETE commands for hooks it has not seen. A loop with no
`host` has no route URL to compare with, so `uninstall` reads the listing first: when any hook
posts to the loop's route names (a host blanked by hand leaves its hooks behind — or they may be
another install's on the same repo) it refuses with exit 2 and the commands to *look* at them,
never DELETE commands it cannot justify; an entry without an integer id refuses too; it goes on
only when none does. `set --host` refuses a blank or invalid origin rather than blanking it. `--purge` refuses
up front, untouched, when the state directory cannot even be read for its in-flight check. It
deletes the state directory last; if that delete — or removing the config before it — fails (a
permission, a busy mount) it exits 2 with `uninstall INCOMPLETE — removed: …; left behind: …` and
the exact `rm` commands that finish it — the config may already be gone by then, so a re-run
cannot. A cron shim that cannot be removed (or is a symlink, which is never followed) does the
same: the run ends INCOMPLETE (exit 2) with the shim under "left behind" and its `rm` command,
alongside anything else that stayed. Every shared file a later step rewrites — the route
registry — is read before the first hook is touched, so an unparseable one refuses with nothing
removed; a route step that still fails later (a registry that changes or becomes unwritable
mid-run) ends INCOMPLETE with the config kept, so re-running `uninstall` finishes it. A loop file
the loader refuses is never torn down on a guess: `uninstall` exits 2 with the reason and the
commands to look up what it may still have live (its hooks' ids, its watchdog job). When the
loop removed was the last one configured, `uninstall` also forgets the run ledger's presence
marker (`review-loops.d/.ledger-present`, after `--purge` has removed the state directory), so a
later fresh install is not reported as a vanished ledger. `init --hooks` refuses when hooks from a previous install still post to the loop's
routes (they sign with a secret the new routes will not hold), and `doctor` fails a route with
more than one hook, or whose latest delivery the gateway answered 401/403 (a secret that does not
match) or any other non-2xx (a 5xx is the gateway erroring); a latest delivery with no HTTP answer
at all (timed out, refused) is reported as unproven, never as green. After `arm` (and `init --hooks --arm`) activates the hooks it asks GitHub to **ping** each
one and waits up to 10s for the delivery: `✅ … signature accepted`, `❌ … HTTP 401 — signature
rejected` (exit 1), or `⚠️ no ping delivery seen` (nothing proven yet). A ping is harmless: the
gateway checks its signature, then ignores it, because the loop's routes subscribe only to
`pull_request` / `pull_request_review`. `doctor` never pings; `selftest` reads the recorded
deliveries and pings only with `--ping` — only hooks at the loop's own route URLs, matched the way
`arm` and `uninstall` match them, never another install's or another profile's hook on the same
route name (its single
GitHub write, e.g.
`hermes review-loop selftest --loop name --no-model --ping --admin-token LOGIN`).

`set` is how you change the knobs after install — `--reviewer-concurrency`, `--fixer-concurrency`,
`--concurrency` (the default for both seats, except a seat given its own value), `--cap`, `--clone`, `--base`, `--grace-min`,
`--ttl-min`, `--turn-budget` (and per seat `--reviewer-turn-budget` / `--fixer-turn-budget`) —
through the same validation `init` uses, so a capacity above 1 without a clone is
refused here exactly as it is at init. Prompts are rendered from the payload at fire time, so a
change takes effect on the next event with nothing to re-install. The observer feed is changed the
same way: `--observer-profile`, `--observer-route`, `--observer-deliver`, `--observer-events`,
`--observer-digest-min`, and `--observer-mute` / `--observer-unmute` / `--observer-disable`.

`set --adjudicator-login LOGIN --token LOGIN=/abs/path` names (or `--adjudicator-login ""`
clears) the optional account a ruling is also posted as, and `set --read-token LOGIN
[--token LOGIN=/abs/path]` moves the reader; `set --token` maps only those two logins. Both go
through the [four-identity rule](#token-files-one-pat-per-account).

Who serves each seat, and how the plugin-level defaults reach a loop, is covered in
[Settings, in the desktop](settings.md).

## What the loop signs

Everything the loop itself posts says so, unless you turn it off:

| write | how it is signed |
|---|---|
| the reviewer seat's review | a footer: `🤖 Automated by hermes-review-loop · reviewer seat (Vex) · head abc1234`, linking to this project |
| the fixer seat's answers comment | the same footer, naming the fixer seat |
| the adjudicator's ruling comment (with `seats.adjudicator.login`) | the same footer |
| a commit the fixer seat pushes | an `Automated-By: hermes-review-loop (…)` trailer |

The host adds it at the moment it sends the write, never the seat, so a seat can neither remove it
nor pre-empt it. And it marks only what the loop sends: a PR your coding agent opens by hand, or a
review a person writes, is never touched. The seat's agent name appears as letters, digits and
spaces only. A footer cannot become a link, markup or a second line.

It is a label for people reading the PR, not evidence: anyone can type the same words, so the loop
never treats a footer as proof that it posted something. Its receipts and run ledger do that. When a
seat reads the PR's history, the loop's own footers are dropped so the seat sees the words, not the
label.

Turn it off per loop with `hermes review-loop set --loop name --attribution off` (and back on with
`on`), or for new loops in the settings form (**Sign what the loop posts**). `status` shows
`signed: on|off`, and `doctor` has an `attribution` line. If the form names the setting, `apply`
pushes it like any other knob, so set the form to match if you opt out with `set`.

## Token files: one PAT per account

Each GitHub account the loop uses gets its **own PAT in its own file**, and the loop config (or the
settings form) holds only the path. The scopes each role needs are in
[Token scopes by role](#token-scopes-by-role) below.

```bash
(umask 077; mkdir -p ~/.hermes/keys)   # paste each account's PAT into its own <login>-pat file
chmod 600 ~/.hermes/keys/*-pat

hermes review-loop init --repo owner/name \
  --fixer dev-account --reviewer rev-bot \
  --fixer-profile drey --reviewer-profile vex \
  --read-token reader-bot \
  --token reader-bot=~/.hermes/keys/reader-bot-pat \
  --token rev-bot=~/.hermes/keys/rev-bot-pat \
  --token dev-account=~/.hermes/keys/dev-account-pat \
  --adjudicator-route name-breach --adjudicator-profile tuck \
  --adjudicator-login rule-bot --token rule-bot=~/.hermes/keys/rule-bot-pat \
  --host https://your-gateway.example
```

* **Mode 600, owned by you, absolute path.** The settings form's `*_token_file` fields and the
  adjudicator's `--token` are refused unless the path is absolute after `~` expansion, exists, is a
  regular file you own, and is not group/world readable. The check reads metadata only.
* **The four-identity rule.** The reader, the reviewer, the fixer and (if set) the adjudicator
  comment login must be four different accounts with four different token files — the broker
  re-checks distinct `/user` principals before each write. A shared file is one account wearing
  two hats, and the review loop exists so a different account reviews the fixer's work.
* **Never a token value.** `status`, `settings`, `doctor` and every refusal print paths; `doctor`
  reports each seat's file as `path (exists: yes, private: yes)`.

## Token scopes by role

A token has to be able to do its seat's job, and on a repo owned by a **user account** there is no
narrower option than the broad scope. That is a property of the platform, not a choice this loop
makes.

| role | account | what it does | token | why nothing narrower works |
| --- | --- | --- | --- | --- |
| reader (`read_token`) | the repo owner | reads PRs, refs and the repo's hooks | **fine-grained, read-only**: `contents: read`, `pull_requests: read`, `repository_hooks: read` (classic `repo` also works). When the reader also creates and arms the hooks — `init --hooks` / `arm` without `--admin-token`, as in the README example — make that `repository_hooks: write` | the owner *is* the fine-grained token's resource owner, so this is the one seat that can hold a read-only credential on a user-owned repo. `doctor` and `explain` read the hooks to tell *armed* from *paused*; without hook read access the line reports the state as unknown |
| reviewer | collaborator (write) | posts one review | classic, `repo` | a review POST needs pull-request write, and on a user-owned repo that is the same permission that can push code |
| fixer | collaborator (write) | pushes a fix commit | classic, `repo` | the fix is a commit |
| adjudicator login | collaborator (write) | posts one comment | classic, `repo` | a comment needs only read, but a user-owned repo refuses a read-only collaborator grant (`422`), so the account can write whatever its token says |

* **A fine-grained PAT cannot serve any of the *collaborator* seats** — the reviewer, the fixer and
  the adjudicator login. GitHub's documented gap: a fine-grained token cannot "contribute to
  repositories where the user is an outside or repository collaborator", and it is bound to a single
  *resource owner*; for a repo you do not own that owner is a different account, i.e. a different
  identity, exactly what the four-identity rule forbids. The **reader is the exception**: it is the
  owner, and an owner can scope a fine-grained token to their own repo with read-only permissions —
  which is the least-privilege credential worth using where it is available.
* **Scope cannot make a reviewer or an adjudicator safe.** If the account can post a review or a
  comment it can also push; there is no token shape on a user-owned repo that separates the two.
  What keeps a seat's write credential away from a turn is the
  [boundary](issue-16-boundary.md) — the sandboxed agent never receives the token, and one seat's
  proxy holds only that seat's credential — plus one PAT per login, so a leak or a rotation touches
  one seat. Give the adjudicator its own file even though its account can push: the loop then never
  holds the credential a seat pushes with.

**Org-owned repos** narrow the same roles to fine-grained tokens, which is the practical reason to
move a repo you intend to run a loop on into an organization:

| role | fine-grained permissions |
| --- | --- |
| reader | `contents: read`, `repository_hooks: read` |
| reviewer | `pull_requests: write` |
| fixer | `contents: write`, `pull_requests: write` |
| adjudicator | `pull_requests: write` |

An org repo also has a read-only collaborator role, so an account that only reads needs no write
permission at all.

In both cases: absolute path, mode 600, one file per login — and know each token's expiry, because a
seat whose PAT lapsed mid-turn fails as an authentication error that reads like a code bug. Ask
GitHub rather than trusting a note:

```bash
GH_TOKEN=$(cat ~/.hermes/keys/<login>-pat) gh api -i / | grep -i github-authentication-token-expiration
```

Not `curl -H "Authorization: token $(cat …)"`: that puts the token in the command's argument list,
where `ps` can read it while the call runs. An environment variable does not.

A `repo`-scoped classic PAT does **not** carry `workflow` — but a seat never gets that far: the
broker refuses any path under `.github/`, along with `.gitmodules`, `.gitattributes` and
`CODEOWNERS`, before it invokes git at all. Workflow edits are always a human's, made with a
credential that has `workflow`.

Creating the repo's hooks (`init` with `--hooks`, or `apply`) needs hook *write* **and delete**
access, which a classic `repo` token already has: `admin:repo_hook` is the narrower hooks-only
scope, not an extra requirement on top of `repo`. It is `admin:repo_hook` and not the narrower still
`write:repo_hook` because a failed install rolls back: `init --hooks` deletes the hooks it already
created and re-reads the listing to confirm they are gone (`_install_hooks` in `review_loop/cli.py`),
so a write-only token turns a partial failure into an orphaned hook and a `ROLLBACK FAILED` report.
An owner's fine-grained reader token needs `repository_hooks: write` to create them — that permission
offers only read and write, so `write` is what covers the rollback — so either widen that file once
or create the hooks with the owner's classic credential.

## Preflight: `doctor`

`init` writes the config, the routes and (optionally) the hooks and the cron job — but a
syntactically valid file is not proof that any of it can run. The reviewer's profile may not
exist, the token file named for the fixer may have gone in a key rotation, the route in the
gateway registry may wake a *different* profile than the loop config says, the repo hook may
point at your previous gateway, or the cron shim may still be pinned to the plugin directory a
previous upgrade left behind. Every one of those is a loop that looks armed and cannot wake a
seat — so `doctor` checks the installation itself, read-only, before anyone arms it:

```bash
hermes review-loop doctor --loop attest             # one loop; without --loop it preflights them all
hermes review-loop doctor --loop attest --offline   # skip the gateway probe and the hooks read
hermes review-loop doctor --loop attest --strict    # an undecided check counts as a failure
```

One line per check, in one of these states:

| state | meaning |
|---|---|
| ✅ verified | checked, and correct |
| ❌ absent | the thing is not there — a missing profile, token file, route, hook, job or script |
| ❌ mismatch | present, but not what this loop needs — a route waking another profile, a hook on another gateway, a shim pinned to a stale plugin path, a world-readable PAT |
| ⚠️ unknown | could not be decided *from here* — a hooks read the token was not allowed to make, or a probe skipped with `--offline` — or a decision still yours to make: `fixer-push` is ⚠️ while unattended fixer pushes are off, because the fix leg cannot run (verdicts are held for you) |
| ➖ skipped | not checked because another line already fails for the same cause — `extras:<seat>` while `model:<seat>` is ❌ (no provider to check); neither a pass nor a failure |

Each failure is followed by the one command that fixes it, failures exit 1, and `unknown` is never
reported as `absent`: "the API refused to tell me" and "there are no hooks" are different claims,
and printing the second when the first is true sends you hunting for a hook that exists (reading the
repo's hooks needs hook read access, which classic `repo` or the narrower `read:repo_hook` grants, so
a token without either shows ⚠️, not ❌).

It writes nothing — no config, no route registry, no state, no GitHub hook — unless you pass
`--repair`, whose one write is restoring this loop's own routes from the plugin's intent record
(same secret) before the read-only checks run, and the gate shims those routes run. A route
with no intent record to restore it from (an install older than the record) is written back by

```
hermes review-loop apply --loop attest --recreate-routes
```

from the loop config, with a new secret — the old one left with the route — and one repo hook for
that route is re-keyed to it in the same step, moved to the route's URL if it was left at the loop's
previous one (another profile or host).

Whenever `apply` re-keys or moves a route's hooks, and on every plain `apply`, it keeps exactly one
hook per route: an **active** hook first (a route never ends with fewer armed hooks than it had),
then one already at the route's URL, then the oldest (lowest id). Every other hook for that route is
left where it is — never duplicated onto the route's URL, never deleted by the plugin — and named
with the `gh api -X DELETE repos/<repo>/hooks/<id>` command that removes it; `apply` then exits 1. `init` refuses a loop that exists, so it is
never the way back. `doctor` never fires a
route, because a synthetic POST at a seat's route is a real agent run with a real budget. The
network side is a TCP connect to the gateway (is anything listening?) and, when the token is
allowed to, a read of the repo's hooks.

A correct installation:

```
$ hermes review-loop doctor --loop widgets
[widgets] acme/widgets — preflight (read-only: it writes nothing and fires nothing)
  ✅ config               doctor-demo/loops/widgets.json (repo acme/widgets, cap 3, base main)
  ✅ turn-budget          reviewer 900s · fixer 900s · adjudicator 900s per isolated turn (sandbox killed past it); up to 1830s launch to end (300s dependency prefetch + 900s budget + 30s kill grace + 600s broker drain) — grace_min 35m, ttl_min 45m; stall grace reviewer 35m · fixer 35m; seat lock TTL reviewer 45m · fixer 45m · adjudicator 45m; 'that run died' after twice that; breach marker: awaiting-adjudication stalls after 60m; adjudicating only with no live ruling run, after 60m
  ✅ profile:reviewer     reviewer-profile → doctor-demo/hermes-home/profiles/reviewer-profile
  ✅ credential:reviewer  rev-coach → doctor-demo/rev.pat (exists: yes, private: yes), nonempty (identity and API access not checked)
  ✅ profile:fixer        fixer-profile → doctor-demo/hermes-home/profiles/fixer-profile
  ✅ credential:fixer     dev-fixer → doctor-demo/fix.pat (exists: yes, private: yes), nonempty (identity and API access not checked)
  ✅ profile:adjudicator  default → doctor-demo/hermes-home
  ✅ model:reviewer       profile reviewer-profile: openrouter / test-model [chat_completions, API key] (credential checked by selftest)
  ✅ model:fixer          profile fixer-profile: openrouter / test-model [chat_completions, API key] (credential checked by selftest)
  ✅ model:adjudicator    profile default: openrouter / test-model [chat_completions, API key] (credential checked by selftest)
  ✅ extras:reviewer      openrouter [chat_completions] needs no optional Hermes package
  ✅ extras:fixer         openrouter [chat_completions] needs no optional Hermes package
  ✅ extras:adjudicator   openrouter [chat_completions] needs no optional Hermes package
  ✅ fixer-push           enabled — a changes-requested verdict starts an isolated fixer turn that can publish one push and re-request review
  ✅ token:dev-fixer      doctor-demo/fix.pat (mode 600, non-empty)
  ✅ token:read-acct      doctor-demo/read.pat (mode 600, non-empty)
  ✅ token:rev-coach      doctor-demo/rev.pat (mode 600, non-empty)
  ✅ read_token           read-acct (mapped in tokens; its own account and file)
  ✅ route:widgets-review reviewer-profile · pull_request · [webhook URL redacted]
  ✅ route:widgets-fix    fixer-profile · pull_request_review · [webhook URL redacted]
  ✅ route:widgets-breach default · adjudication wake
  ✅ scripts              /path/to/hermes-review-loop/scripts (watchdog, three gates, cleanup)
  ✅ cron:shim            doctor-demo/hermes-home/scripts/review-loop-watchdog.py → /path/to/hermes-review-loop/scripts/watchdog.py
  ✅ cron:job             watchdog-job every 15m, next 2026-01-01T00:00:00Z
  ✅ clone                doctor-demo/clone (git checkout)
  ✅ state_dir            doctor-demo/state (created under doctor-demo on the first run)
  ✅ roots                2 configured: doctor-demo/reviews, doctor-demo/scratch
  ✅ gateway              configured gateway accepts a TCP connection (URL withheld)
  ✅ hook:widgets-review  hook 41 → [webhook URL redacted] (pull_request, active)
  ✅ hook:widgets-fix     hook 42 → [webhook URL redacted] (pull_request_review, active)

widgets: 29 verified, 0 failed, 0 unknown (of 29 checks)
  every check passed — this loop can wake a seat and post a verdict.
```

and the same loop with the ways it really breaks — a missing fixer profile (so its model is
unresolved and its `extras:` line is skipped), a reviewer on a Claude provider whose venv lacks
the `anthropic` package, a route registered at an old gateway, a route waking the wrong profile
(and the hook that no longer matches it), a stale shim and a paused watchdog job:

```
$ hermes review-loop doctor --loop widgets
[widgets] acme/widgets — preflight (read-only: it writes nothing and fires nothing)
  ✅ config               doctor-demo/loops/widgets.json (repo acme/widgets, cap 3, base main)
  ✅ turn-budget          reviewer 900s · fixer 900s · adjudicator 900s per isolated turn (sandbox killed past it); up to 1830s launch to end (300s dependency prefetch + 900s budget + 30s kill grace + 600s broker drain) — grace_min 35m, ttl_min 45m; stall grace reviewer 35m · fixer 35m; seat lock TTL reviewer 45m · fixer 45m · adjudicator 45m; 'that run died' after twice that; breach marker: awaiting-adjudication stalls after 60m; adjudicating only with no live ruling run, after 60m
  ✅ profile:reviewer     reviewer-profile → doctor-demo/hermes-home/profiles/reviewer-profile
  ✅ credential:reviewer  rev-coach → doctor-demo/rev.pat (exists: yes, private: yes), nonempty (identity and API access not checked)
  ❌ profile:fixer        no profile home at doctor-demo/hermes-home/profiles/fixer-profile
      fix: `hermes profile create fixer-profile`, or re-run init with --fixer-profile pointing at a profile that exists: the run happens as this profile
  ✅ credential:fixer     dev-fixer → doctor-demo/fix.pat (exists: yes, private: yes), nonempty (identity and API access not checked)
  ✅ profile:adjudicator  default → doctor-demo/hermes-home
  ✅ model:reviewer       profile reviewer-profile: anthropic / claude-sonnet-4-6 [anthropic_messages, API key, or Claude subscription OAuth (host-refreshed)] (credential checked by selftest)
  ❌ model:fixer          profile fixer-profile does not exist at doctor-demo/hermes-home/profiles/fixer-profile (or has no config.yaml); the fixer turn will be held
      fix: set a supported provider in profile fixer-profile (see `docs/configuration.md`), or add seats.fixer to the runtime file
  ✅ model:adjudicator    profile default: openrouter / test-model [chat_completions, API key] (credential checked by selftest)
  ❌ extras:reviewer      anthropic [anthropic_messages] needs the Hermes extra `anthropic` (import anthropic) (anthropic is always on anthropic_messages), which doctor-demo/doctor-venv/bin/python cannot import; the reviewer turn would fail at model setup
      fix: `hermes pm install --extra anthropic` (Hermes's own command for a missing extra) — it installs into the venv Hermes selects, so if that is not doctor-demo/doctor-venv, install the extra into doctor-demo/doctor-venv or point `venv` in doctor-demo/hermes-home/review-loop-runtime.json at the venv that has it; then re-run doctor
  ➖ extras:fixer         skipped: model unresolved (see model:fixer)
  ✅ extras:adjudicator   openrouter [chat_completions] needs no optional Hermes package
  ✅ fixer-push           enabled — a changes-requested verdict starts an isolated fixer turn that can publish one push and re-request review
  ✅ token:dev-fixer      doctor-demo/fix.pat (mode 600, non-empty)
  ✅ token:read-acct      doctor-demo/read.pat (mode 600, non-empty)
  ✅ token:rev-coach      doctor-demo/rev.pat (mode 600, non-empty)
  ✅ read_token           reader-bot (mapped in tokens; its own account and file)
  ❌ route:widgets-review registered gateway origin differs from the loop's configured origin (URLs withheld)
      fix: run `hermes review-loop apply --loop widgets` to rewrite it at the loop's origin (its secret is kept)
  ❌ route:widgets-fix    wakes profile 'some-other-agent', but seats.fixer.profile is 'fixer-profile' — the wake would run the wrong agent
      fix: run `hermes review-loop apply --loop widgets` to rebind it to fixer-profile (its secret is kept)
  ✅ route:widgets-breach default · adjudication wake
  ✅ scripts              /home/jeremy/projects/rl-15-doctor/scripts (watchdog, both gates, cleanup)
  ❌ cron:shim            pinned to /opt/old/plugins/hermes-review-loop/scripts/watchdog.py, this install runs /home/jeremy/projects/rl-15-doctor/scripts/watchdog.py
      fix: `hermes review-loop apply --loop widgets --watchdog-shim` rewrites it from the plugin (the scheduled job runs it by name)
  ❌ cron:job             8f21c0 (review loop watchdog) is paused
      fix: `hermes cron resume 8f21c0`: a paused watchdog never reports a stall
  ✅ clone                doctor-demo/clone (git checkout)
  ✅ state_dir            doctor-demo/state (created under doctor-demo on the first run)
  ✅ roots                2 configured: doctor-demo/reviews, doctor-demo/scratch
  ✅ gateway              configured gateway accepts a TCP connection (URL withheld)
  ✅ hook:widgets-review  hook 41 → [webhook URL redacted] (pull_request, active)
  ❌ hook:widgets-fix     hook 42 posts to another profile ('fixer-profile'; the route is bound to 'some-other-agent', and the gateway answers any other profile's URL 404), not [webhook URL redacted]
      fix: re-run init --hooks, or repoint hook 42 at the route's URL: the gateway delivers this route only at that URL, so the hook wakes nothing

widgets: 20 verified, 8 failed, 0 unknown, 1 skipped (of 29 checks)
  8 failed: profile:fixer, model:fixer, extras:reviewer, route:widgets-review, route:widgets-fix, cron:shim, cron:job, hook:widgets-fix — fix the ❌ lines above before this loop is armed.
```

Every fix on a route, hook or cron line is a command that works on a loop that already exists
(`init` refuses one): `apply` rewrites the loop's routes and their origin, `apply --hooks` makes
its two repo hooks what the routes need (a missing one is created paused until `arm`), and
`apply --watchdog-shim` rewrites the cron shim:

```
hermes review-loop apply --loop widgets --hooks --admin-token admin-login --dry-run
hermes review-loop apply --loop widgets --watchdog-shim
```

(Both transcripts are real output from the suite's isolated demo home — a loopback gateway
sink for the probe, a stubbed GitHub, a runtime venv without the `anthropic` package — with the
demo home shortened to `doctor-demo` and the plugin checkout to `/path/to/hermes-review-loop`. A
run against a live install prints the same lines with absolute paths and the real hook list.)

Each seat needs its own GitHub token, and that is deliberate: the token that reviews, the token
that pushes and the token that reads are separate and revocable one at a time
([scopes by role](#token-scopes-by-role)). A loop that names tokens must name one per seat, and the
file has to be there — checked before `init` or `apply` writes anything, because a missing PAT
otherwise surfaces hours later as an unauthenticated read.

The checks and the reasoning behind them are in
[architecture: Preflight](architecture.md#preflight-can-this-installation-run).

## Verifying the isolated setup: `selftest`

`doctor` checks the installation; `selftest` checks the **isolated turn path** (issue #16) against
the real capabilities, one ✅/❌ line per check with the fix under each failure. It exits 1 on any
failure. **It never writes to GitHub**: every GitHub call it makes goes through a GET-only guard,
and the live turn's broker runs in a host-only no-write mode. No token or key is printed.

Run these in order (replace `ID` and `N`; `N` should be an open, non-draft, same-repository PR
targeting the loop's base):

```bash
# 0. the private runtime file the production worker reads (host paths; each seat's model comes
#    from its Hermes profile — see configuration.md#runtime-file-and-seat-models-review-loop-runtimejson)
(umask 077; touch ~/.hermes/review-loop-runtime.json); chmod 600 ~/.hermes/review-loop-runtime.json; $EDITOR ~/.hermes/review-loop-runtime.json
hermes review-loop doctor   --loop ID                       # installation preflight
hermes review-loop selftest --loop ID --no-model            # 1,2,4,6: runtime, bwrap, identities, ledger — free
hermes review-loop selftest --loop ID --pr N                # + one tiny completion per seat model + broker dry run
hermes review-loop selftest --loop ID --pr N --live-turn    # + one real isolated reviewer turn, NOT posted
python -m review_loop.run_supervisor status ~/.hermes/state/review-loop-runs.sqlite
```

A detached worker's stderr goes to `~/.hermes/state/review-loop-runs.sqlite.workers.log`, which the host opens for it and rotates once to `.1` past 256 KiB. A worker never creates host state. If its ledger or a state directory is gone, replaced or unusable, it writes one `review-loop worker …: …; nothing to run` line there and exits. If the ledger vanishes, alone or with the whole state directory, the host's next open of it (a gate enqueue, the watchdog sweep, `selftest`, or `run_supervisor status`) recreates it empty, says so on stderr, and the watchdog delivers one ⚠️ notice about it. The host knows a ledger existed from `review-loop-runs.sqlite.present` beside it and from `~/.hermes/review-loops.d/.ledger-present`, which survives a wiped state directory. `uninstall` of the last loop removes the latter, so a later fresh install is not reported.

| step | what it proves |
|---|---|
| 1 runtime | the runtime file is a regular 0600 file you own with `source venv runtime rust` (plus optional `seats.<seat>` overrides and the legacy `model upstream key_file`, warned about), the paths exist (the venv's interpreter link must stay inside `runtime`), and any override has an HTTPS `…/chat/completions` upstream and a private non-empty key file; then one `seat:<seat>` line per seat — reviewer, fixer, and the adjudicator when it has a route — with the profile → provider / model the worker will use and its `[api_mode, API key \| OAuth (host-refreshed)]` (never the key or token), or the reason that seat's turn would be held |
| 2 bubblewrap | unprivileged user namespaces work; a probe in the real sandbox layout (committed source snapshot, configured venv/runtime/Rust) cannot read a dummy host secret, any model key file, each seat profile's `.env`/`auth.json`/`config.yaml`, the PATs, the runtime file, `~/.hermes/.env` or the loop config, and has no network or credential-like env |
| 3 inference | one ~16-token request in the seat's own wire format (chat completion, Responses or Messages) through the host inference capability **per distinct seat resolution** (seats that share a profile's provider, model and credential share one call), each with that resolution's own credential; an OAuth seat's 401 is refreshed and retried once on the host before it is reported (`--no-model` skips it) |
| 4 identities | read, reviewer, fixer (and optional adjudicator) PATs resolve via `/user` to the expected logins and distinct principals; the repo is readable |
| 5 authorization | with `--pr N`: the broker's reviewer-write checks (`broker.authorize`, reads only) and the host receipt generation; then whether the seat can build that head: `build:rust:fetch` is the host prefetch of its `Cargo.lock` crates.io dependencies (a warning when refused or failed, since turns still run and the seat judges by reading; it prints the cache's size against its byte cap, and names a `REVIEW_LOOP_CRATE_CACHE_GIB` value it refused), and `build:rust` is an offline `cargo metadata --locked` inside the real sandbox layout with the cache mounted read-only ([dependency prefetch](issue-16-boundary.md#dependency-prefetch-issue-51-the-host-fetches-the-sandbox-builds-offline)) |
| 6 supervisor | the ledger migrates and `status` reads; the route would accept the runtime file; `doctor`'s state dir, cron shim/job and gateway checks; the observer route |
| 7 live turn | with `--live-turn --pr N`: a real isolated reviewer turn with the reviewer seat's resolved model, for the loop's reviewer `turn_budget_s` — the budget production enforces — unless `--timeout N` overrides it; the verdict and body the agent *would* submit are printed |

Example step-1/3 lines for a ChatGPT-subscription reviewer and an API-key fixer:

```text
✅ seat:reviewer    profile codex: openai-codex / gpt-5.3-codex via chatgpt.com [codex_responses, OAuth (host-refreshed)]
✅ seat:fixer       profile fix: custom:acme / fix-model via acme.example [chat_completions, API key]
✅ model:completion HTTP 200 — reviewer: profile codex: openai-codex / gpt-5.3-codex via chatgpt.com [codex_responses, OAuth (host-refreshed)], reply 'OK'
```

A subscription seat's step 3 spends a request from **your** plan's usage window (the seat shares
it with your own use of that account); a 429 there means that window is spent.

A live turn that times out here would be killed in production too: raise the seat's budget
(`set --reviewer-turn-budget N`, see [Turn budget](configuration.md#turn-budget-how-long-one-turn-may-run))
rather than only `--timeout`.

The live turn runs in the CLI process, not through the supervisor, so it adds no ledger row and
raises no operator notice; confirming alerts still needs a real enqueued turn and a watchdog sweep.

The no-write guarantees and what the selftest does not prove are in
[Issue #16: live verification](issue-16-boundary.md#live-verification-hermes-review-loop-selftest).

## Sandbox size caps: the two writable mounts

Every surface a seat can grow is a named tmpfs, because bubblewrap's own default is half of RAM and
a bind mount has no size at all — a seat that spent its turn writing could fill the host filesystem
that holds the loop's ledger.

| Mount | Holds | Default | Override |
| --- | --- | --- | --- |
| `/work` | the checkout the seat builds and edits in (`CARGO_TARGET_DIR` points here) | 8 GiB | `REVIEW_LOOP_CHECKOUT_SIZE_GIB` |
| `/tmp` | `TMPDIR`, `CARGO_HOME`/`RUSTUP_HOME`, an unwritable checkout's build target | 2 GiB | `REVIEW_LOOP_SCRATCH_SIZE_GIB` |

Both are sized against what real Rust workspaces build — a `patchhive/attest` debug target is
2.2 GiB, two others 3.3 and 3.9 GiB — because a cap below a real target does not fail loudly: the
seat reports that it could not verify and every review requests changes.

Are these caps or reservations? Caps: tmpfs is charged page by page, so a seat that writes nothing
costs nothing. But they are charged against **RAM and swap**, not disk, so at this loop's
concurrency they are also a memory budget. `doctor`'s `sandbox:caps` line prints the two caps, the
worst case at the loop's own concurrency, and the host's available memory beside them, and fails
when the worst case is larger than what is available.

### Where to set an override

In the environment of **the process that runs the supervisor** — normally the gateway, since every
sweep, webhook route and `arm` runs inside its process tree. The launcher reads these once, when
the supervisor imports it, so a change needs a restart of that unit (or, for a hand-run `arm`, an
export in that shell first):

```ini
[Service]
Environment=REVIEW_LOOP_CHECKOUT_SIZE_GIB=16
Environment=REVIEW_LOOP_SCRATCH_SIZE_GIB=4
```

Values are integers in GiB, 1 to 1024. A value that cannot be parsed is reported on stderr and the
default stays in force rather than taking an unattended loop down — but it is not silent: `doctor`
and `selftest` both name an override they refused. Confirm what took effect with
`hermes review-loop doctor --loop <id>`; `selftest --pr N` adds
`sandbox:build-fits`, which measures the isolation clone's own `target` against the cap.

## Why isn't this PR moving?

`status` shows the loop's shape; `explain` answers the question you actually have at 2am, for one
PR: what GitHub says about the head, how much of the budget is spent *at that head*, who holds the
seat, what is queued or marked in flight, whether the loop is paused, and — last line, always — the
one event that has to happen next.

```bash
hermes review-loop explain --loop widgets --pr 7    # --loop may be omitted when it is the only loop
```

```
[widgets] acme/widgets#7 — why this PR is not moving
  pr:         https://github.com/acme/widgets/pull/7
  read:       2026-09-24T01:58:53Z (GitHub pulls/reviews/hooks + local state; read once, nothing written)
  state:      open · base main · author dev-fixer · head aaaaaaa
  budget:     1/3 verdicts spent · 1 at head aaaaaaa — changes requested 2026-01-01T00:00:00Z by rev-coach
  seat:       nobody holds it
  queue:      not queued
  in-flight:  none
  deps:       reviewer #7 @ aaaaaaa succeeded — rust: ready — 224 crates.io crates from Cargo.lock (2.4s)
  escalation: none
  hooks:      armed — both seat routes are active repo hooks
  sweep:      no watchdog sweep recorded — nothing has read this loop's PRs yet
  github:     no failed GitHub call recorded
  gates:      no unresolved gate failure recorded for this PR
  blocked:    the changes-requested verdict at head aaaaaaa has no fix run out — the fixer gate did not start one for that delivery
  next:       re-deliver the changes-requested review event for head aaaaaaa to the fixer gate after checking why its run did not start — no fixer is running to push a fix
```

On a loop that has not opted in to unattended fixer pushes, the same PR is not broken — it is
waiting for you, and the last line says exactly what to run:

```
  queue:      fixer 1 of 1 (waiting 3m) — fixer held: unattended fixer pushes are off for this loop — …
  blocked:    fixer held: unattended fixer pushes are off for this loop: the changes-requested verdict at head aaaaaaa starts no fixer turn until the loop opts in — `hermes review-loop fixer-push --loop widgets --enable --acknowledge-pr-race`
  next:       operator decision: unattended fixer pushes are off for this loop, so the changes-requested verdict at head aaaaaaa starts no fixer turn. To let the fixer answer it, run `hermes review-loop fixer-push --loop widgets --enable --acknowledge-pr-race` — the next watchdog sweep (or `hermes review-loop drain --loop widgets --seat fixer`) then starts the fix run for this head; or fix it by hand, push, and re-request review
```

A PR that is waiting rather than broken says so, instead of looking like a failure:

```
[widgets] acme/widgets#9 — why this PR is not moving
  pr:         https://github.com/acme/widgets/pull/9
  read:       2026-09-24T01:58:53Z (GitHub pulls/reviews/hooks + local state; read once, nothing written)
  state:      open · base main · author dev-fixer · head bbbbbbb
  budget:     0/3 verdicts spent · nothing at head bbbbbbb
  seat:       nobody holds it
  queue:      reviewer 1 of 1 (waiting 9m) — reviewer at capacity 1/1: acme/widgets#7 (720s)
  in-flight:  none
  escalation: none
  hooks:      armed — both seat routes are active repo hooks
  sweep:      no watchdog sweep recorded — nothing has read this loop's PRs yet
  github:     no failed GitHub call recorded
  gates:      no unresolved gate failure recorded for this PR
  blocked:    no capacity: queued with the reviewer seat — reviewer at capacity 1/1: acme/widgets#7 (720s)
  next:       a reviewer slot frees — the queued run starts then (a verdict or a handoff ends the run holding it; the lock expiry at 45m is the backstop)
```

The `deps:` line is what the host's dependency prefetch did for that PR's newest turns, read from
the run ledger: `fetching — started …` while a turn is still fetching (it is bounded at 300 s and
runs before the turn budget starts, so it is not a hung turn), then `ready` or `unavailable` with
the reason — for example `the host crate cache would exceed 2 GiB (host limit
REVIEW_LOOP_CRATE_CACHE_GIB)`, or a lockfile with git dependencies, which the host never fetches
([dependency prefetch](issue-16-boundary.md#dependency-prefetch-issue-51-the-host-fetches-the-sandbox-builds-offline)).
`status` prints the same line for the loop's newest turns. The cap is a host setting: set
`REVIEW_LOOP_CRATE_CACHE_GIB` (whole GiB, 1-1024, default 2) in the environment of the process
that runs the supervisor (normally the gateway), and it applies from the next prefetch.

Three rules keep it honest:

* **No second engine.** The conclusions come from the same predicates the live gates run
  (`verdicts`, `reviewed_at_head`, `changes_at_head`, `approved_at_head`, the seat ledgers, the
  queue, the breach marker, the armed check), in the gates' own guard order. A gate stops at the
  first guard that silences it; `explain` reports every guard and names the one that is holding the
  PR. What it says cannot drift from what the loop would do, because it is the same code.
* **Unknown is not a guess.** A transient GitHub failure, an unreadable review list or hook list,
  or a malformed PR head is printed as unknown and needs a retry, not a guessed verdict or review
  request. HTTP 404 means missing *or inaccessible* and calls for checking the number and access,
  not treating it as a transient failure. Every timestamp is labelled with where it came from — the
  read itself, the verdict's `submitted_at`, or the state file's own mark.
* **Read-only, byte for byte.** No claim, no queue entry, no inflight mark, no drain, no webhook
  POST, no token printed, and it does not even prune an expired lock while looking at it. Run it
  twice and GitHub, the loop's state directory and your routes file are untouched. The suite asserts
  exactly that.

It needs to read the repo's hooks to tell "paused" from "armed", so the read token wants enough
scope to see them (`repo` is normally enough); if it cannot, the line says the hook state is unknown
rather than claiming the loop is parked. `explain` exits 2 only when the question cannot be asked at
all — an unknown loop, a loop file the loader refuses (without `--loop` each such file is named on a
`skipping <file>: <reason>` line), several loops (refused ones included) and no `--loop`, or no loop
files at all (`no loops configured in <dir>`). Before `cmd_explain` itself runs, argparse can also
exit 2 on its own account, for the same subcommand: a missing `--pr` (`the following arguments are
required: --pr`), an `--pr` value that is not an integer (`invalid int value`), an `--pr` flag
with no value after it (`expected one argument`), an `--loop` flag with no value after it
(`argument --loop: expected one argument`), or an unrecognized flag (`unrecognized arguments`).

The guard order `explain` walks is in
[architecture: Explain](architecture.md#explain--why-is-this-pr-not-moving).

### The loop stops with `RealHomeError` or `RealNetworkError`

Those are the **test suite's** tripwires, not a loop failure. Under the test harness
(`tests/_home_guard.py`), the plugin refuses to touch anything inside the real home (`~/.hermes`
included), run the real `hermes`, or reach a real host. It raises a `BaseException`, so no handler
swallows it. The message starts with `test guard active (REVIEW_LOOP_TEST_HOME_GUARD=1)`.

There is one exception to "no real host": the dependency prefetch (`deps._fetch`) may make an
anonymous, credential-free `cargo fetch` from crates.io's own hosts, `index.crates.io` (the sparse
index) and `static.crates.io` (downloads), for the dependency tests. Before cargo starts,
`deps.guard_registry` refuses the fetch if anything could send it elsewhere: a proxy variable,
another registry or a source replacement, a cargo config file, a registry key or
`[source]`/`[registries]`/`[patch]`/`[replace]` table in the manifest, or a non-crates.io source in
the lockfile. A refusal names which of these it found. Every other host is refused.

The tripwires arm only when **both** of these are set, and only the test harness sets them:

| Variable | Set by | Meaning |
|---|---|---|
| `REVIEW_LOOP_TEST_HOME_GUARD=1` | `tests/_home_guard.py` | "this process runs under the test guard" |
| `REVIEW_LOOP_TEST_GUARD_SENTINEL` | `tests/_home_guard.py` | path to an empty sentinel file the guard creates in its temp dir |

`REVIEW_LOOP_TEST_HOME_GUARD` on its own does nothing, so a real loop that inherits it keeps
working. A real loop stops only if its gateway inherited **both** variables while the sentinel file
still existed, for example because it was started from a shell that was running the test suite.
To clear it:

```bash
systemctl --user show-environment | grep REVIEW_LOOP_TEST_     # or check the shell / unit that starts the gateway
unset REVIEW_LOOP_TEST_HOME_GUARD REVIEW_LOOP_TEST_GUARD_SENTINEL REVIEW_LOOP_TEST_REAL_HOME
```

Then restart the gateway from the cleaned environment so it re-reads it (`hermes gateway status`
shows whether it is running; `hermes gateway start` starts it).

Remove them wherever the gateway gets its environment: the systemd unit's `Environment=`, the
shell profile, or the launching terminal. `REVIEW_LOOP_TEST_REAL_HOME`, `REVIEW_LOOP_TEST_USER_HOME`,
`REVIEW_LOOP_TEST_SHIM_DIR` and `REVIEW_LOOP_TEST_FAKE_HERMES` are test-only too. None of them is
ever needed by a real loop.

## Why did that delivery start nothing? `trace`

A gate that declines an event answers the gateway `[SILENT]` and exits 0, and Hermes logs nothing
for that (#209). GitHub's webhook page then shows `200 {"status": "ignored", "reason": "script"}`
for every delivery, the one that queued a run and the one that was refused alike. `trace` answers
for one delivery:

```bash
hermes review-loop trace --loop name --delivery 40ac7f60-be4a-11f1-8969-4f6489738e63 --admin-token LOGIN
hermes review-loop trace --loop name --payload saved.json --event pull_request
```

`--delivery` takes the numeric id or the `X-GitHub-Delivery` GUID from the hook's *Recent
Deliveries* page. Reading deliveries needs a token with hook read access (`--admin-token`, the hook
admin). `--payload` traces a saved payload instead.

It runs the **real** gate script on the payload, in a child whose Hermes home is a temporary copy of
this loop's: its config, state and a snapshot of the run ledger. The gate's own decisions are
therefore exactly the live gate's. Anything that would leave the machine is listed instead of done:
GitHub writes, gateway route POSTs, observer notices, and the drain or isolated worker it would
start. GitHub reads are real, so the gate judges the PR as it is now. The output shows the
delivery's facts, everything the gate logged, each `would …`, and one `outcome:` line:
`would start a reviewer run`, `held — <why>`, or `declined — <why>`. The loop's real state is
untouched, and the copy is deleted afterwards.

## When a gate crashes or runs out of time

The Hermes gateway runs a gate synchronously inside the webhook request: payload on stdin, no
headers (so no delivery id), and a platform-wide `script_timeout_seconds` (default 30s) after which
the gate is killed. A crash, a timeout, empty output and `[SILENT]` all get the same HTTP 200
`ignored` reply. No script outcome yields a non-2xx, and GitHub does not redeliver failed
deliveries by itself, so a non-2xx "retry me" is neither available nor useful. The loop keeps its
own record instead:

* **Budget.** Each gate has 20s (`REVIEW_LOOP_GATE_BUDGET_S`) and shrinks it to fit the
  `script_timeout_seconds` of the gateway running it. The gateway reads `gateway.json` and
  `config.yaml` from its own home, so the gate reads those same files, with the gateway's
  precedence (later wins):
  1. `gateway.json` `platforms.webhook`. If the file is malformed it is skipped, as the gateway
     skips it.
  2. `config.yaml`, with the administrator's managed overlay (`$HERMES_MANAGED_DIR` or
     `/etc/hermes`) merged over it: `gateway.platforms.webhook`, then `platforms.webhook`, then
     `gateway.webhook`. In each of these an `extra:` value beats a plain key. A `config.yaml`
     that is malformed, or that cannot be read or decoded as UTF-8 (a UTF-16 or Latin-1 file,
     say), drops this whole layer, as it does for the gateway, and `gateway.json` decides.
  3. A top-level `webhook:` block, which the gateway bridges into `extra` last: it beats every
     block above, and its own `extra:` beats its plain key.
  - A `/p/<profile>/` route on the multiplexing host gateway (the default setup) runs under the
    root home's limit. The script's `HERMES_HOME` is still the profile's home.
  - A profile with `gateway.standalone: true` runs its own gateway and uses its own limit.

  From inside the script these two cases look the same. So for a profile that isn't marked
  standalone, the gate uses the lower of the host's and the profile's limits. Every GitHub call
  is clipped to the time left, a timer up to 3s later interrupts anything else that hangs, and a
  gate-triggered queue drain gets at most half the remaining time. The fit always keeps 1s for
  start-up and 3s for recording a failure. Recording never waits on a busy ledger past its
  share of those 3s: it falls through to the no-loop ledger, which every watchdog run sweeps.
  `doctor` prints one `gate:timeout:<profile>` line for each profile hosting a loop route
  (reviewer, fixer, adjudicator, observer). Each line names the gateway and file, flags any
  limit below 27s (the lowest that fits the full 20s budget, the 3s backstop, start-up and
  recording: `doctor` and the gate use the same arithmetic), flags a limit below 5s as too small for a gate even to record its own
  failure, and says which file to fix.
* **Seats.** The reviewer and fixer gates do not claim seats: they enqueue an isolated turn in
  the host run ledger (`gate.block_pr_agent` → `enqueue_isolated`), whose worker enforces each
  seat's capacity. What a gate does with `locks.json` is release a legacy claim it finds for the
  PR (`st.release_if`). `gate.take_seat` still claims a seat through `st.acquire`, but no gate
  script calls it. Every `st.acquire` is also remembered by the process
  that made it, so if any gate process ever claims a seat and then crashes or times out, it
  releases that claim on its way out, and a failed delivery never keeps a seat until `ttl_min`.
* **Ledger.** A crash (exit 2), a timeout (exit 3), a stop (SIGTERM from a gateway or systemd
  stop, or a kill; exit 143), or a `[SILENT]` that followed a failed GitHub read is written to `gate-failures.json` in the loop's state directory. The entry holds the gate,
  repo, PR, head, action, exception type and message, and a bounded traceback, and the payload is
  stored beside it. Failures that happen before a loop can be named go to
  `~/.hermes/state/review-loop-gate-failures/`. The same event delivered again (same payload)
  bumps its attempt count instead of adding an entry. A payload over 1 MiB is not kept, and
  that entry cannot be re-driven: the alert and `explain` say so and name the route whose hook
  to open in GitHub (Settings → Webhooks → Recent Deliveries → Redeliver).
* **An unreadable ledger is kept, not overwritten.** If `gate-failures.json` exists but is not a
  ledger, the next gate failure or watchdog sweep moves it aside once, to
  `gate-failures.json.corrupt-<UTC time>` in the same directory, and starts a fresh ledger. "Not a
  ledger" covers a torn write or a bad hand edit, and also a file that parses but has the wrong
  shape: not an object of entry objects, or holding a key starting with `_`. The move is
  crash-safe. The corrupt bytes get their second name first, and only then does the fresh ledger
  replace the original path atomically. A crash in between leaves the original in place, and the
  next writer reuses the copy it already made. The fresh ledger holds one entry that names the
  copy. That entry is never pruned by the 200-entry bound while the copy exists. The watchdog
  alerts on it every cooldown until the copy is gone, and `explain` lists it for every PR of the loop, because that PR's earlier
  failures may only be in the copy. Salvage what you need from the copy and delete it; the next
  sweep then clears the entry. A file that cannot even be read as bytes (mode 000) is still
  moved aside, by hard link. Something that is not a regular file (a directory in its place), or
  a state directory that is not writable, cannot be moved aside. For those, nothing is written,
  the sweep reports why, `explain` says it cannot be moved aside automatically, and the failed
  reads its entries owned are reported by the health check instead. An entry whose stored payload has since been deleted says so, and gives the same GitHub
  redelivery steps as a payload that was never kept.
* **Watchdog.** Each sweep alerts on unresolved entries, once per new failure and again after the
  cooldown. It re-drives reviewer and fixer events by running the gate again on the stored
  payload. This is safe because those gates re-read the live PR and the run ledger dedups a second
  enqueue. It stops after 3 re-drives. Sweeps overlap (the one shared cron job sweeps all
  loops), so each entry is claimed under the ledger's lock before anything is sent or run. The
  claim adds the re-drive to the count and gives this sweep a 120-second lease on the entry.
  Another sweep finds the live claim and skips the entry. The owner prints the alert, and only
  then marks the entry alerted and drops the claim. If a sweep dies before its alert is printed,
  the lease runs out and a later sweep says it: an alert can be repeated, never lost. However
  many sweeps run, a failure is alerted once per new attempt (and per cooldown) and re-driven at
  most 3 times in total. A loop whose hooks are paused still gets its gate failures alerted, but
  nothing is re-driven until it is armed again. The no-loop ledger
  (`~/.hermes/state/review-loop-gate-failures/`) is swept by every watchdog run, including one
  scoped with `--loop`.
  Adjudicator failures are alerted but never re-driven, because that gate's output is its
  dispatch. An entry resolves when the same event later
  completes cleanly, whether through a re-drive or a manual redelivery from GitHub.
* **`explain`** lists unresolved gate failures for the PR as blockers. It also lists the
  loop's failures whose payload named no PR, and the corrupt-copy entry, for every PR. It says
  "the next sweep retries it" only when the sweep could: re-drivable, payload kept and present,
  under the cap, and the gate script present.
* **One owner per failed read.** When a gate's GitHub read fails, the event's
  `gate-failures.json` entry owns it: that is what raises the alert and triggers the re-drive.
  `github-reads.json` still records it as the last failed call, which `explain` shows on its
  `github:` line, but marks it `owned_by`. The watchdog's GitHub-health check therefore doesn't
  announce it again. The watchdog's own reads (the `/user` probe and the hook list) and any
  failed read no gate-failure entry claims are still reported by the health check.
* **The watchdog is budgeted too.** Its GitHub reads are capped at 20s each and the run at
  600s (`REVIEW_LOOP_WATCHDOG_BUDGET_S`). A sweep that runs out of budget stops, and the next cron
  run starts fresh. Running out of time does not prove GitHub gave no answer, since slow answered
  reads spend the budget too. So it counts as a failed-read sweep for the health check and is
  said like any failure short of a 401/403: one "watchdog stopped: the sweep ran out of its …
  budget" line after 3 such sweeps in a row, then once per cooldown.
* **One event, one ledger.** If a gate's write to the loop's ledger raises *after* it landed (a
  failed directory fsync, or the record-phase alarm), the gate keeps it there instead of also
  writing it to the no-loop ledger. An event that is in both anyway is resolved in the no-loop
  ledger as a duplicate, so it is alerted once and re-driven at most 3 times in total. A failure
  the no-loop ledger had to take (the loop's ledger was busy) is still shown by `explain --pr N`
  for that loop's PR, and its `github-reads.json` record names the ledger that holds it. When
  the owning entry resolves or is pruned, or the same event later completes cleanly, that record
  is marked `resolved_by`, so `explain` stops pointing at a gate-failure line and the health check
  does not announce the old read. A duplicate resolved in the no-loop ledger leaves the mark
  alone, because the original still owns the read. Both `explain` and the health check also
  check that some ledger still holds the owning entry: the ledger named by `owned_in`, the
  loop's, and the no-loop one, since one event can be in two. An open entry in any of them wins.
  A copy resolved only as a duplicate never counts as the owner resolving, because it never
  owned the read. If none holds it open (its ledger was moved aside, pruned, or cannot be read at
  all), `explain` says so instead of promising a line, and the health check reports the read
  itself.

## When an isolated run fails

Every isolated turn is a row in the host run ledger (`~/.hermes/state/review-loop-runs.sqlite`).
A failure is sorted by one question — *could it have written to GitHub?* — answered from the
host's own write-ahead records, never from an exit code. The sandbox holds no GitHub credential;
its only writes go through the run's broker, which commits a record keyed by the run ID *before*
the external call: a review-receipt claim (reviewer), a push intent/confirmation (fixer; its
review request needs a confirmed push first) or a ruling (adjudicator; its optional PR comment
follows the ruling). A run with any of those, or one ever quarantined as `uncertain`, may have
written. Anything else did not.

```
pending ──claim──► claimed ──► launching/running ──► succeeded
   ▲  │ PR draft, or no effective verdict yet: stays pending (a wait, not a failure)
   │  │ GitHub read failed at claim (a 502, a timeout): waiting, counted like a failed turn
   │  │ PR closed / head moved: cancelled               (a reopen or redelivery re-arms it)
   │  │ fixer push not admitted / revoked: cancelled    (listed; an operator `retry` after
   │  │                                                   opting in re-admits it — see below)
   │                         │ failed, nothing on the write-ahead record
   │                         ├─ transient (non-zero sandbox exit — model 429/5xx, OAuth refresh —,
   │                         │  timeout, network, staging read): waiting, backoff 2m, 4m, 8m
   ├──── backoff elapsed ────┘     … the 4th failure: failed (with a notice)
   │                         │ killed at its turn budget: failed at once (raise turn_budget_s)
   ├──── redelivered event (≤8 failures) or `retry` ◄── failed / waiting / push-policy cancelled
   │                         │ may have written, or a worker lost/still alive: uncertain
   └─ never ◄────────────────┘   (operator `reconcile` only; never replayed)
```

Backoff is `2m·2ⁿ⁻¹` after the n-th failure, so a run waits 120 s, 240 s and 480 s (the
longest wait the chain can reach) before its fourth failure makes it `failed`. The same count
covers a GitHub read that failed at claim time: a claim that cannot read the PR waits and retries
like a failed turn, and is never left pending and invisible.

Due retries start on the next event for the PR, when another run finishes, or on the next armed
watchdog sweep. A failed run's notice carries the real reason (the exception text, or the sandbox
exit status) and the tail of the turn's stdout/stderr; `status` and `explain` print the same with
the next step, and `explain` reports a waiting, write-free failed, cancelled-by-push-policy or
uncertain run at the PR's head as a `blocked:` line with that step as `next:`.
`hermes review-loop retry --loop name --pr 123 [--seat reviewer]` re-arms the PR's failed and
waiting runs, and its fixer runs cancelled by the push policy, at its newest head (the head of its
most recently active run), resets their retry budget and starts the worker. It refuses a run that
may have written and prints the `reconcile` command instead. Any other cancellation (head moved,
PR closed) is not offered: a new head gets its own turn.

A fixer run the push policy cancelled at claim (pushes were off when its verdict was enqueued, or
were turned off before it started) is listed by `status` and `explain` with that reason and what
to do. Admission has two rules. A redelivered event **never** upgrades it: a row admitted while
pushes were off stays unadmitted. An operator `retry` re-admits it under the policy in force
**now**, taking a fresh admission snapshot under the same push-policy lock the gate and the
broker use. While pushes are still off, that retry is refused with the command that turns them on.

## How it handles a burst

Fifty PRs arrive in an hour. Ten of them wake the reviewer, and forty-one queue — the queue costs
nothing but disk-less JSON. The moment a review ends, that slot is filled from the queue:

```
review #101 finishes (approve or changes-requested)
  → its slot is freed
  → the queue is drained immediately, up to the free slots
  → the next queued PR's review starts
```

A slot is not freed by a timer. The **verdict** frees it, either kind; the fixer's **request** frees
the fixer's. The watchdog sweep is only the backstop, and `ttl_min` is the last resort for a run that
died without a verdict. Capacity is per seat, so `reviewer 10 · fixer 2` is a legitimate shape —
reviews are cheap and parallel, fixes are not.

Do the arithmetic before setting it high: each in-flight run is a whole agent plus its own clone and
its own cold build. On a big Rust repo, ten at once is ten parallel builds — the machine, not GitHub,
is what decides how high this number can go.