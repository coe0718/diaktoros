# Getting started

[Documentation map](README.md) · Previous: [Accounts](accounts.md) · Next: [Operations](operations.md)

This guide installs one loop for one repository, verifies isolation before production writes and leaves unattended fixer pushes **off**. Read [concepts](concepts.md) first if seats, routes or hooks are unfamiliar.

## Prepare the host and identities

| Prerequisite | What to prepare | Why it matters |
| --- | --- | --- |
| Linux | Install `bwrap` from your distribution and enable working unprivileged user namespaces | Every seat turn uses bubblewrap; native macOS is unsupported. A Linux VM must run both Hermes and the plugin, not just hold the repository |
| Hermes | Manifest requires >=0.21.5; keep a Git checkout with `run_agent.py`, `.git`, and a venv containing `bin/hermes` and `bin/python` | The isolated runtime mounts a filtered Hermes source snapshot and the environment; a desktop installation alone is not proof these paths exist |
| Python runtime | Directory containing the venv interpreter's target and resolved installation | Mounting only the venv can leave its Python symlink broken |
| Rust | Actual toolchain directory with `bin/cargo` | Runtime verification expects it even if your first PR is not Rust; the rustup proxy directory is not the toolchain |
| Gateway | Your public HTTPS origin, with webhook reception working | GitHub must reach your Hermes gateway; there is no shared host |
| Profiles | Two existing, distinct Hermes profiles and homes, each with a configured provider/model | GitHub login and Hermes profile are different identities; model auth stays on the host |
| GitHub | Distinct reader, reviewer and fixer accounts/files; accepted access invitations | See [accounts](accounts.md). The repo owner commonly serves as reader and hook admin |
| Hook administration | Token able to list/create/update/delete repository hooks | Reader hook read is needed to determine armed/paused state. Hook write is needed only for the administrator |

