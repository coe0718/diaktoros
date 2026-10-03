# Command reference

Every `hermes review-loop` command, what it is for, what it changes, and every flag it takes.
New to the loop? Read [How it works](concepts.md) first; the words used here (seat, route, hook,
reader, arm) are defined there.

The flag tables are generated from the plugin's own command-line parser, so they always match
the installed version. `hermes review-loop <command> --help` prints the same flags in the
terminal.

**Conventions used below.**

- `name` is a loop id: by default the repository's name, so `owner/name` gets the loop `name`.
  `hermes review-loop list` shows yours.
- A **login** is a GitHub username, such as `rev-bot`. A **profile** is a Hermes profile name,
  such as `vex`.
- "**Read-only**" means the command changes nothing: no files, no routes, no GitHub writes. You
  can run it as often as you like.
- **Exit codes:** `0` means it worked, `1` means it ran but found something you need to fix, and
  `2` means it could not run at all (a refused config, an unknown loop, a missing flag). Commands
  that differ say so.

| I want to… | command |
| --- | --- |
| see my loops | [`list`](#list), [`status`](#status) |
| install a loop | [`setup`](#setup) (guided), or [`init`](#init) (flag by flag) |
| check an install before going live | [`doctor`](#doctor), [`selftest`](#selftest) |
| turn the loop on or off | [`arm`](#arm) |
| change a setting | [`set`](#set), [`apply`](#apply), [`settings`](#settings) |
| let the fixer push on its own | [`fixer-push`](#fixer-push) |
| label new issues automatically | [`triage`](#triage) |
| find out why a PR is not moving | [`explain`](#explain) |
| find out why a webhook started nothing | [`trace`](#trace) |
| run a failed turn again | [`retry`](#retry) |
| start a queued turn now | [`drain`](#drain) |
| see which models a seat can use | [`models`](#models) |
| free disk space | [`cleanup`](#cleanup) |
| remove a loop | [`uninstall`](#uninstall) |

---

## Seeing what you have

### list

Lists every configured loop on one line each: its id, repository, verdict cap, how many PRs
each seat may work at once, and the allowlisted fixer and reviewer logins. Read-only.

```bash
hermes review-loop list
```

A loop file that does not load is named on a `skipping <file>: <reason>` line instead, and the
command exits 2 so a script notices.

<!-- flags:list -->
No flags.
<!-- /flags -->

### status

Shows a loop's configuration and its live state: who serves each seat, the routes as they are
actually installed, the token files (paths, never contents), the observer feed, pacing (daily
caps, and any seat waiting for its usage window), queued and running turns, and when the
watchdog last ran. Read-only.

```bash
hermes review-loop status --loop name
```

Without `--loop` it shows every loop. Use it to answer "what is this loop set to?" Use
[`explain`](#explain) to answer "why is this PR stuck?"

<!-- flags:status -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` |  | loop id (default: every configured loop) |
<!-- /flags -->

### settings

Shows the plugin's settings form: the defaults a new loop starts from, and what `apply` would
push onto a loop. Each value is marked `[set]` (you changed it) or `[default]`. It also shows
the seat mapping the form holds and what each existing loop actually runs as. Read-only.

```bash
hermes review-loop settings
```

The form lives in the Hermes desktop app under **Capabilities → Plugins → review loop**. See
[settings.md](settings.md).

<!-- flags:settings -->
No flags.
<!-- /flags -->

---

## Installing and changing a loop

### setup

A first install in one command, and the easiest way to start. It walks through five steps, each
using the same code as the command named in it:

1. **Runtime paths.** It finds the Hermes checkout, its virtualenv, the Python installation and a
   Rust toolchain, and writes the private runtime file `~/.hermes/review-loop-runtime.json`
   (mode 600). A path already in the file that still works is kept; a broken one is replaced. The
   file is written only when every path passes the same checks `selftest` makes.
2. **The loop.** It asks the [`init`](#init) questions (repository, accounts, profiles, token
   files, reader, gateway address, observer, signing, hook admin), with the settings form's values
   as defaults. It shows `init`'s dry run, asks you to confirm, then runs `init`. A loop that
   already exists is kept as it is.
3. **The watchdog.** It schedules the shared watchdog job, only if it is missing.
4. **Checks.** It runs [`doctor`](#doctor) and [`selftest --no-model`](#selftest). Any ❌ stops it
   here, with the fix line above.
5. **Arm.** Only after a clean pass, and only if you say yes (the question defaults to no).

```bash
hermes review-loop setup --repo owner/name
hermes review-loop setup --repo owner/name --dry-run
```

Running it again is safe: whatever is already in place is kept, so a second run only repairs what
is missing or broken. `--dry-run` shows every step and writes nothing. `--yes` asks no questions:
your flags and the settings form are the answers and every confirmation is yes, except arming,
which still needs `--arm`. Without a terminal (in a script), it refuses unless you pass `--yes`.

`setup` never turns on adjudication (it passes no `--adjudicator-route` to `init`) or issue
triage. To add an adjudicator afterwards, add an `adjudicator` block to the loop file
`~/.hermes/review-loops.d/name.json`, for example
`"adjudicator": {"route": "name-breach", "profile": "tuck"}`, then write its route with
`hermes review-loop apply --loop name --recreate-routes`. For triage, see [`triage`](#triage).

<!-- flags:setup -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--repo` | `REPO` |  | owner/name (asked when not given) |
| `--id` | `ID` |  | loop id (default: the repository name) |
| `--yes` |  |  | no questions: the flags and the plugin settings are the answers, and every confirmation is yes (arming still needs --arm) |
| `--dry-run` |  |  | show every step, write nothing |
| `--arm` |  |  | arm the hooks after a clean doctor and selftest |
| `--reviewer` | `REVIEWER` |  | reviewer GitHub login |
| `--fixer` | `FIXER` |  | fixer GitHub login |
| `--reviewer-profile` | `REVIEWER_PROFILE` |  | reviewer's Hermes profile |
| `--fixer-profile` | `FIXER_PROFILE` |  | fixer's Hermes profile |
| `--reviewer-token` | `REVIEWER_TOKEN` |  | reviewer's token file |
| `--fixer-token` | `FIXER_TOKEN` |  | fixer's token file |
| `--read-token` | `READ_TOKEN` |  | reader login (its own account) |
| `--read-token-file` | `READ_TOKEN_FILE` |  | reader's token file |
| `--host` | `HOST` |  | your gateway's webhook origin |
| `--admin-token-file` | `ADMIN_TOKEN_FILE` |  | hook admin's token file |
| `--schedule` | `SCHEDULE` |  | watchdog interval (default 15m) |
| `--watchdog-deliver` | `WATCHDOG_DELIVER` |  | where watchdog alerts go (default local) |
| `--admin-token` | `ADMIN_TOKEN` |  | hook admin login: the hooks are created (paused) as it |
| `--observer-profile` | `OBSERVER_PROFILE` |  | Hermes profile whose chat gets the loop's notices |
| `--attribution` | `on` \| `off` |  | sign what the loop posts with 'Automated by hermes-review-loop' (default: the plugin setting, on) |
| `--source` | `SOURCE` |  | runtime file's source path (default: detected) |
| `--venv` | `VENV` |  | runtime file's venv path (default: detected) |
| `--runtime` | `RUNTIME` |  | runtime file's runtime path (default: detected) |
| `--rust` | `RUST` |  | runtime file's rust path (default: detected) |
<!-- /flags -->

### init

Installs a new loop for one repository. In one transaction it writes:

1. the loop config: `~/.hermes/review-loops.d/<id>.json`;
2. the webhook routes in the gateway's registry: `<id>-review`, `<id>-fix`, plus the adjudicator
   route with `--adjudicator-route NAME` (any name; `<id>-breach` by convention) and `<id>-observe`
   with `--observer-profile`. Each gets a fresh secret. The adjudicator route never wakes an agent
   itself: naming it is what turns adjudication on;
3. the gate shims in each seat profile's `scripts/` directory, the small files the gateway runs
   when a route is called;
4. with `--hooks`, the two GitHub repo hooks, created **paused** (nothing happens until
   [`arm`](#arm));
5. with `--schedule`, the shared watchdog cron job, created only if it does not exist yet.

Everything is checked before the first file is written: the profiles exist, the logins are in
their allowlists, the four accounts (reader, reviewer, fixer, optional adjudicator) are distinct
and each has its own token file, and no route name belongs to someone else. A refused `init`
writes nothing. If a later step fails, the earlier ones are rolled back.

```bash
hermes review-loop init --repo owner/name --fixer dev-account --reviewer rev-bot --fixer-profile drey --reviewer-profile vex --read-token reader-bot --token reader-bot=~/.hermes/keys/reader-bot-pat --token rev-bot=~/.hermes/keys/rev-bot-pat --token dev-account=~/.hermes/keys/dev-account-pat --host https://your-gateway.example --hooks --admin-token reader-bot --schedule 15m --dry-run
```

**Always run it with `--dry-run` first.** That prints the seat mapping, the route URLs and the
credentials it would use, and writes nothing. Drop `--dry-run` to install.

Notes:

- `init` refuses a loop id that already exists. To change a loop, use [`set`](#set) or
  [`apply`](#apply).
- Flags you leave out come from the settings form (`--fixer`, `--reviewer`, the profiles, the
  token files, `--host`, the numbers). An explicit flag always wins.
- Exit `1` means it installed but something was not finished, for example the watchdog job could
  not be created or a hook's ping failed. The output names the command that finishes it.
- Accounts and tokens, step by step: [accounts.md](accounts.md).

<!-- flags:init -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--repo` | `REPO` | **required** | owner/name |
| `--id` | `ID` |  | loop id (default: the repository name) |
| `--fixer` | `FIXER` (repeatable) |  | GitHub login that pushes (repeatable; default: the plugin setting) |
| `--reviewer` | `REVIEWER` (repeatable) |  | GitHub login that may review (repeatable; default: the plugin setting) |
| `--reviewer-seat` | `REVIEWER_SEAT` |  | the login the reviewer route serves |
| `--reviewer-profile` | `REVIEWER_PROFILE` |  | Hermes profile for the reviewer seat (default: the plugin setting) |
| `--fixer-profile` | `FIXER_PROFILE` |  | Hermes profile for the fixer seat (default: the plugin setting) |
| `--reviewer-agent` | `REVIEWER_AGENT` |  | display name for the reviewer (default: profile) |
| `--fixer-agent` | `FIXER_AGENT` |  | display name for the fixer |
| `--cap` | `CAP` | `3` | verdicts allowed before adjudication |
| `--concurrency` | `CONCURRENCY` |  | default PRs per seat at once: 1 = serialized (default: 1, from the plugin settings). Above 1 needs --clone, because each run then gets its own clone. Override per seat with --reviewer-concurrency / --fixer-concurrency. |
| `--reviewer-concurrency` | `REVIEWER_CONCURRENCY` |  | PRs the reviewer may work at once (overrides --concurrency; default: the loop's, settings 1) |
| `--fixer-concurrency` | `FIXER_CONCURRENCY` |  | PRs the fixer may work at once (overrides --concurrency; default: the loop's, settings 1) |
| `--base` | `BASE` | `main` | the branch PRs target; only PRs against it are reviewed |
| `--clone` | `CLONE` |  | local clone the runs may use |
| `--root` | `ROOT` (repeatable) |  | a directory reviews may clean (repeatable) |
| `--state-dir` | `STATE_DIR` |  | where this loop keeps its state files (default: ~/.hermes/state/review-loops/<id>) |
| `--token` | `TOKEN` (repeatable) |  | login=/path/to/pat (repeatable) |
| `--read-token` | `READ_TOKEN` |  | required: login whose token reads GitHub — its own account, never a seat or the adjudicator login (the four-identity rule) |
| `--skill` | `SKILL` |  | skill the seats are told to load. A plugin-provided skill is qualified, e.g. hermes-review-loop:review-loop |
| `--adjudicator-route` | `ADJUDICATOR_ROUTE` |  | route name for the adjudicator (e.g. <id>-breach): setting it turns adjudication on when the verdict cap is spent |
| `--adjudicator-login` | `ADJUDICATOR_LOGIN` |  | optional fourth GitHub account the ruling is also posted as (needs --adjudicator-route and its own --token LOGIN=/path) |
| `--adjudicator-profile` | `ADJUDICATOR_PROFILE` |  | Hermes profile for the adjudicator (default: the plugin setting, else the launch profile) |
| `--observer-route` | `OBSERVER_ROUTE` |  | route name for the read-only observer feed (default: <id>-observe) |
| `--observer-profile` | `OBSERVER_PROFILE` |  | Hermes profile the observer feed belongs to (its chat) — naming one switches the feed on |
| `--observer-deliver` | `OBSERVER_DELIVER` | `telegram` | where the gateway delivers the feed (telegram, discord, ...); the feed never wakes an agent |
| `--observer-events` | `OBSERVER_EVENTS` |  | comma-separated transitions to send, from opened,handoff,verdict,approved,escalation,ruling,stall,closed (default: all) |
| `--observer-digest-min` | `OBSERVER_DIGEST_MIN` |  | batch the feed into one message per this many minutes (0 = one notice per transition) |
| `--host` | `HOST` |  | your gateway webhook origin (required unless set in plugin settings) |
| `--grace-min` | `GRACE_MIN` | `35` | minutes a PR may sit quiet before the watchdog reports a stall |
| `--ttl-min` | `TTL_MIN` | `45` | how long a run may hold its seat slot |
| `--inflight-ttl-min` | `INFLIGHT_TTL_MIN` | `10` | how long an in-flight mark blocks a second run at the same head |
| `--attribution` | `on` \| `off` |  | sign what the loop posts with 'Automated by hermes-review-loop' (default on) |
| `--turn-budget` | `TURN_BUDGET` | `900` | seconds one isolated seat turn may run, build and tests included (default 900; the sandbox is killed past it) |
| `--reviewer-turn-budget` | `REVIEWER_TURN_BUDGET` |  | the reviewer seat's own turn budget in seconds (overrides --turn-budget) |
| `--fixer-turn-budget` | `FIXER_TURN_BUDGET` |  | the fixer seat's own turn budget in seconds (overrides --turn-budget) |
| `--hooks` |  |  | create the GitHub hooks too, paused until `arm` |
| `--arm` |  |  | with --hooks: create them armed (live at once) instead of paused |
| `--admin-token` | `ADMIN_TOKEN` |  | login whose token can create hooks |
| `--schedule` | `SCHEDULE` |  | e.g. 15m — install the watchdog cron job |
| `--watchdog-deliver` | `WATCHDOG_DELIVER` | `local` | cron delivery target for watchdog alerts |
| `--dry-run` |  |  | print the seat mapping and what would be written, write nothing |
<!-- /flags -->

### set

Changes one loop's settings in place, through the same checks `init` uses. Only the flags you
pass change; everything else stays as it is.

```bash
hermes review-loop set --loop name --cap 4
hermes review-loop set --loop name --reviewer-turn-budget 1800
hermes review-loop set --loop name --reviewer-daily-turns 40
hermes review-loop set --loop name --read-token reader-bot --token reader-bot=~/.hermes/keys/reader-bot-pat
hermes review-loop set --loop name --observer-events opened,verdict,approved,escalation,ruling
```

What it is for:

- numbers: the verdict cap, concurrency, turn budgets, daily caps, the watchdog's patience
  (`--grace-min`, and `--marker-grace-min`: how long an adjudication may wait before the watchdog
  reports it);
- signing: `--attribution off` stops adding the "Automated by hermes-review-loop" footer and commit
  trailer to what this loop posts, and `on` restores it (see
  [what the loop signs](operations.md#what-the-loop-signs));
- the reader account, or the adjudicator's comment account;
- the gateway host (`--host`), which rewrites the routes' URLs;
- the observer feed: its route, profile, destination and events, plus `--observer-mute` and
  `--observer-unmute` to pause and resume it.

`set` never changes who serves a seat (profile or login). That comes from the settings form
through [`apply`](#apply), so one place decides seat identity. Concurrency above 1 needs a
`--clone`, because each parallel run gets its own copy. Without one it is refused, exactly as at
`init`.

<!-- flags:set -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` | **required** | loop id (its config file name; `list` shows them) |
| `--concurrency` | `CONCURRENCY` |  | default PRs per seat at once (1 = serialized; above 1 needs a clone, since each run gets its own) |
| `--reviewer-concurrency` | `REVIEWER_CONCURRENCY` |  | how many PRs the reviewer may work at once |
| `--fixer-concurrency` | `FIXER_CONCURRENCY` |  | how many PRs the fixer may work at once |
| `--cap` | `CAP` |  | verdicts allowed before adjudication |
| `--clone` | `CLONE` |  | local clone the runs isolate from |
| `--base` | `BASE` |  | base branch the loop watches |
| `--grace-min` | `GRACE_MIN` |  | quiet minutes before the watchdog speaks |
| `--marker-grace-min` | `MARKER_GRACE_MIN` |  | minutes an adjudication may sit claimed with no live run before the watchdog reports it |
| `--ttl-min` | `TTL_MIN` |  | how long a run may hold its slot |
| `--inflight-ttl-min` | `INFLIGHT_TTL_MIN` |  | minutes an in-flight mark blocks a second run at the same head |
| `--turn-budget` | `TURN_BUDGET` |  | seconds one isolated seat turn may run (loop default) |
| `--attribution` | `on` \| `off` |  | sign what the loop posts ('Automated by hermes-review-loop'), or stop |
| `--reviewer-turn-budget` | `REVIEWER_TURN_BUDGET` |  | the reviewer seat's own turn budget in seconds |
| `--fixer-turn-budget` | `FIXER_TURN_BUDGET` |  | the fixer seat's own turn budget in seconds |
| `--reviewer-daily-turns` | `REVIEWER_DAILY_TURNS` |  | most reviewer turns per day on this loop; later ones wait for midnight (0 removes the cap) |
| `--fixer-daily-turns` | `FIXER_DAILY_TURNS` |  | most fixer turns per day on this loop (0 removes the cap) |
| `--host` | `HOST` |  | gateway webhook host |
| `--adjudicator-login` | `ADJUDICATOR_LOGIN` |  | optional fourth GitHub account the ruling is also posted as; "" clears it (rulings go to the operator only) |
| `--read-token` | `READ_TOKEN` |  | the login the gates read GitHub as — its own account, never a seat or the adjudicator login (the four-identity rule); map a new login with --token LOGIN=/path |
| `--token` | `TOKEN` (repeatable) |  | LOGIN=/path/to/pat for the --read-token or --adjudicator-login login only (a path, never the token) |
| `--observer-route` | `OBSERVER_ROUTE` |  | route the observer feed delivers through |
| `--observer-profile` | `OBSERVER_PROFILE` |  | profile that owns the observer destination |
| `--observer-deliver` | `OBSERVER_DELIVER` |  | where the gateway delivers the feed (telegram, discord, ...) |
| `--observer-events` | `OBSERVER_EVENTS` |  | comma-separated transitions to send, from opened,handoff,verdict,approved,escalation,ruling,stall,closed (blank = all) |
| `--observer-digest-min` | `OBSERVER_DIGEST_MIN` |  | batch the feed into one message per N minutes (0 = per transition) |
| `--observer-mute` |  |  | stop the feed without forgetting it |
| `--observer-unmute` |  |  | resume a muted feed |
| `--observer-disable` |  |  | drop this loop's observer config entirely |
<!-- /flags -->

### apply

Pushes the settings form onto **one** loop. It shows the difference first, then rewrites the
loop config and any route whose profile, prompt or event no longer matches. A form that changed
never touches a running loop until you run `apply` for it.

```bash
hermes review-loop apply --loop name --dry-run
hermes review-loop apply --loop name
```

It also repairs:

- `--recreate-routes` writes routes the gateway's registry lost, from the loop config, with a
  new secret, and re-keys the repo hooks that point at them. Use it when `doctor` says a route
  is missing and `doctor --repair` cannot restore it.
- `--hooks` makes the loop's two repo hooks match its routes: it creates a missing one (paused),
  repoints one at the route's exact URL, and adds a missing event. It needs a token with hook
  write access (`--admin-token`).
- `--watchdog-shim` rewrites the small script the watchdog cron job runs, pointing it at this
  version of the plugin.

If a seat is in the middle of a turn, rebinding its profile or login is refused until the turn
ends. `--while-busy` does it anyway; that turn finishes as the identity it started with.

<!-- flags:apply -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` | **required** | loop id (its config file name; `list` shows them) |
| `--dry-run` |  |  | show the diff without writing it |
| `--while-busy` |  |  | rebind a seat's profile/login even while a run is in flight (that run keeps the identity it started with) |
| `--admin-token` | `ADMIN_TOKEN` |  | login whose token can write the repo's hooks, for the hook moves and re-keys apply makes (default: the reader) |
| `--recreate-routes` |  |  | write this loop's routes the registry lost, from the loop config, with a new secret, and re-key the repo hooks that point at them (when no intent record can restore them) |
| `--hooks` |  |  | make this loop's two repo hooks what its routes need: create a missing one (paused until arm), repoint one at the route's exact URL, add its gate's event (hook write access, see --admin-token) |
| `--watchdog-shim` |  |  | rewrite the cron shim the watchdog job runs, pinned to this plugin's watchdog |
<!-- /flags -->

### fixer-push

Turns **unattended fixer pushes** on or off for one repository. They are off by default. While
off, a "changes requested" verdict is held for you and no fixer turn starts. You fix it by hand,
or turn this on.

```bash
hermes review-loop fixer-push --loop name --enable --acknowledge-pr-race --dry-run
hermes review-loop fixer-push --loop name --enable --acknowledge-pr-race
hermes review-loop fixer-push --loop name --disable
```

`--acknowledge-pr-race` is required to enable it. Read
[the push policy](security.md#unattended-fixer-push-policy-host-operator-not-github-owner-consent)
first: the host checks the PR right before every push, but a PR can still be closed or
retargeted in the moment between that check and Git accepting the push. Enabling is refused
while a fixer turn is running.

<!-- flags:fixer-push -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` | **required** | exact loop id (never all loops) |
| `--enable` |  |  | opt this repository in (one of `--enable`, `--disable`) |
| `--disable` |  |  | turn unattended pushes off (one of `--enable`, `--disable`) |
| `--acknowledge-pr-race` |  |  | accept the residual non-atomic PR-metadata/ref race; required for --enable |
| `--dry-run` |  |  | show action without writing |
<!-- /flags -->

### triage

Turns **issue triage** on or off for one loop, or shows its settings. When an issue opens, a
sandboxed triage seat reads it and adds labels from a fixed list, before any agent works on it.
Off unless you turn it on. New to it? [Issue triage and issue fixes, step by step](issues.md)
walks through the whole setup and a first test.

```bash
hermes review-loop triage --loop name
hermes review-loop triage --loop name --enable --profile tuck --author you --labels bug,feature,docs,question,P0,P1,P2,P3 --dry-run
hermes review-loop triage --loop name --enable --profile tuck --author you --labels bug,feature,docs,question,P0,P1,P2,P3 --admin-token you
hermes review-loop triage --loop name --disable --admin-token you
```

- **Only issues from `--author` logins are triaged.** Anyone else's issue is dropped before any
  model sees it, so spam or a prompt-injection attempt on a public repository costs nothing.
- **Only labels from `--labels` can be applied**, at most `--max-labels` per issue (default 3),
  and a comment only with `--comment on`. The issue text is untrusted, and this list is the
  boundary; the host broker enforces it, not the model.
- **People win.** Labels are only ever added. If the issue already has a label from the list,
  triage writes nothing.
- **It labels as `--login`** (default: the reviewer seat's account), which needs `issues: write`
  and can never be the reader.
- With `--admin-token`, `--enable` also creates the repo hook for `issues` events, paused: run
  [`arm`](#arm) afterwards. Without it, `apply --hooks` creates the hook later.

**Issue fixes.** With `--fix-label LABEL --maintainer LOGIN`, a maintainer applying that label
to an allowlisted author's open issue hands it to the fixer seat. It opens a PR from a new branch
`review-loop/issue-N` that the loop then reviews, or comments on the issue when it cannot fix it.
This needs unattended fixer pushes on ([`fixer-push`](#fixer-push)), and the label can't be one of
the triage labels, so only a person can trigger it.

```bash
hermes review-loop triage --loop name --enable --fix-label agent-fix --maintainer you
```

Step by step: [issues.md](issues.md). Details: [Issue triage](operations.md#issue-triage) and
[issue fixes](operations.md#issue-fixes-a-maintainer-hands-an-issue-to-the-fixer).

<!-- flags:triage -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` | **required** | loop id (its config file name; `list` shows them) |
| `--enable` |  |  | turn triage on (or change it): writes its route, shim and, with --admin-token, its issues hook (paused until arm) (one of `--enable`, `--disable`) |
| `--disable` |  |  | turn triage off: removes its route, shim and (with --admin-token) hook (one of `--enable`, `--disable`) |
| `--profile` | `PROFILE` |  | Hermes profile whose model triages |
| `--author` | `AUTHOR` (repeatable) |  | GitHub login whose new issues are triaged (repeatable); anyone else's are ignored |
| `--labels` | `LABELS` |  | comma-separated labels triage may apply, e.g. bug,feature,docs,question,P0,P1,P2,P3 |
| `--max-labels` | `MAX_LABELS` |  | at most this many labels per issue (default 3) |
| `--comment` | `on` \| `off` |  | allow one short comment with the labels (default off) |
| `--login` | `LOGIN` |  | account that labels (default: the reviewer seat); needs issues: write, never the reader |
| `--token` | `TOKEN` (repeatable) |  | login=/path/to/pat for --login (or --admin-token), if not mapped |
| `--fix-label` | `FIX_LABEL` |  | a label a maintainer applies to hand an issue to the fixer (#214; needs unattended fixer pushes on); '' turns it off |
| `--maintainer` | `MAINTAINER` (repeatable) |  | login whose applying --fix-label counts (repeatable) |
| `--daily-turns` | `DAILY_TURNS` |  | at most this many triage turns per day (0 removes the cap) |
| `--admin-token` | `ADMIN_TOKEN` |  | login whose token can create or delete repo hooks |
| `--dry-run` |  |  | show the change, write nothing |
<!-- /flags -->

---

## Checking an install

### doctor

A read-only preflight: can this machine run the loop at all? It checks the config, the profiles,
the token files, the routes in the gateway's registry, the gate shims, the runtime file's paths,
each seat's model (from its profile, without using a credential), the watchdog job, the clone,
the state directory, the gateway's reachability and the repo hooks. Each line is ✅ verified,
❌ absent or mismatched (with a `fix:` line), or ⚠️ unknown (it could not tell).

```bash
hermes review-loop doctor --loop name
hermes review-loop doctor --loop name --offline
```

- Exit `1` means at least one ❌. With `--strict`, a ⚠️ counts as a failure too.
- `--offline` skips the two network checks (gateway reachability and the repo hooks).
- `--repair` is the one write `doctor` can make: it puts this loop's own routes back from the
  plugin's record of them (same secret) when another tool overwrote or removed them.

`doctor` never starts a turn and never fires a route: a test call to a seat's route could enqueue
a real isolated seat turn. To prove the isolated path end to end, use [`selftest`](#selftest).
`doctor` is the quick check; `selftest` is the authoritative one for the sandbox and models.

<!-- flags:doctor -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` |  | loop id (default: every configured loop) |
| `--offline` |  |  | skip the two network probes (gateway reachability, repo hooks) |
| `--strict` |  |  | treat a check that could not be decided as a failure |
| `--repair` |  |  | restore this loop's own routes from the plugin's intent record (same secret) before checking; the only write doctor makes |
<!-- /flags -->

### selftest

Proves the isolated turn path step by step: the runtime file and its paths, the bubblewrap
sandbox (no network, no credentials visible inside), each seat's model, the GitHub identities,
the broker and the run ledger. It never writes to GitHub, except `--ping`, which asks GitHub to
ping the hooks.

Run it in three stages, each one costing a little more:

```bash
hermes review-loop selftest --loop name --no-model
hermes review-loop selftest --loop name --pr 12
hermes review-loop selftest --loop name --pr 12 --live-turn
```

1. `--no-model` checks everything except the model: no tokens spent.
2. Without `--no-model`, each seat's model also gets one tiny real completion (a few tokens).
   `--pr N` adds a dry run of the reviewer's write authorization on that PR.
3. `--live-turn` runs one real reviewer turn on that PR in the sandbox. Its verdict is printed
   and **never posted**. It takes as long as a real review (up to the reviewer's turn budget).

Every failed step prints a `fix:` line. Exit `1` means a step failed. When `doctor` and
`selftest` disagree (for example about the runtime path), trust `selftest`: it runs the same
checks the worker does.

<!-- flags:selftest -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` | **required** | loop id (its config file name; `list` shows them) |
| `--pr` | `PR` |  | dry-run the reviewer write authorization on this PR |
| `--no-model` |  |  | skip the one tiny real completion (costs a few tokens) |
| `--live-turn` |  |  | with --pr: run one real isolated reviewer turn whose verdict is printed and never posted |
| `--ping` |  |  | ask GitHub to ping each loop hook and report whether the gateway accepted its signature (the selftest's only GitHub write) |
| `--admin-token` | `ADMIN_TOKEN` |  | login whose token may ping hooks (admin:repo_hook or repo) |
| `--timeout` | `TIMEOUT` |  | live turn budget in seconds (default: the loop's reviewer turn_budget_s — the budget the production worker enforces) |
<!-- /flags -->

### models

Lists the models a profile's provider offers, from Hermes's own catalog. Read-only, and it never
uses a credential.

```bash
hermes review-loop models --seat reviewer --loop name
hermes review-loop models --profile vex
```

To change the model a seat uses, change that profile's model in Hermes (`hermes -p vex model`).
The loop always uses the seat profile's model.

<!-- flags:models -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--profile` | `PROFILE` |  | Hermes profile name |
| `--seat` | `reviewer` \| `fixer` \| `adjudicator` |  | use this seat's profile from the loop config |
| `--loop` | `LOOP` |  | loop id for --seat (default: the only loop) |
<!-- /flags -->

---

## Turning it on and off

### arm

Arms (turns on) or pauses (turns off) the loop's repo hooks. `init --hooks` creates them paused,
so nothing happens until you arm. After arming, it asks GitHub to ping each hook and reports
whether your gateway accepted the signature. A hook that looks armed but cannot authenticate
wakes nothing, and this catches it.

```bash
hermes review-loop arm --loop name --admin-token reader-bot
hermes review-loop arm --loop name --pause --admin-token reader-bot
```

Run it after `doctor` and `selftest` pass. It reads back each hook and exits `1` unless GitHub
confirms every one is in the state you asked for. It edits the hooks as `--admin-token`'s login
(default: the reader), which needs hook write access (`repository_hooks: write`, or classic
`repo`).

<!-- flags:arm -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` |  | loop id (default: every configured loop) |
| `--pause` |  |  | pause instead of arming |
| `--admin-token` | `ADMIN_TOKEN` |  | login whose token can edit the repo's hooks (default: the reader) |
<!-- /flags -->

---

## When something is stuck

### explain

Answers "why is this PR not moving, and what has to happen next?" for one pull request. It reads
GitHub and the loop's state and shows:

- the PR's head and whose turn it is;
- the verdicts counted against the cap;
- any queued, running, waiting or failed turn, with its reason and its next retry;
- holds (fixer pushes off, no runtime file, a usage window);
- the one event that would move the PR.

Read-only.

```bash
hermes review-loop explain --loop name --pr 12
```

Its conclusions come from the same checks the live gates run, so it is not a second opinion. It
exits `2` only when it cannot ask: an unknown loop, a refused loop file, or several loops and no
`--loop`.

<!-- flags:explain -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` |  | loop id (default: the only configured loop) |
| `--pr` | `PR` | **required** | pull request number to explain |
<!-- /flags -->

### trace

Answers "why did this webhook start nothing?" A gate that declines an event exits quietly, and
GitHub shows the same `200 {"status": "ignored", "reason": "script"}` for every delivery,
declined or queued alike. `trace` replays one delivery through the **real** gate script, on a
temporary copy of this loop's home, and prints the gate's own reasons:

```bash
hermes review-loop trace --loop name --delivery 40ac7f60-be4a-11f1-8969-4f6489738e63 --admin-token reader-bot
hermes review-loop trace --loop name --payload saved.json --event pull_request
```

- `--delivery` takes the GUID from the hook's **Recent Deliveries** page on GitHub (repo →
  Settings → Webhooks → the hook → Recent Deliveries), or its numeric id. Reading deliveries
  needs hook read access (`--admin-token`).
- `--payload` replays a payload you saved to a file instead.

The last line is the outcome: `would start a reviewer run` (or `would queue a … run`),
`held — <why>` or `declined — <why>`. Anything the gate would send out (GitHub writes, a run
starting, a notice) is listed as `would …` and never done. Your real state is untouched.

`trace` cannot replay `issues` deliveries (issue triage and issue fixes) yet (#230). For those, see
[an issue opened and nothing was labelled](troubleshooting.md#an-issue-opened-and-nothing-was-labelled).

<!-- flags:trace -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` | **required** | loop id (its config file name; `list` shows them) |
| `--delivery` | `DELIVERY` |  | a recorded delivery to this loop's hooks: GitHub's numeric id or the X-GitHub-Delivery GUID (one of `--delivery`, `--payload`) |
| `--payload` | `PAYLOAD` |  | a webhook payload JSON file instead (one of `--delivery`, `--payload`) |
| `--event` | `pull_request` \| `pull_request_review` |  | with --payload: the event it was (default pull_request) |
| `--route` | `ROUTE` |  | the route it was sent to (default: from the delivery's hook, or the event) |
| `--admin-token` | `ADMIN_TOKEN` |  | login whose token can read hook deliveries (admin:repo_hook or repo) |
<!-- /flags -->

### retry

Runs a failed turn again. A turn that failed **before writing anything** retries on its own:
four attempts in all, waiting 2, 4 and 8 minutes between them, then it ends as `failed`.
`retry` re-arms it once more, for example after you raised its turn budget or fixed its model.

```bash
hermes review-loop retry --loop name --pr 12
hermes review-loop retry --loop name --pr 12 --seat fixer
```

It only considers the PR's newest head. A run that **may have written** (it posted a review,
pushed, or ended `uncertain`) is never replayed: `retry` refuses it and prints how to inspect
and reconcile it instead.

<!-- flags:retry -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` | **required** | loop id (its config file name; `list` shows them) |
| `--pr` | `PR` | **required** | the pull request whose failed run to re-arm |
| `--seat` | `reviewer` \| `fixer` \| `adjudicator` \| `triage` \| `issue_fixer` |  | only that seat's run (default: whichever failed at the PR's newest head) |
<!-- /flags -->

### drain

Starts a queued turn now, if its seat has room, instead of waiting for the next watchdog sweep.
Turns queue when a seat is already busy (see `concurrency`).

```bash
hermes review-loop drain --loop name --seat reviewer
```

<!-- flags:drain -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` | **required** | loop id (its config file name; `list` shows them) |
| `--seat` | `reviewer` \| `fixer` | `reviewer` | which seat's queue to drain |
<!-- /flags -->

---

## Housekeeping

### cleanup

Gives a finished PR's disk space back: review checkouts, build directories, logs and locks under
the loop's clone and its `roots`. A merged PR can leave gigabytes behind. Branch checkouts (your
own working copies) are never touched.

```bash
hermes review-loop cleanup --loop name --dry-run
hermes review-loop cleanup --loop name
hermes review-loop cleanup --loop name --pr 12
```

Without `--pr` it sweeps every closed PR the clone knows about. Closing a PR also runs cleanup
for it automatically. Use `--dry-run` first to see what would go.

<!-- flags:cleanup -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` | **required** | loop id (its config file name; `list` shows them) |
| `--pr` | `PR` |  | clean one closed PR (default: sweep every closed PR the clone knows about) |
| `--dry-run` |  |  | list what would be removed, remove nothing |
<!-- /flags -->

### uninstall

Removes a loop, the reverse of `init`: its repo hooks, the watchdog job (only when no other loop
still needs it), its routes and gate shims, and its config. With `--purge` it also removes the
loop's default state directory.

```bash
hermes review-loop uninstall --loop name --admin-token reader-bot
```

It undoes the parts that need the config first. If any of them cannot be undone (say, deleting a
hook is refused), it stops, keeps the config so you can retry, and prints the commands that
finish the job. It refuses to leave live hooks behind unless you pass `--keep-hooks`.
`--keep-config` removes everything except the config file.

<!-- flags:uninstall -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` | **required** | loop id (its config file name; `list` shows them) |
| `--keep-config` |  |  | remove hooks, cron job and routes but keep the loop config file |
| `--admin-token` | `ADMIN_TOKEN` |  | login whose token can delete hooks (admin:repo_hook or repo) |
| `--keep-hooks` |  |  | leave the repo hooks live (explicit opt-out) |
| `--purge` |  |  | also delete the loop's default state directory |
<!-- /flags -->
