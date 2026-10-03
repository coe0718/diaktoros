# Accounts and tokens, step by step

This page walks you through the GitHub side of a review loop: which accounts you need, how to
create them, how to give each one a token, where to keep the tokens, and how to check that the
loop sees them correctly. It assumes you have never made a second GitHub account or a
fine-grained token before.

Terms used here:

* **PAT** (personal access token) — a password-like string GitHub issues to one account, so a
  program can act as that account. GitHub has two kinds: **classic** (broad scopes such as `repo`)
  and **fine-grained** (per-repository permissions such as `pull_requests: read`).
* **Seat** — one of the two agents in the loop: the **reviewer** or the **fixer**. Each seat acts
  as its own GitHub account. The optional issue seats reuse these accounts: **triage** labels as
  the reviewer's account by default, and an **issue fix** is the fixer working from an issue
  ([issues](issues.md)).
* **Token file** — a small file that holds one account's PAT and nothing else. The loop stores the
  file's *path*, never the token.

How the loop works as a whole is in [concepts](concepts.md). The scopes table this page follows is
[Token scopes by role](operations.md#token-scopes-by-role). Every flag is listed in
[commands](commands.md).

## 1. Why the loop needs several accounts

The loop exists so that **a different account reviews the fixer's work**. If one account wrote the
code and approved it, the review would mean nothing. So the loop refuses to run with shared
accounts.

The rule is called the **four-identity rule**. In the loop's own words:

> the four-identity rule: the reader, the reviewer, the fixer and (if set) the adjudicator comment
> login must be four different accounts with four different token files

In plain words:

* The **fixer** and the **reviewer** are two different GitHub accounts.
* The **reader** is a third account. It only reads. The loop's scripts read every pull request,
  branch and hook through it, and the broker (the host-side process that makes every GitHub write
  for the seats) refuses every write while the reader is also a seat.
* The **adjudicator comment login** is optional. If you set it, it is a fourth account. When the
  review budget runs out, an isolated adjudicator turn rules on the PR; with this login set, the
  ruling is also posted as a PR comment. Without it, rulings go to you (the operator) only, and
  that is a valid setup.
* Each of these accounts has **its own token file**. Two logins that point at the same file are
  one account wearing two hats, and the loop treats it that way.

The repository owner usually fills one of these roles. On a repo owned by your personal account
(a **user-owned** repo), the owner is normally the **reader** and also the **hook admin** — the
account that creates, arms and pauses the repo's webhooks. Only the owner can manage hooks on a
user-owned repo. The hook admin is not a fifth identity; it is whichever login `--admin-token`
names, and by default it is the reader.

| role | example login | what it does | needs write? | where it's configured |
| --- | --- | --- | --- | --- |
| reader | `reader-bot` (on a user-owned repo, usually your own owner account) | reads PRs, refs and the repo's hooks for every gate decision | no — read-only. Hook write only if it also creates and arms the hooks (the default) | `init`/`set --read-token LOGIN` plus `--token LOGIN=PATH`; loop keys `read_token` and `tokens`. The settings form has no reader field |
| reviewer seat | `rev-bot` | posts one review per turn | yes (a review needs pull-request write) | `init --reviewer LOGIN` (and `--reviewer-seat` if you list several) plus `--token`; settings `reviewer_login`, `reviewer_token_file`; loop keys `reviewers`, `reviewer_seat`, `seats.reviewer.login` |
| fixer seat | `dev-account` | pushes fix commits, answers the review, asks for review again. The PRs the loop works on must be opened by a login in the fixers allowlist — usually this one | yes (a fix is a commit) | `init --fixer LOGIN` plus `--token`; settings `fixer_login`, `fixer_token_file`; loop keys `fixers`, `seats.fixer.login` |
| adjudicator comment login (optional) | `rule-bot` | posts the ruling as one PR comment | on a user-owned repo, yes (see below); on an org repo, `pull_requests: write` | `init --adjudicator-login LOGIN` (needs `--adjudicator-route`) plus `--token`; settings `adjudicator_login`, `adjudicator_token_file`; loop key `seats.adjudicator.login` |
| triage (optional, not a separate identity by default) | `rev-bot` (the reviewer seat's account) | adds labels to a new issue, and one short comment if you allow it | yes: `issues: write`. It can never be the reader | `triage --login LOGIN` (default: the reviewer seat) plus `--token` if that login has no file yet; loop key `triage.login` |
| issue fixer (optional, not a separate identity) | `dev-account` (the fixer seat's account) | pushes a new branch `review-loop/issue-N`, opens a PR from it, requests the review, or comments on the issue when it cannot fix it | yes (a branch, a PR and an issue comment) | nothing extra: it is always the fixer seat; turned on with `triage --fix-label` |
| hook admin (not a separate identity) | usually the reader | creates, arms, pauses and deletes the repo's two webhooks (three with triage on) | hook write | `--admin-token LOGIN` on `init --hooks`, `arm`, `apply`, `uninstall`, `trace`, `selftest --ping`, `triage --enable`/`--disable`; default: the reader |

Why the adjudicator login needs write on a user-owned repo: a comment needs only read access, but
a user-owned repo has no read-only collaborator grant, so any collaborator can write. That is a
GitHub limitation, explained in [Token scopes by role](operations.md#token-scopes-by-role).

## 2. Create the extra GitHub accounts

For a basic loop on a user-owned repo you need **two** new accounts: one for the reviewer
(`rev-bot`) and one for the fixer (`dev-account`). Your own account is the reader. Add a third
(`rule-bot`) only if you want rulings posted as PR comments.

You may instead use a dedicated `reader-bot` account as the reader. On a user-owned repo that
account must be a collaborator, which means it gets write access and needs a classic token (see
step 3). It also cannot read the repo's hooks, so `doctor` reports the hook lines as unknown. Using
your own owner account as the reader avoids all of that.

### Things to know first

* **One email address per account.** GitHub does not let two accounts share an email address. If
  your email provider supports plus-addressing (`you+revbot@example.com`), that gives you a
  separate address that still arrives in your inbox. Check that your provider supports it before
  relying on it.
* **Machine accounts are allowed.** GitHub's Terms of Service allow a *machine account*: an account
  a person sets up and is responsible for, used only to run automated tasks. Read the current terms
  yourself before creating several; you remain responsible for what each account does.
* **Two-factor authentication (2FA).** GitHub may require 2FA on each account. Set it up with an
  authenticator app and store the recovery codes somewhere safe. 2FA protects the web login; a PAT
  keeps working without it, so the loop does not need your 2FA codes.
* **Use a private browser window** for each new account, so you do not confuse it with your own
  signed-in session.

### Steps

1. Open a private browser window and go to `https://github.com/signup`.
2. Enter the account's email address, a strong password and the username you want (for example
   `rev-bot`). Usernames must be unique on GitHub, so you may need a variation.
3. Verify the email address from the message GitHub sends.
4. Set up 2FA if GitHub asks (Settings → Password and authentication).
5. Write down the exact login and type it exactly as GitHub shows it. You will use it in several
   flags, and `selftest` checks that each token belongs to the login it is mapped to.
6. Repeat for `dev-account` and, if you want it, `rule-bot`.

### Invite each account to the repository

The seat accounts must be **collaborators** on the repo, or they cannot push or review.

1. Sign in as the **repo owner** (your own account).
2. Open the repository on GitHub, for example `https://github.com/owner/name`.
3. Go to **Settings → Collaborators** (on newer layouts this sits under **Access**). GitHub may ask
   you to confirm your password or 2FA.
4. Click **Add people**, type the account's login (`rev-bot`), select it and confirm.
5. Repeat for `dev-account` and, if used, `rule-bot`.

Which permission level each account gets:

| repo owned by | reviewer | fixer | adjudicator login | separate reader account |
| --- | --- | --- | --- | --- |
| a user account | collaborator (write — the only grant a user-owned repo offers) | collaborator (write) | collaborator (write) | collaborator (write) |
| an organization | a role with pull-request write (**Write**) | **Write** | **Write** | **Read** |

On an organization repo you pick a role when you add someone. An org repo has a read-only role, so
an account that only reads needs no write permission at all.

### Accept the invitation

Each invited account must accept, or it has no access.

1. In a private window, sign in as the invited account (`rev-bot`).
2. Open the invitation email and click the link, or go to `https://github.com/notifications`, or
   go straight to `https://github.com/owner/name/invitations`.
3. Click **Accept invitation**.
4. Repeat for each invited account. Invitations expire if nobody accepts them; if one did, invite
   the account again.

## 3. Create a token for each account

Each account gets **its own** token, created while signed in **as that account**. A token always
acts as the account that created it.

Which kind of token each role needs (from
[Token scopes by role](operations.md#token-scopes-by-role)):

| role | user-owned repo | org-owned repo (fine-grained) |
| --- | --- | --- |
| reader (the owner) | **fine-grained, read-only**: `contents: read`, `pull_requests: read`, `repository_hooks: read` (classic `repo` also works). If the reader also creates and arms the hooks — the default — make it `repository_hooks: write` | `contents: read`, `pull_requests: read`, `repository_hooks: read` |
| reviewer | **classic**, `repo` | `pull_requests: write` |
| fixer | **classic**, `repo` | `contents: write`, `pull_requests: write` |
| adjudicator login | **classic**, `repo` | `pull_requests: write` |
| triage login (default: the reviewer's token) | **classic**, `repo` (the reviewer's token already has it) | add `issues: write` |
| issue fixer (the fixer's token) | **classic**, `repo` (already has it) | `contents: write`, `pull_requests: write`, and add `issues: write` |

Why classic for the collaborator seats on a user-owned repo: GitHub documents that a fine-grained
token cannot contribute to repositories where the user is an outside or repository collaborator. A
fine-grained token is also bound to one *resource owner*, and for a repo you do not own that owner
is a different account. So the reviewer, the fixer and the adjudicator login use classic `repo`
tokens there. The reader is the exception: it *is* the owner, so it can hold a narrow, read-only
fine-grained token. A separate `reader-bot` collaborator on a user-owned repo is a collaborator
like the seats, so it would need classic `repo` too.

The reader reads pull requests, so its token needs **Pull requests** read access in both columns;
`selftest`'s `github:repo` step checks it. With triage on, the reader also reads each new issue
before a turn starts and again before the label is written, so a fine-grained reader on an org repo
also needs `issues: read`. (On a user-owned repo the owner-reader's fine-grained token covers its
own repo's issues once you add **Issues: Read-only**.)

On GitHub's settings page, the `repository_hooks` permission is labelled **Webhooks**,
`contents` is **Contents**, `pull_requests` is **Pull requests** and `issues` is **Issues**. GitHub adds **Metadata:
read-only** to every fine-grained token automatically.

### Fine-grained token (the reader, or any role on an org repo)

1. Sign in as the account the token is for (for the reader on a user-owned repo: you, the owner).
2. Click your avatar (top right) → **Settings**.
3. In the left sidebar, at the bottom: **Developer settings**.
4. **Personal access tokens → Fine-grained tokens → Generate new token**.
5. **Token name**: something you will recognise later, for example `review-loop reader for
   owner/name`.
6. **Resource owner**: the account or organization that owns the repo (`owner`).
7. **Expiration**: pick a date you will actually remember to act on, for example 90 days, and put
   it in your calendar. An expired token fails as an authentication error that can look like a code
   bug.
8. **Repository access**: **Only select repositories**, then choose `owner/name`. Do not choose
   "All repositories".
9. **Permissions → Repository permissions**: set exactly the permissions from the table above.
   For the owner-reader that also manages hooks: **Contents: Read-only**, **Pull requests:
   Read-only**, **Webhooks: Read and write**.
10. Click **Generate token** and copy the token now. GitHub shows it only once.
11. Paste it into the account's token file (step 4 below) before you close the page.

An organization may require an org owner to approve fine-grained tokens before they work. If a
token is pending approval, approve it in the organization's settings.

### Classic token (the reviewer, fixer and adjudicator login on a user-owned repo)

1. Sign in **as that account** (`rev-bot`, then `dev-account`, then `rule-bot`) in a private
   window.
2. Avatar → **Settings → Developer settings → Personal access tokens → Tokens (classic) →
   Generate new token → Generate new token (classic)**.
3. **Note**: for example `review-loop reviewer for owner/name`.
4. **Expiration**: as above — a date you will act on.
5. **Select scopes**: tick **`repo`** only. Do not tick `workflow`, `admin:org`, `delete_repo` or
   anything else. The loop never needs `workflow`: the broker refuses any path under `.github/`
   before it runs git at all.
6. **Generate token**, copy it, and paste it into that account's token file straight away.

A classic `repo` token reaches every repository the account can reach. That is why the seat
accounts should be dedicated accounts that are collaborators on this repo and nothing else.

### The hook admin's token

`init --hooks`, `arm`, `arm --pause`, `apply --hooks`, `uninstall` and
`triage --enable`/`--disable --admin-token LOGIN` edit the repo's webhooks.
They act as the `--admin-token` login, or as the reader when you pass none. That token needs hook
**write** access: fine-grained `repository_hooks: write` (Webhooks: Read and write), classic
`admin:repo_hook`, or classic `repo`, which already includes it.

It is `admin:repo_hook` and not the narrower `write:repo_hook`, because a failed hook install rolls
back by deleting the hooks it created; a write-only token would leave an orphaned hook. The full
reasoning is at the end of [Token scopes by role](operations.md#token-scopes-by-role).

There are two webhooks, or three with triage on: `triage --enable --admin-token LOGIN` creates the
third (for `issues` events, paused until `arm`), and `triage --disable --admin-token LOGIN` deletes
it.

If you want the reader's token to stay strictly read-only, leave `--hooks` off and add and toggle
the hooks by hand on GitHub — or, on an org repo, use a separate admin login with its own token
file and pass `--admin-token` to every hook command.

## 4. Store the tokens safely

Each token goes in its own file, readable only by you.

1. Create the keys directory, private from the start:

   ```bash
   (umask 077; mkdir -p ~/.hermes/keys)
   ```

2. Write each token into its own file named after the login. Use an editor so the token never
   appears in your shell history or in the process list:

   ```bash
   (umask 077; $EDITOR ~/.hermes/keys/reader-bot-pat)
   (umask 077; $EDITOR ~/.hermes/keys/rev-bot-pat)
   (umask 077; $EDITOR ~/.hermes/keys/dev-account-pat)
   (umask 077; $EDITOR ~/.hermes/keys/rule-bot-pat)   # only if you use an adjudicator login
   ```

   Paste the token as the only content and save. A trailing newline is fine.

3. Make sure each file is mode 600 (owner read/write only):

   ```bash
   chmod 600 ~/.hermes/keys/*-pat
   ls -l ~/.hermes/keys/
   ```

   Every line should start with `-rw-------`.

The rules the loop enforces on a token path:

* It must be an **absolute path** once `~` is expanded (`/…` or `~/…`).
* It must **exist** and be a **regular file** that **you own**.
* It must not be readable by group or others (**mode 600**). A symlink is followed and its target
  must pass the same checks.
* It must not be **empty**.

Where tokens must **not** go:

* **Not in a Hermes profile's `.env`.** The seats never use a `GH_TOKEN` there; every GitHub write
  goes through the host broker with the mapped token file. A copy in `.env` is only an extra copy
  to leak. `doctor` flags a `GH_TOKEN` in a seat profile's `.env` when no token file is mapped.
* **Not in the loop config or the settings form.** Both hold only paths. `status`, `settings`,
  `doctor` and every refusal print paths, never token values.

How the loop checks the files: the path checks (absolute, owner, mode) look at file metadata only.
The checks that the file is present and non-empty open it to see that it is not blank, but never
print, hash or copy what is in it. `doctor` reports each seat's file like this:

```
  ✅ credential:reviewer  rev-bot → /home/you/.hermes/keys/rev-bot-pat (exists: yes, private: yes), nonempty (identity and API access not checked)
```

## 5. Tell the loop about the accounts

### With `init` flags

`init` takes the logins and the token paths together:

| flag | what it does |
| --- | --- |
| `--fixer LOGIN` | a login allowed to open and push the loop's PRs (repeatable). The first one serves the fixer seat unless the settings form names another |
| `--reviewer LOGIN` | a login whose verdicts count (repeatable). With several, `--reviewer-seat LOGIN` names the one the reviewer seat acts as |
| `--read-token LOGIN` | **required**: the reader's login. Its own account, never a seat or the adjudicator login |
| `--token LOGIN=/path/to/pat` | maps one login to its token file (repeatable). Give one for the reader, each seat and the adjudicator login |
| `--adjudicator-login LOGIN` | optional fourth account the ruling is also posted as. Needs `--adjudicator-route` and its own `--token` |
| `--admin-token LOGIN` | the login whose token creates the hooks with `--hooks` (default: the reader). It must have its own `--token` |

A full example, with a separate reader account, an adjudicator login and the hooks left for a
later `arm`:

```bash
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

Preview first with `--dry-run`. It prints a `credentials:` line showing which login reads which
file, and writes nothing:

```bash
hermes review-loop init --repo owner/name --fixer dev-account --reviewer rev-bot --fixer-profile drey --reviewer-profile vex --read-token reader-bot --token reader-bot=~/.hermes/keys/reader-bot-pat --token rev-bot=~/.hermes/keys/rev-bot-pat --token dev-account=~/.hermes/keys/dev-account-pat --host https://your-gateway.example --dry-run
```

The README's [install example](../README.md#install) shows the common user-owned shape: the owner
account is the reader *and* the hook admin (`--hooks --admin-token owner-account`), so the owner's
token needs hook write.

### With `setup`

[`setup`](commands.md#setup) asks for the same accounts as `init`, one question at a time, with
the settings form's values as defaults. In a script, pass them as flags with `--yes`:

| flag | what it holds |
| --- | --- |
| `--reviewer-token PATH` | the reviewer's token file |
| `--fixer-token PATH` | the fixer's token file |
| `--read-token LOGIN` | the reader's login (its own account) |
| `--read-token-file PATH` | the reader's token file |
| `--admin-token LOGIN` | the hook admin's login; the hooks are created (paused) as it |
| `--admin-token-file PATH` | the hook admin's token file |

```bash
hermes review-loop setup --repo owner/name --yes --reviewer rev-bot --fixer dev-account --reviewer-profile vex --fixer-profile drey --reviewer-token ~/.hermes/keys/rev-bot-pat --fixer-token ~/.hermes/keys/dev-account-pat --read-token reader-bot --read-token-file ~/.hermes/keys/reader-bot-pat --host https://your-gateway.example
```

`setup` sets up no adjudicator, triage or issue fixes. Triage and issue fixes are added later with
`triage` ([issues](issues.md)); adding an adjudicator is described under
[the switches](concepts.md#the-switches).

### With the settings form

The desktop form at **Capabilities → Plugins → review loop** holds per-profile defaults for a new
loop ([Settings, in the desktop](settings.md)). Its credential fields hold **paths only**:

| setting | what it holds |
| --- | --- |
| `reviewer_login` / `reviewer_token_file` | the reviewer's login, and the path to its PAT file |
| `fixer_login` / `fixer_token_file` | the fixer's login, and the path to its PAT file |
| `adjudicator_login` / `adjudicator_token_file` | the optional adjudicator comment login and its file; lands only on a loop with an adjudicator route |

There is no reader field: the reader is always named with `--read-token`. Each path in the form is
checked (absolute, exists, yours, mode 600) before anything is written. An explicit `--token` for a
login wins over the form's path for it. The key-by-key mapping is in
[Plugin settings](configuration.md#plugin-settings-the-desktop-form).

### Changing them later

| what changes | how |
| --- | --- |
| the reader, or the reader's file | `set --read-token LOGIN --token LOGIN=/path` (the same command repairs a loop file with no `read_token`) |
| the adjudicator login, or its file | `set --adjudicator-login LOGIN --token LOGIN=/path`; `--adjudicator-login ""` clears it |
| a seat's login or token file | put the new value in the settings form, then `apply --loop name` |

```bash
hermes review-loop set --loop name --read-token reader-bot --token reader-bot=~/.hermes/keys/reader-bot-pat
hermes review-loop set --loop name --adjudicator-login rule-bot --token rule-bot=~/.hermes/keys/rule-bot-pat
hermes review-loop apply --loop name --dry-run
hermes review-loop apply --loop name
```

`set --token` accepts the login named by `--read-token` or `--adjudicator-login` in the same
command, or an extra login such as a hook admin (`set --loop name --token admin-login=/path`). For a
seat's login it refuses with "seat token files move through the plugin settings and `apply`". A new
token-file *path* for a seat counts as changing who that seat is, so `apply` refuses while that
seat has a run in flight; `apply --while-busy` is the explicit override, and the running turn
finishes under the identity it started with
([Seat identity](configuration.md#seat-identity-who-serves-each-seat)).

If you move the reader off a seat account and the hooks are edited without `--admin-token`, then
`arm`, `arm --pause` and `uninstall` act as the new reader — so its token needs hook write, or pass
`--admin-token` with the owner's login (mapped with its own `--token`, at `init` or with `set`).

## 6. Check that it worked

### `doctor`

```bash
hermes review-loop doctor --loop name
```

Look for these lines, all ✅ (full example output in
[Preflight: `doctor`](operations.md#preflight-doctor)):

| line | what it proves |
| --- | --- |
| `credential:reviewer`, `credential:fixer` | each seat's login maps to a file that exists, is non-empty and is private: `path (exists: yes, private: yes)` |
| `credential:adjudicator` | only with an adjudicator login: its own file, not shared with the reader or a seat |
| `token:<login>` | one per mapped file: `(mode 600, non-empty)` |
| `read_token` | `reader-bot (mapped in tokens; its own account and file)` |
| `hook:<route>` | the read token could list the repo's hooks; ⚠️ unknown means it lacks hook read access |
| `profile:triage`, `credential:triage` | only with triage on: the triage profile exists, and the triage login (the reviewer's by default) has a private, non-empty token file. The fix line reminds you it needs `issues: write` |

`doctor` does not contact GitHub as each account — it says so: "identity and API access not
checked". That is `selftest`'s job.

### `selftest`

```bash
hermes review-loop selftest --loop name --no-model
```

Step **4. GitHub identities** calls GitHub's `/user` endpoint with each token and checks:

* `github:read`, `github:reviewer`, `github:fixer` (and `github:adjudicator`) — each token belongs
  to the login it is mapped to, printed as `rev-bot (id 12345)`.
* `github:distinct-files` — no two identities share one token file.
* `github:distinct` — `4 distinct principals` (or 3 without an adjudicator login): every token is a
  different GitHub account.
* `github:repo` — `owner/name readable as reader-bot`.

The broker repeats the distinct-account check before every write, so a token swapped later is still
caught. More on the steps: [`selftest`](operations.md#verifying-the-isolated-setup-selftest).

### Common refusals and their fixes

The messages below are quoted from the code. `init` prefixes them with `config refused:`, `set`
with `refused:`, and they name the loop id first. Logins appear in quotes.

| message (excerpt) | what it means | fix |
| --- | --- | --- |
| `the reader 'rev-bot' is also the reviewer seat` (or `fixer seat`, `adjudicator comment login`) | the reader and a seat are the same account | give the reader its own account: `set --loop name --read-token reader-bot --token reader-bot=/path` |
| `the reader 'reader-bot' and the fixer seat 'dev-account' read the same token file — one account wearing two hats` | two logins point at one file | create a separate token for each account and map each to its own file |
| `the reviewer and the fixer read the same token file — each seat needs its own credential` | same, between the two seats | give each seat its own file, through the settings form and `apply` |
| `the adjudicator and 'rev-bot' read the same token file` | the adjudicator login shares a file | give `rule-bot` its own file |
| `--read-token LOGIN names the account the gates read GitHub as (map its file with --token LOGIN=/path/to/pat)` | `init` without `--read-token` | add `--read-token` and its `--token` |
| `read_token 'reader-bot' has no entry in 'tokens' — the gates read GitHub as that login` | the reader has no mapped file | add `--token reader-bot=/path` |
| `no token mapped for the reviewer login 'rev-bot' — add --token rev-bot=/path/to/pat (its own PAT file, never the reader's)` | a seat has no file | add its `--token` at `init`, or its `*_token_file` in the form and `apply` |
| `no token file mapped for 'rev-bot' (a GH_TOKEN in … would not be used)` (`doctor`) or `rev-bot: no token file mapped` (`selftest`) | same, seen by the checks | as above |
| `--admin-token 'owner-account' has no token file — add --token owner-account=/path/to/pat (hook write access)` | `init --hooks` names an admin with no mapped file | add that `--token` to the same `init` |
| `token file for 'rev-bot' is missing (…)` / `is empty (…)` | the path is wrong or the file is blank | fix the path, or paste the token into the file |
| `… is mode 644 — group/other can read it (chmod 600 …)` | the file is readable by others | `chmod 600` the file |
| `… is not an absolute path (use /… or ~/…)` | a relative path | use `~/…` or `/…` |
| `… is owned by uid …, not you (…)` | someone else owns the file | recreate it as yourself |
| ``the token for 'reader-bot' needs hook write access on owner/name (classic `repo` or `admin:repo_hook`, or fine-grained `repository_hooks: write`)`` | `init --hooks` or `arm` was refused by GitHub | widen that token to hook write, or pass `--admin-token` with the owner's login |
| `cannot read a complete, valid repo hook listing; no changes made` | the token used could not list hooks | same as above: a token with hook access |
| `the token mapped to rev-bot belongs to dev-account` (`selftest`) | the wrong token is in the file | put `rev-bot`'s own token in `rev-bot`'s file |
| `rev-bot: GET /user failed (…)` (`selftest`) | the token is invalid, expired or revoked | create a new token for that account (section 7) |
| `same GitHub principal for: read + reviewer` (`selftest`) | two files hold tokens of one account | each role needs a separate GitHub account |
| `owner/name not readable as reader-bot` (`selftest`) | the reader's token cannot see the repo | give it Contents and Pull requests read on this repo |
| `read, reviewer and fixer tokens resolve to same principal`, `seat token principal cannot be verified` (broker) | the broker's own check before a write | run `selftest --no-model`; its identities step names the cause |

When a reader problem exists on an installed loop, `status` also shows it on a `⚠️  reader:` line
with the `set` command that fixes it. More symptoms are in [troubleshooting](troubleshooting.md).

## 7. Rotate or revoke a token

To rotate (replace) a token, **keep the same file path and replace its contents**. Nothing else
changes: the loop config, the settings form and the routes all name the path, not the token, so
there is nothing to `set` or `apply`.

1. Sign in as the account and create a new token with the same settings (section 3).
2. Open the existing file and replace the old token with the new one:

   ```bash
   (umask 077; $EDITOR ~/.hermes/keys/rev-bot-pat)
   chmod 600 ~/.hermes/keys/rev-bot-pat
   ```

3. Check it: `selftest` step 4 should show the login and its id again.

   ```bash
   hermes review-loop selftest --loop name --no-model
   ```

4. Only now delete the old token on GitHub (the token's page → **Delete**, or **Revoke** for a
   classic token).

Doing it in that order means there is no moment with no working token. GitHub's **Regenerate
token** button also works, but it stops the old token at once, so the file is briefly stale.

What happens to running turns: the token file is read when the loop needs it — for each broker
write, push and trusted read — not once at the start of a turn. A turn that is already running uses
whatever the file holds at its next GitHub call. If the old token stops working before the file
holds the new one, a call in that gap fails as an authentication error. A run that failed before
any GitHub write can be re-armed with `retry`; see
[When an isolated run fails](operations.md#when-an-isolated-run-fails).

To **revoke** a token in an emergency (for example, it leaked), delete it on GitHub first, then
put a fresh one in the file. Until you do, that role cannot read or write, and the loop fails
loudly rather than acting as anyone else.

To check when a token expires, ask GitHub rather than trusting a note:

```bash
GH_TOKEN=$(cat ~/.hermes/keys/rev-bot-pat) gh api -i / | grep -i github-authentication-token-expiration
```

This passes the token through an environment variable, not the command line, where `ps` could
read it.

## 8. Least privilege: what a leaked token could do

| token | if it leaked, the holder could |
| --- | --- |
| reader, fine-grained read-only | read `owner/name`. With `repository_hooks: write`, also change or delete its webhooks |
| reviewer, fixer or adjudicator login, classic `repo` | do anything that account can do on every repo it can reach, including pushing code |

That is why:

* each seat account should be a dedicated account that is a collaborator on this repo only;
* each login has its own file, so a leak or a rotation touches one role;
* the reader, where the platform allows it, holds a narrow fine-grained token.

Scope alone cannot make a reviewer or adjudicator token safe on a user-owned repo: an account that
can post a review can also push. What keeps a seat's token away from the agents is the
[isolation boundary](security.md): **seat tokens never enter the sandbox**. The agent runs
in a bubblewrap sandbox with no GitHub credential; the broker holds the token files on the host and
makes each write itself after checking the live PR and the four distinct identities. `selftest`
step 2 checks that the sandbox cannot read the PAT files.

Moving the repo into an organization narrows every role to fine-grained permissions (the org
column in section 3), which is the practical way to tighten this further.