Use the [Hermes documentation](https://hermes-agent.nousresearch.com/docs) for Hermes installation, profile creation, provider login and gateway deployment. Configure each seat's model in its profile:

```bash
hermes -p "<reviewer-profile>" model
hermes -p "<fixer-profile>" model
```

`-p` selects the named existing Hermes profile; `model` selects its provider/model. Different profiles are mandatory; different models are useful but not an identity-validation requirement. A subscription may still use the same underlying account in two profiles. Experimental `claude-subscription-directsdk-experimental` uses native authentication on the host; separate native accounts need explicit host-side config directories, not merely separate profile names. See [configuration](configuration.md) and [security](security.md).

## Example conventions

Replace every quoted `"<…>"` value with your own value. `ID` stands for the loop id and `N` for a PR number. Use absolute token paths: quoted `"$HOME/…"` expands correctly; a quoted `"~/…"` is not shell-expanded, although the plugin expands token paths containing `~`.

Examples assume the operator/gateway Hermes home is `~/.hermes`. All commands must target that same home, not alternate between unrelated profiles. Runtime and loop files are relative to `HERMES_HOME`; supplying a seat profile does not move the operator's loop config there.

## Install the plugin

```bash
hermes plugins install diaktoros
```

This installs the plugin from the Hermes plugin catalog, at the commit the catalog reviewed and pinned; Desktop can install it from the catalog as well. It is the plugin, **not** the repository to review; reviewed-repository examples remain placeholders.

The install scanner rates the plugin **caution**, which a catalog install accepts without a prompt. To run the current `main` instead, install from the repository: `hermes plugins install coe0718/diaktoros`. That install prints the findings and asks `Install anyway? Only continue if you trust the source. [y/N]`. Typical findings concern subprocesses (`bwrap`, Git, workers and fixtures), security probes, and setup advice mentioning privileged commands; review the actual report rather than assuming a fixed finding count. Plugins execute trusted host code, so a scanner pass is not proof of safety.

## Choose one setup path

Do not use a real `init` run to change a loop already created by `setup`: it refuses an existing id. `init --dry-run` can still preview that id without writing. `setup` keeps existing configuration instead of applying new answers. Use [settings/apply](settings.md) and [commands](commands.md) for later changes.

### A. Guided setup (recommended)

```bash
hermes dk setup --repo "<owner>/<repository>"
```

`--repo` names the repository to review; setup asks for omitted logins, profiles, token files, gateway origin and delivery choices. Provide a hook-admin login if you want setup to create paused GitHub hooks. A blank admin answer leaves hooks for manual creation. If those hooks are absent, `doctor` fails and setup prints **stopped before arming**, exiting 1; this is expected until you create them manually and rerun verification. Decline the final arm prompt until you have finished verification.

Setup performs these steps in order:

1. Detect or keep valid runtime paths and write a private `diaktoros-runtime.json`.
2. Show an `init` dry run, confirm installation and create configuration/routes (and paused hooks if a hook admin was named).
3. Install the shared watchdog job if missing.
4. Run `doctor` and `selftest --no-model`. Failure stops before arming; already completed steps are kept.
5. Ask whether to arm. It defaults to **no**. Setup does not enable fixer pushes, adjudication or issue automation.

A repeatable, noninteractive example using the reader as hook admin:

```bash
hermes dk setup \
  --repo "<owner>/<repository>" --id ID \
  --reviewer "<reviewer-login>" --fixer "<fixer-login>" \
  --reviewer-profile "<reviewer-profile>" --fixer-profile "<fixer-profile>" \
  --reviewer-token "$HOME/.hermes/keys/<reviewer-login>-pat" \
  --fixer-token "$HOME/.hermes/keys/<fixer-login>-pat" \
  --read-token "<reader-login>" \
  --read-token-file "$HOME/.hermes/keys/<reader-login>-pat" \
  --admin-token "<reader-login>" \
  --host "https://<gateway-host>" \
  --schedule 15m --watchdog-deliver local \
  --yes --dry-run
```

| Option in this example | Meaning / effect |
| --- | --- |
| `--repo` | Exact owner/repository to monitor |
| `--id ID` | Explicit loop identifier; omitted means repository name. Choose a unique filename-safe id |
| `--reviewer`, `--fixer` | GitHub seat logins; become trusted allowlists for this basic setup |
| `--reviewer-profile`, `--fixer-profile` | Existing distinct Hermes profiles used for inference and prompts |
| `--reviewer-token`, `--fixer-token` | Seat PAT file paths, never token values |
| `--read-token` | Third account used for trusted GitHub reads, not a token string |
| `--read-token-file` | Reader's own PAT file path |
| `--admin-token` | Login that creates paused hooks. Here it reuses the reader mapping, so that PAT needs hook write |
| `--host` | Your gateway origin, without path, credentials, query or fragment; HTTPS for public GitHub |
| `--schedule 15m` | Shared watchdog interval; it checks stalls and drains eligible work without a model |
| `--watchdog-deliver local` | Cron output stays local. Use a configured Hermes destination when you want remote alerts |
| `--yes` | Answers from flags/settings without prompts; does **not** arm without `--arm` |
| `--dry-run` | Preview runtime/configuration steps without writing. Token-file validation still requires prepared files |

Remove **only `--dry-run`** to install the previewed configuration, still paused. A separate hook administrator uses `--admin-token "<admin-login>" --admin-token-file "<absolute-admin-token-path>"`; the file option supplies a mapping only when the admin is not already reader/reviewer/fixer. Omit `--arm` until checks pass.

If detection fails, supply the relevant `--source`, `--venv`, `--runtime` or `--rust` path. Their meanings are in the runtime table below. Setup keeps valid existing paths and model overrides; it cannot fix malformed overrides silently. A failed runtime detection can leave later setup steps installed, but will not arm.

### B. Explicit init (for controlled wiring)

`init` writes loop configuration and routes; it **does not create the runtime file**. This example uses a local clone and two reviewer slots, with paused hooks:

```bash
hermes dk init \
  --repo "<owner>/<repository>" --id ID --base main \
  --reviewer "<reviewer-login>" --fixer "<fixer-login>" \
  --reviewer-profile "<reviewer-profile>" --fixer-profile "<fixer-profile>" \
  --read-token "<reader-login>" \
  --token "<reader-login>=$HOME/.hermes/keys/<reader-login>-pat" \
  --token "<reviewer-login>=$HOME/.hermes/keys/<reviewer-login>-pat" \
  --token "<fixer-login>=$HOME/.hermes/keys/<fixer-login>-pat" \
  --cap 3 --reviewer-concurrency 2 --fixer-concurrency 1 \
  --clone "<absolute-clone-path>" --root "<dedicated-review-artifacts-root>" \
  --turn-budget 900 \
  --host "https://<gateway-host>" \
  --hooks --admin-token "<reader-login>" \
  --schedule 15m --watchdog-deliver local \
  --dry-run
```

| Option | Meaning / constraint |
| --- | --- |
| `--repo`, `--id` | Repository and loop id, as above |
| `--base main` | Only PRs against this base branch qualify; replace `main` if needed |
| `--reviewer`, `--fixer` | Repeatable trusted GitHub login allowlists. With several reviewers, name `--reviewer-seat "<reviewer-login>"`; it selects the reviewer route's identity |
| `--reviewer-profile`, `--fixer-profile` | Profiles, not GitHub accounts; must exist and use distinct homes |
| `--read-token` | Explicit reader login; never inferred from the first token mapping |
| Repeated `--token` | Each `LOGIN=PATH` maps that login to its own file. Quoting the entire argument preserves it as one shell word |
| `--cap 3` | Changes-requested verdict budget: up to three such verdicts, with at most two intervening fix turns; minimum 2 |
| `--reviewer-concurrency 2` | At most two concurrent reviewer PRs; requires a local clone |
| `--fixer-concurrency 1` | At most one fixer PR at once; no fixer runs while pushes remain off |
| `--clone` | Existing local repository used by trusted snapshot/fetch operations; turns get their own exact-head exports |
| `--root` | Repeatable **dedicated** cleanup root. PR-named children may be deleted on close/merge; never use your home, filesystem root or general project folder |
| `--turn-budget 900` | Wall-clock seconds per turn, builds/tests included; allowed range 60–14400. Per-seat overrides are available in the command reference |
| `--host` | Gateway origin as above; required even without `--hooks`, since routes are installed |
| `--hooks` | Create the reviewer/fixer repo hooks **paused**; omitted means create/toggle hooks yourself |
| `--admin-token` | Mapped login authorized to manage hooks; default hook editor is reader |
| `--schedule`, `--watchdog-deliver` | Install shared watchdog with this interval/output destination; no schedule means init creates no cron job |
| `--dry-run` | Validate and preview, write nothing. Does not prove token ownership or live API permissions |

Remove `--dry-run` after inspecting the preview. Explicit flags take precedence over per-profile plugin defaults, but invalid token-path defaults are still validated. `init --arm` exists but bypasses this guide's paused verification sequence; do not use it for first installation.

For manual runtime configuration, create the following JSON at `$HERMES_HOME/diaktoros-runtime.json`, substituting real absolute directories (no shell-variable expansion inside JSON):

```json
{
  "source": "<absolute-hermes-checkout>",
  "venv": "<absolute-hermes-venv>",
  "runtime": "<absolute-python-runtime-root>",
  "rust": "<absolute-rust-toolchain>"
}
```

| Runtime key | Required directory |
| --- | --- |
| `source` | Hermes Git checkout containing `run_agent.py` and `.git` |
| `venv` | Environment containing `bin/hermes` and `bin/python` |
| `runtime` | Python installation root covering both literal and resolved interpreter link targets |
| `rust` | Actual toolchain containing `bin/cargo`, not `~/.cargo/bin` rustup proxies |

Create/edit this file with a private umask and enforce mode 600. Keep models in the profiles for the basic setup; do not paste secrets into runtime JSON. [Runtime and seat-model reference](configuration.md) covers overrides. Alternatively, run `setup` for the already-created loop: it keeps that loop and detects/writes missing runtime paths.

## Verify and arm

```bash
hermes dk doctor --loop ID
hermes dk selftest --loop ID --no-model
hermes dk selftest --loop ID --pr N
hermes dk selftest --loop ID --pr N --live-turn
hermes dk arm --loop ID --admin-token "<reader-login>"
hermes dk status --loop ID
```

| Command / option | What it proves or changes |
| --- | --- |
| `doctor --loop ID` | Checks the selected loop's profiles/models, token files, routes, hooks, clone and watchdog wiring. File checks do not authenticate every account |
| `selftest --no-model` | Runtime/sandbox and GitHub identity/access preflight; skips model requests, **not network reads**. May create local verification/ledger state |
| `--pr N` | Adds read-only reviewer authorization and PR build/dependency checks. The model probe runs whenever `--no-model` is absent, with or without `--pr` |
| `--live-turn --pr N` | Runs a real isolated reviewer/model turn; prints its proposed verdict, never posts it. Spends quota and can run build/test code in isolation |
| `arm --loop ID` | Activates matching existing hooks and sends each a GitHub ping (a write); exits 1 if a ping is rejected. Does not create missing hooks or enable fixer pushes |
| `--admin-token` on `arm` | Hook editor login mapped in loop tokens; omit only if the reader already has hook write |
| `status --loop ID` | Read back seats, route mapping, policy, live/queued runs and holds |

Use an **open, non-draft, same-repository PR** authored by a fixer-allowlisted login and targeting the configured base. Review every failed or unknown check rather than counting green lines. `doctor --offline` skips network probes and therefore cannot prove public delivery. `selftest --ping` explicitly requests a GitHub hook-ping write; ordinary selftest probes do not post a review. `arm` also pings hooks, so the full sequence above is not write-free. See [operations](operations.md) for individual steps and [troubleshooting](troubleshooting.md) for failures.

## Exercise the first production review

After arming, open/ready/reopen an eligible PR, or explicitly request the configured reviewer from an allowlisted fixer/reviewer account. Arming alone does not replay GitHub's historical webhook deliveries. A bare push (`synchronize`) does not request the next review.

```bash
hermes dk explain --loop ID --pr N
hermes dk status --loop ID
```

Both commands inspect the selected loop; `--pr` chooses the exact PR. Verify the verdict **on GitHub at the expected commit**, not merely a successful webhook response. `[SILENT]` / an ignored-script response prevents gateway fallback and can describe either a declined event or a queued turn. `trace` explains a delivery; [operations](operations.md) explains monitoring and recovery.

An approval leaves merging to you. A changes-requested verdict below the cap is held for your decision while fixer pushes are off. You can fix manually, verify the resulting ref/PR, and start a fresh review: request the reviewer seat as a loop maintainer (`triage.maintainers`), or toggle the PR to draft and back. A review request from an account that is not a fixer, the reviewer or a maintainer is ignored.

## Decide separately about unattended pushes

Only after reading [security and the PR race](security.md), opt in if appropriate:

```bash
hermes dk fixer-push --loop ID --enable --acknowledge-pr-race
hermes dk status --loop ID
```

`--enable` authorizes unattended fixer pushes for this loop; `--acknowledge-pr-race` records the host operator's explicit risk decision, **not GitHub owner consent**. The second command reads the policy back. GitHub PR state and a Git ref update are not one atomic operation; readback can detect some post-push changes but cannot undo publication.

To disable future fixer pushes use `fixer-push --loop ID --disable`; to pause hook-driven work use `arm --loop ID --pause` (with the appropriate admin login). Neither is a claim that a running external write was undone. Consult [operations](operations.md) before changing a loop with work in flight.

## Next steps

- [Accounts](accounts.md): rotate a PAT without changing the file path.
- [Settings](settings.md): change identities/models/defaults without reinstalling.
- [Configuration](configuration.md): add isolated adjudication (not enabled by setup).
- [Observer](observer.md): add delivery-only transition notices; no observer agent runs.
- [Issues](issues.md): opt-in triage and maintainer-triggered issue fixes, with extra scopes and policy.
- [Operations](operations.md): pacing, retries, uncertain outcomes and removal.
- [Commands](commands.md): exact full CLI syntax; [documentation map](README.md) for everything else.
