# Desktop settings reference

The form at **Capabilities → Plugins → diaktoros** holds plugin-level defaults in the
Hermes profile that saved them. It is not a live editor for every repository. A loop's
JSON file remains its effective configuration; changes reach an existing loop only through
an explicit `apply --loop "<loop-id>"`.

## Contents

- [The three configuration surfaces](#the-three-configuration-surfaces)
- [Every form field](#every-form-field)
- [Defaults, blanks and coercion](#defaults-blanks-and-coercion)
- [Concurrency inheritance](#concurrency-inheritance)
- [Identity and token-file validation](#identity-and-token-file-validation)
- [Preview and apply](#preview-and-apply)
- [Safety and implementation limits](#safety-and-implementation-limits)
- [Related documentation](#related-documentation)

## The three configuration surfaces

| Surface | Owns | How changes take effect |
| --- | --- | --- |
| Plugin settings form | Numeric defaults, gateway/clone defaults, working-seat identities and token paths, optional adjudicator identity, attribution | `init`/`setup` consume effective defaults for a new loop; `apply` overlays explicitly stored nonblank fields on one named loop. |
| Per-loop JSON | Repository, allowlists, routes, seats, consent policy, observer/triage and all effective loop settings | Use `set`, `triage`, `fixer-push`, or a carefully reviewed manual edit plus route reconciliation. See [Configuration](configuration.md). |
| Private runtime JSON | Host Hermes/Python/Rust paths and optional model overrides | `setup` detects/writes it; workers resolve models through the runtime. See [Configuration](configuration.md). |

The form and `SETTINGS_SCHEMA` in `diaktoros/config.py` mirror the 23 keys declared in
`plugin.yaml:config_schema`. The schema supplies types, labels and defaults, not a complete
validation contract. The CLI handlers validate the resulting loop before their own writes.

## Every form field

Blank string defaults below mean **not set here**, not a valid ready-to-run identity.
Defaults are for a fresh form; existing loops retain values when a field is absent/blank.

| Field | Type / fresh default | Accepted value | Destination and behavior |
| --- | --- | --- | --- |
| `cap` | int / `3` | Whole verdict count >= 2 | `cap`; at the cap, a configured adjudicator can rule. The cap counts verdicts, not pushes. |
| `reviewer_concurrency` | int / `1` | Whole number >= 1 | Effective reviewer capacity; see inheritance below. Above 1 requires a nonblank clone. |
| `fixer_concurrency` | int / `1` | Whole number >= 1 | Effective fixer capacity; above 1 requires a nonblank clone. Does not enable unattended pushes. |
| `clone` | str / `""` | Local clone path | `clone`; a blank form keeps the loop's clone. Actual checkout usability is a separate doctor/selftest check. |
| `base` | str / `main` | Branch string | `base`; gates use it to select PR targets. The loader does not enforce full Git ref syntax. |
| `grace_min` | int / `35` | Integer minutes; use positive values | `grace_min`; watchdog quiet-head grace is at least the relevant seat's full worst-case turn. No explicit positive range is enforced here. |
| `ttl_min` | int / `45` | Integer minutes; use positive values | `ttl_min`; a seat claim survives at least its whole turn, including recorded budget. No explicit positive range is enforced here. |
| `inflight_ttl_min` | int / `10` | Integer minutes; use positive values | `inflight_ttl_min`; age limit for duplicate-head in-flight marks. No explicit positive range is enforced here. |
| `turn_budget_s` | int / `900` | Seconds 60–14400 | `turn_budget_s`; wall-clock model turn budget. Existing per-seat overrides win, and queued runs keep recorded budgets. |
| `host` | str / `""` | HTTP(S) gateway origin, optional port, no path/query/fragment/userinfo | `host`; required for route installation. HTTPS recommended. Blank does not erase an existing origin. Applying a changed origin can move remote hooks and is refused while observer notices are unsettled. |
| `reviewer_profile` | str / `""` | Existing Hermes profile | `seats.reviewer.profile`; a blank field keeps it. Must use a different profile home from the fixer. |
| `fixer_profile` | str / `""` | Existing Hermes profile | `seats.fixer.profile`; blank keeps it. Must use a different profile home from reviewer. |
| `reviewer_login` | str / `""` | GitHub login in this loop's reviewers allowlist | `seats.reviewer.login` and `reviewer_seat` together. Does not add the login to the allowlist. |
| `fixer_login` | str / `""` | GitHub login in this loop's fixers allowlist | `seats.fixer.login`; does not add the login to the allowlist. On init a saved fixer login must be eligible even if explicit fixer flags were passed. |
| `adjudicator_profile` | str / `""` | Existing Hermes profile distinct from the working-seat homes | `adjudicator.profile`, only if the loop already names an adjudicator route. Does not enable adjudication by itself. |
| `reviewer_token_file` | str / `""` | Owned private regular file; absolute path or `~` path | Maps the effective reviewer login into `tokens`. This is a path, never a PAT value. Blank keeps the mapping. |
| `fixer_token_file` | str / `""` | Owned private regular file; absolute path or `~` path | Maps the effective fixer login into `tokens`; path only, blank keeps mapping. |
| `adjudicator_login` | str / `""` | Optional fourth GitHub login distinct from reader, seats and their allowlists | `seats.adjudicator.login`, only with an adjudicator route; adds a PR comment identity for rulings. Without it rulings remain operator/ledger output. |
| `adjudicator_token_file` | str / `""` | Owned private regular file; absolute path or `~` path, not shared with another login | Maps the optional adjudicator comment login into `tokens`, only with an adjudicator route. Needs an effective adjudicator login. |
| `review_after_ci` | bool / `false` | Boolean; CLI reader also recognizes boolean words | `review_after_ci`: a review waits while the head's checks are still running (up to an hour), then starts with their results. |
| `review_only` | str / `""` | Comma-separated GitHub logins; at most 50 unique, none a fixer or a reviewer | `review_only`: their PRs are reviewed, never fixed; a verdict goes back to the author, with no adjudication. Verdicts are capped by `review_only_cap`. |
| `review_only_cap` | str / `""` | Whole number 1-1000, or blank | `review_only_cap`; verdicts the reviewer gives one review-only PR before it waits for `review --another-round`. Blank = the loop's `cap`. |
| `review_only_daily` | str / `""` | Whole number 1-1000, or blank | `review_only_daily`; reviewer turns per local day on review-only PRs. Blank = no cap. |
| `required_checks` | str / `""` | Comma-separated check names (a comma inside parentheses stays in the name); at most 50 unique names | `required_checks`: only these gate an approval and the CI hold; others are optional. A required check that never reports at the head is refused on approval and holds the review (up to an hour) whatever `review_after_ci` is. |
| `fixer_check` | str / `""` | One printable line, at most 500 characters | `fixer_check`: one command the fixer and issue-fix turns run before publishing, besides the tests they touched. Blank keeps the loop's value; `set --fixer-check ''` clears it. |
| `reviewer_max_steps` | str / `""` | Whole number 8-200, or blank | `seats.reviewer.max_steps`; agent steps one reviewer turn may take. `init`/`setup` start a new loop from it (`--reviewer-max-steps` overrides). Blank = not set here: the loop keeps its own value or the role default. |
| `fixer_max_steps` | str / `""` | Whole number 8-200, or blank | `seats.fixer.max_steps`; agent steps one fixer turn (and an issue fix) may take. `init`/`setup` start a new loop from it (`--fixer-max-steps` overrides). Blank = not set here. |
| `fix_daily_turns` | str / `""` | Whole number 1-1000, or blank | `triage.fix_daily_turns`; issue-fix turns per local day. Needs the loop's `triage.fix_label`, else apply is refused. A new loop has no fix label, so `init` notes the value and does not write it; `apply` or `triage --fix-daily-turns` lands it. Blank = not set here. |
| `attribution` | bool / `true` | Boolean; CLI reader also recognizes boolean words | `attribution`; signs plugin-mediated reviews/comments and commit trailers. See the attribution-only apply limitation below. |

The form deliberately has **no reader login/token field**, no observer destination, no triage,
no allowlist editor, no route-name editor, no per-seat model picker and no unattended-push
consent field. Supply the reader at installation or with `set --read-token`, configure observer
and triage through their documented commands, and enable push policy only with `fixer-push`.
Change a profile model through Hermes, not through this form. Runtime overrides are separate.

## Defaults, blanks and coercion

For **new loops**, `settings_defaults` substitutes schema defaults for `None`/empty strings.
It converts integer settings with `int(...)`, string settings with `str(...)`, and falls back
to the default when conversion raises `TypeError`/`ValueError`. These are reader behaviors,
not assurances that arbitrary malformed data is harmless: whitespace strings and some
non-finite numeric values can still fail later validation. Supply properly typed fields.

For **existing loops**, `apply_settings` tests the raw form first: absent, `None`, empty or
whitespace-only fields are not set here and do not reset a loop. This rule includes numeric
knobs, identity strings and attribution. Explicit `false` and zero are supplied values, not
blanks; a supplied zero cap/concurrency/budget is refused, not interpreted as omission.
An empty form preserves a loop's own nondefault settings.

The boolean reader recognizes actual booleans and case-insensitive `true/on/yes/1` or
`false/off/no/0`; other values fall back to schema default. Prefer the form's boolean control.
The `set --attribution` CLI accepts only `on` or `off`.

`settings` marks stored values `[set]` and others `[default]`. A display label is not proof
that a field is effective: whitespace-only raw entries can look set yet be ignored by apply.
Check the proposed diff and then the named loop's `status`.

## Concurrency inheritance

Capacity is per seat, not one shared reviewer-plus-fixer pool:

- If **both form concurrency fields are supplied and equal**, apply writes that value as the
  loop-level `concurrency` and removes working-seat pins that agree with it.
- If **both are supplied but differ**, the loop default becomes 1 and each differing seat
  gets its own explicit override.
- If **only one is supplied**, only that seat gets a pin; the loop default and other seat
  remain unchanged.
- If **neither is supplied**, loop default and pins remain unchanged.

`set --concurrency` subsequently affects only unpinned working seats. A form that explicitly
owns both seat values can reconcile their pins again. Adjudicator/triage do not inherit the
working-seat concurrency default. Concurrency above 1 requires a clone for reviewer/fixer
at normalization, even though production turns are sandbox-isolated at concurrency 1 too.

`turn_budget_s` is a loop default, not a reset of `seats.<seat>.turn_budget_s`. Form apply does
not clear per-seat turn budgets or daily caps. Queued and running ledger rows retain the
budget they were given; a later CLI retry records the current seat budget.

## Identity and token-file validation

Profile names start alphanumeric and contain letters, digits, `.`, `_` or `-`. Named
profiles must exist with a nonempty `config.yaml`; symlinked profile directories are refused.
Reviewer/fixer profile homes and logins must differ. An adjudicator profile must be independent
of both seats when checked for a moved role.

Changing a login does **not** change repository allowlists. The reviewer login must already
be in `reviewers`, the fixer in `fixers`. The form moves `reviewer_seat` with the reviewer login
so the route and counted verdict selector remain aligned. Display names already supplied by
the loop are preserved rather than treated as identities.

All nonblank token-file fields are checked before init/apply computation, even if a particular
adjudicator setting would otherwise not land. Metadata validation expands `~`, requires an
absolute path, follows symlinks, and rejects missing files, directories, foreign ownership
and **any** group/other permission bit. A mode-0600 file is the recommended shape; the rule is
not exactly “must equal 0600.” It does not open the file during this metadata check.

Subsequent credential-reference validation **does read files** to establish nonempty readable
content; never assume init/apply are credential-free simply because the first check uses stat.
Tokens are not printed. Live `/user` checks belong to selftest and the write broker: distinct
file paths and claimed login strings do not prove distinct actual GitHub principals.
See [Accounts](accounts.md) and [Security](security.md).

A token-file change is an identity change for busy-seat checking, just like a profile/login move.
A form token path without an effective login is refused. Adjudicator profile/login/token fields
land only with an existing adjudicator route; no adjudicator route is created from these fields.
Blank cannot clear a saved identity: for the optional comment login, `set --adjudicator-login ""`
is the explicit clearing path.

## Preview and apply

Replace the quoted placeholders with actual values; quoting only protects shell syntax.

```bash
hermes dk settings
hermes dk apply --loop "<loop-id>" --dry-run
hermes dk apply --loop "<loop-id>"
hermes dk status --loop "<loop-id>"
hermes dk doctor --loop "<loop-id>"
```

- `settings` takes no plugin flags and displays current-profile defaults plus current loops.
- `--loop` selects exactly one existing loop; it is required for apply.
- `--dry-run` previews form/route changes without requested writes. Hook listings may be read;
  the live busy-seat check happens after the dry-run path, so preview is not approval to rebind.
- Omitting dry-run performs the apply. Status reads effective configuration and actual route
  profile side by side; doctor checks installation health, including routes/hooks/model setup.

Identity/route reconciliation stages owned route updates and hook URL moves before publishing
the changed loop config. Route/hook readback must agree. Failures attempt rollback and report
any rollback failure. Never treat this as a single atomic filesystem-plus-GitHub transaction:
shims, explicit missing-route recreation and optional hook/cron repairs have separate steps.

If a role being rebound has a live turn, normal apply refuses. The override is explicit:

```bash
hermes dk apply --loop "<loop-id>" --while-busy --admin-token "<hook-admin-login>"
```

Here `--while-busy` permits the rebind while the live turn retains its starting identity;
`--admin-token` selects a previously mapped account with hook write access for remote moves
(the default is the reader). This is not a kill/restart command and does not convert an old
run into the newly selected identity. Prefer waiting for the turn to finish.

Apply also reconciles drifted plugin-owned route profiles/contracts and older adjudicator gate
scripts. If routes are missing, try intent-based `doctor --repair` first. Explicit extras:

```bash
hermes dk apply --loop "<loop-id>" --recreate-routes --hooks --watchdog-shim --admin-token "<hook-admin-login>" --dry-run
```

- `--recreate-routes` writes missing routes with new secrets from loop config and re-keys owned
  hooks; use only when the original intent cannot restore the route/secret.
- `--hooks` reconciles owned repository hooks, including enabled triage; missing hooks start paused.
- `--watchdog-shim` rewrites the shared cron entry script pinned to this plugin's watchdog.
- The admin account authorizes hook changes; dry-run previews without requested writes.

These extras do not come from form fields and do not edit other loops' repository configs.
The watchdog and some profile shims are shared infrastructure; see [Architecture](architecture.md).
Full options and exit codes: [Command reference](commands.md).

## Safety and implementation limits

- **Attribution-only apply gap:** `apply_settings` produces a changed attribution value, but
  CLI `_apply` does not include it in its change list. A form change affecting attribution
  alone can report “already matches” without saving it. Use this direct path and verify status:

  ```bash
  hermes dk set --loop "<loop-id>" --attribution off
  ```

  `--loop` names the repository loop; `--attribution off` disables its plugin-added signatures.
  Use `on` to restore them. A change accompanied by another persisted form change may carry
  attribution too; do not rely on that incidental behavior.
- **No implicit push consent.** Arming hooks, selecting a fixer or increasing concurrency never
  enables unattended pushes. `fixer-push --enable` requires its separate race acknowledgement.
- **Selective validation.** Apply checks moved/rebound roles; an unchanged legacy profile may
  keep loading. A successful apply is not a full installation health report. Run doctor/selftest.
- **Not all previews are offline.** Dry-run apply can read remote hooks. Doctor's `--offline`
  skips its named network probes; selftest may resolve/refresh credentials and call providers.
- **Privacy and recovery.** Token paths belong in the form, never PAT strings. Do not copy
  runtime credentials into repository JSON. Origin/destination changes cannot forward unsettled
  observer outbox entries to another chat; reconcile notices first.
- **Existing data survives blanks.** Clearing a form field does not erase its effective loop
  identity, clone, origin or nondefault number. Use the documented loop-specific clearing paths,
  or a reviewed manual edit for fields without a CLI deletion flag.

## Related documentation

[Getting started](getting-started.md) · [Concepts](concepts.md) · [Commands](commands.md) ·
[Configuration](configuration.md) · [Accounts](accounts.md) · [Operations](operations.md) ·
[Security](security.md) · [Troubleshooting](troubleshooting.md) · [Observer](observer.md) ·
[Issues](issues.md) · [Architecture](architecture.md) · [Development](development.md)
