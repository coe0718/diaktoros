# Getting started with GitHub Apps

[Documentation map](README.md) · Previous: [Getting started](getting-started.md) · Next: [Operations](operations.md)

A loop's reader, reviewer, fixer and adjudicator can each be a **GitHub App** instead of a person's
account with a PAT. Each App is its own `<slug>[bot]` identity. Its private key stays on the host
at mode 600, and every call uses a short-lived installation token for this one repository. A loop
made entirely of Apps needs no personal account and no PAT, not even for its webhooks.

This guide makes a loop's Apps, sets up a new loop with them (or moves an existing one), and
checks it before arming. It assumes the host is ready as in [Getting started](getting-started.md):
Hermes, the gateway, bubblewrap and the seat profiles. Follow that guide's install step for the
plugin first.

## What changes in App mode

| | With PATs | With GitHub Apps |
| --- | --- | --- |
| Identities | One account and PAT file per role | One App per role; a private key per App, mode 600 |
| Events | One repo hook per seat route, made with an admin token | The reader App's own webhook, one route for every loop it serves (`ingress: app-webhook`) |
| Asking for a review | Request the reviewer on GitHub | GitHub can't request an App, so the host hands the fixer's request to the reviewer itself. By hand: `hermes dk review --loop ID --pr N` |
| Merge protection | GitHub counts the reviewer's approval | GitHub never counts an App's approval. The loop posts the `diaktoros/review` check instead, and `protect` requires it |
| Pausing | `arm --pause` pauses the repo hooks | `arm --pause` sets the loop's own flag; the dispatcher drops its deliveries |

What App bots can and can't do on GitHub is in [GitHub App support](github-app-support.md).

## 1. Make the Apps

`create-apps` makes them for you: one "Create GitHub App" click per App in your browser, with the
name, permissions and webhook filled in. The private keys are written to the host and never printed
or copied by hand.

```bash
hermes dk create-apps --owner "<you-or-your-org>" --prefix "<name>-dk" --host "https://<your-gateway>"
```

- `--owner` is the account or organization that owns the Apps; add `--org` for an organization.
- Each App is named `<prefix>-<role>`. GitHub limits App names to 34 characters.
- For an existing loop, use `--loop ID` instead of `--prefix`/`--host`. Its id names the Apps
  (`<id>-dk-…`) and its gateway receives the reader's webhook.
- The roles are reader, reviewer and fixer by default, plus the adjudicator when the loop has
  one; `--roles` picks others.
- Keys go to `$HERMES_HOME/apps/<slug>.pem` (`--key-dir` to change it).
- If your browser is on another machine, use `--no-browser`. The wizard writes each App's page to
  a private file; open it there, click, and paste back the address GitHub sends you to.
- A run that stops part way resumes when you rerun it with the same options, and skips the Apps
  it already made.

