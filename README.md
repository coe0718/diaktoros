# hermes-review-loop

A Hermes plugin for bounded GitHub pull-request review: an isolated **reviewer** posts a verdict; an optional **fixer** answers changes; deterministic Python controls eligibility, capacity, retries and escalation. The loop never merges.

**Start here:** [Getting started](docs/getting-started.md) · [Documentation map](docs/README.md) · [Commands](docs/commands.md) · [Troubleshooting](docs/troubleshooting.md)

## Before you start

- Linux with working bubblewrap (`bwrap`) and unprivileged user namespaces. Native macOS turns are not supported; run Hermes and the plugin inside Linux instead.
- Hermes **>=0.21.5** (manifest requirement), a Hermes Git checkout and working virtualenv, Python runtime and Rust toolchain for the isolated runtime. A version number alone does not prove sandbox/provider compatibility: run `selftest`.
- Your own HTTPS-reachable Hermes gateway, and a repository whose hooks you can administer.
- Separate reviewer and fixer Hermes profiles; three distinct GitHub accounts and token files: reader, reviewer and fixer. An adjudicator comment account is optional and must be distinct too.

[Accounts and permissions](docs/accounts.md) explains which token each role needs. See the [Hermes documentation](https://hermes-agent.nousresearch.com/docs) for Hermes installation, profiles, model authentication and gateway setup.

## Install

Replace quoted angle-bracket placeholders before running examples. `ID` is the installed loop id; `N` is a PR number. No shared webhook host is provided.

```bash
hermes plugins install coe0718/hermes-review-loop
hermes review-loop setup --repo "<owner>/<repository>"
```

The plugin currently receives a **caution** scanner verdict. A terminal install shows the findings and asks `Install anyway? Only continue if you trust the source. [y/N]`. Review them before continuing: typical findings concern subprocess execution (`bwrap`, Git, workers and test fixtures), security-probe code, and setup advice mentioning privileged commands. A caution verdict is not a security audit; counts and classifications can change with the scanner version. **Install from a terminal until the plugin is in the reviewed catalog:** Desktop refuses caution-rated plugins from outside that catalog.

`setup` detects runtime paths, previews `init`, installs the requested wiring, schedules the watchdog and runs `doctor` plus `selftest --no-model`. It asks before arming; **decline arming until ready**. Name a hook admin to create paused hooks. If that answer is blank and hooks are absent, `doctor` fails and setup prints **stopped before arming**, exiting 1; that is expected until you create the hooks manually. Re-running setup on an existing loop keeps its saved settings; use `set` or `apply` to change them.

The [complete onboarding guide](docs/getting-started.md) includes a reproducible noninteractive setup and a flag-by-flag `init` alternative. To preview explicit wiring instead of the wizard:

```bash
hermes review-loop init \
  --repo "<owner>/<repository>" \
  --fixer "<fixer-login>" --reviewer "<reviewer-login>" \
  --fixer-profile "<fixer-profile>" --reviewer-profile "<reviewer-profile>" \
  --read-token "<reader-login>" \
  --token "<reader-login>=~/.hermes/keys/<reader-login>-pat" \
  --token "<reviewer-login>=~/.hermes/keys/<reviewer-login>-pat" \
  --token "<fixer-login>=~/.hermes/keys/<fixer-login>-pat" \
  --host "https://<gateway-host>" --dry-run
```

| Option | What it does |
| --- | --- |
| `--repo` | Repository to review; the repository component becomes the default loop id |
| `--fixer`, `--reviewer` | Distinct GitHub logins admitted for the two roles |
| `--fixer-profile`, `--reviewer-profile` | Existing, distinct Hermes profiles used to run the seats |
| `--read-token` | Third GitHub login for trusted reads, not the token itself |
| Repeated `--token` | `LOGIN=PATH` mappings to separate private PAT files; the plugin expands `~` |
| `--host` | Your gateway origin, without a path |
| `--dry-run` | Validate and preview without writing; does not prove live token permissions |

This minimal preview does not install repository hooks, a watchdog schedule or the runtime file. Use the [annotated complete installation](docs/getting-started.md#b-explicit-init-for-controlled-wiring) before expecting turns to run.

### First run, in order

For a loop already installed with paused hooks:

```bash
# setup writes $HERMES_HOME/review-loop-runtime.json; init alone does not.
hermes review-loop doctor --loop ID
hermes review-loop selftest --loop ID --no-model
hermes review-loop selftest --loop ID --pr N
hermes review-loop selftest --loop ID --pr N --live-turn
hermes review-loop arm --loop ID
```

`--no-model` skips inference, **not GitHub reads**. The next two selftests spend model quota; the live-turn verdict is printed, not posted. `N` must be an eligible open, non-draft, same-repository PR targeting the loop's base. `arm` activates existing matching hooks and sends a GitHub ping to every hook—a GitHub write—using the reader's token unless `--admin-token "<admin-login>"` is supplied. It exits 1 if a ping is rejected; inspect the output and hook state before continuing. Full checks and their limitations: [Verify and arm](docs/getting-started.md#verify-and-arm).

## Safety model

> With a valid private `review-loop-runtime.json` and hooks activated by `arm`, an eligible reviewer turn posts a real GitHub review. Gates answer `[SILENT]` so a normal credential-owning gateway agent does not handle the event. Agents run in bubblewrap without GitHub/model credentials or direct network access; host processes provide bounded inference and broker writes. Unattended fixer pushes stay **off** until the host operator explicitly runs `hermes review-loop fixer-push --loop ID --enable --acknowledge-pr-race`. A Git ref lease cannot atomically enforce GitHub PR metadata: the PR can close or retarget between the final API check and push. Read the [security boundary and push policy](docs/security.md) before opting in. No seat can merge; uncertain writes are not automatically replayed.

Isolation is not immunity: installed plugins are trusted host code, and a kernel/bubblewrap escape would run as the host user. Tests do not establish provider-policy approval or live credentials. Issue triage/fixing and desktop form rendering have offline coverage, not a claimed live acceptance test here. See [security](docs/security.md) and [development](docs/development.md).

## Advanced: Claude subscription seats (DirectSDK)

The experimental `claude-subscription-directsdk-experimental` provider runs its native client on the trusted host. Seat GitHub/model credentials still stay outside bubblewrap; model requests cross a bounded per-run inference capability. Native authentication defaults to the OS user's login: distinct Hermes profiles alone do not create separate subscriptions. Configure explicit host-side native account directories if different accounts are required. This integration is not provider-policy approval, and offline tests do not prove native prerequisites or a live completion. See [configuration](docs/configuration.md) and [security](docs/security.md) before selecting it.

## Run and extend

| Need | Read or run |
| --- | --- |
| Understand seats, rounds and handoffs | [Concepts](docs/concepts.md) |
| Inspect an installed loop | `hermes review-loop status --loop ID` |
| Explain one stuck PR without changing it | `hermes review-loop explain --loop ID --pr N` |
| Pause existing hooks | `hermes review-loop arm --loop ID --pause` |
| Add issue triage or maintainer-triggered fixes | [Issues](docs/issues.md) — opt-in |
| Receive transition notices without another agent | [Observer feed](docs/observer.md) — opt-in |
| Change defaults, identities or limits | [Settings](docs/settings.md) and [configuration](docs/configuration.md) |
| Operate, diagnose or contribute | [Operations](docs/operations.md), [troubleshooting](docs/troubleshooting.md), [development](docs/development.md) |

The watchdog reports stalls, drains eligible queued work and repairs owned routes; it is not an agent. One shared scheduled job sweeps configured loops. A paused loop does not drain or report ordinary stalls; an unreadable GitHub state is reported as unknown, not assumed safe.

## License

MIT. See [LICENSE](LICENSE).