The reader App gets the only active webhook, and Webhooks and Administration **Read and write**.
That makes it the hook admin, and lets it run `protect`. The permissions each App gets are listed
in [Accounts](accounts.md#a-github-app-identity-instead-of-an-account).

When it finishes, it prints an install link for each App, then the commands for the next steps.
**Install each App on the loop's repository only.**

## 2a. A new loop

Pass each App with `--app LOGIN=APP_ID:/abs/key.pem`, the reader as `--read-token`, and the
reader as the App whose webhook delivers the loop's events:

```bash
hermes dk setup --repo "<owner>/<repository>" \
  --reviewer "<name>-dk-reviewer[bot]" --fixer "<name>-dk-fixer[bot]" --read-token "<name>-dk-reader[bot]" \
  --app "<name>-dk-reader[bot]=<id>:/abs/<name>-dk-reader.pem" \
  --app "<name>-dk-reviewer[bot]=<id>:/abs/<name>-dk-reviewer.pem" \
  --app "<name>-dk-fixer[bot]=<id>:/abs/<name>-dk-fixer.pem" \
  --ingress app-webhook --ingress-app "<name>-dk-reader[bot]" --protect
hermes dk app-webhook --loop ID --set
```

`setup` asks for anything else it needs (profiles, the gateway origin) as usual. It installs the
App's route and creates no repo hooks. `--protect` makes the base require `diaktoros/review`,
pinned to the reviewer App. `app-webhook --set` points the reader App's webhook at the loop's
route and sets its secret; the secret is never printed. `init` takes the same flags, if you prefer
to wire the loop flag by flag.

## 2b. An existing loop

Move one identity at a time with [`identity`](commands.md#identity): a dry run, then the move. The
App is resolved live on GitHub first, and nothing is written unless that succeeds. Every open PR
keeps its history.

```bash
hermes dk identity --loop ID --seat read --app "<name>-dk-reader[bot]=<id>:/abs/key.pem" --dry-run
hermes dk identity --loop ID --seat read --app "<name>-dk-reader[bot]=<id>:/abs/key.pem"
```

Do the same for `--seat fixer`, `--seat reviewer` and, with an adjudicator, `--seat adjudicator`.
If the plugin settings form names that seat's login, set it to the App's login or blank it.
`identity` tells you when it does, and until then `apply` refuses rather than move the seat back.

Then switch the loop's events to the reader App's webhook, and require the merge check:

```bash
hermes dk set --loop ID --ingress app-webhook --ingress-app "<name>-dk-reader[bot]"
hermes dk app-webhook --loop ID --set
hermes dk protect --loop ID --admin-token "<name>-dk-reader[bot]"
```

The loop's old repo hooks aren't deleted for you. Until you delete them, the gates drop whatever
they deliver, so nothing runs twice and `arm --pause` holds. `doctor` lists each one by id. Each
step reverses the same way: `identity … --pat LOGIN=/abs/path`, or `set --ingress repo-hooks`.

## 3. Turn required approvals off

GitHub never counts an App's approval. On a base that still requires approvals, every PR needs a
person's approval as well. With an App reviewer, `diaktoros/review` is the gate: turn required
approvals off on the base in repository settings → Rules. `protect` and `doctor` remind you while
they're still on.

## 4. Verify and arm

```bash
hermes dk doctor --loop ID
hermes dk selftest --loop ID --no-model
hermes dk arm --loop ID
```

The lines that matter in App mode:

| `doctor` line | What green means | If it isn't green |
| --- | --- | --- |
| `hooks` | "none, on purpose": events arrive through the App's webhook | It names the leftover repo hooks by id; delete them |
| `app-webhook` | The App's webhook posts to this loop's route with every event it needs | `hermes dk app-webhook --loop ID --set`, and subscribe to any missing event in the App's settings (Permissions & events) |
| `gateway-script:dk-app-…` | The gateway can run the route's script | `hermes dk apply --loop ID` writes it |
| `merge-check` | The base requires `diaktoros/review`, pinned to the reviewer App | `hermes dk protect --loop ID` |
| `credential:<seat>`, `app:<login>` | Each App's key is private and its installation can reach the repository | Install the App on the repository, or accept a new permission on its installation |

`arm` won't say "armed" until the App's route is installed, the gateway can run its script, and
the App's webhook would deliver. `selftest` skips the repo-hook step for an App loop.

## 5. The first PR

Open a PR from an allowlisted author. The reader App's webhook delivers `opened`, and the reviewer
App reviews it. `diaktoros/review` turns green when the head is approved and nothing holds it, and
red when something needs a change. To ask for another review after a push, run
`hermes dk review --loop ID --pr N`: a GitHub review request can't reach an App.

```bash
hermes dk explain --loop ID --pr N
```

`explain` names the App's webhook as the way events arrive, and shows every gate decision for the
PR, with its reason.

## Next steps

- [Accounts](accounts.md#rotate-or-revoke): what each App's permissions are for, and rotating an App's key.
- [Commands](commands.md): `create-apps`, `identity`, `app-webhook` and `protect` in full.
- [GitHub App support](github-app-support.md): what App bots can and can't do on GitHub, observed.
- [Operations](operations.md): running the loop day to day.
