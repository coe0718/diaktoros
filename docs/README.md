# Documentation

[Project overview](../README.md) · [Start installation](getting-started.md) · [Find a command](commands.md) · [Fix a symptom](troubleshooting.md)

## First installation

1. [Concepts](concepts.md): roles, eligible events, review budget and safety switches.
2. [Accounts and tokens](accounts.md): prepare distinct accounts, repository access and private token files.
3. [Getting started](getting-started.md): prerequisites, installation, guided or explicit configuration, checks and first PR.
4. [Operations](operations.md): watch the loop, change it, diagnose failures and remove it.

## Choose by task

| Task | Page | Scope |
| --- | --- | --- |
| Install a first loop | [Getting started](getting-started.md) | Copyable examples, every example option explained, expected effects and verification |
| Understand the vocabulary | [Concepts](concepts.md) | Seats, profiles, hooks, gates, runtime, turns, handoffs and escalation |
| Provision or rotate credentials | [Accounts](accounts.md) | Distinct identities, token kinds/scopes, storage and verification |
| Look up an exact flag | [Commands](commands.md) | CLI reference; use for syntax, not as an installation tutorial |
| Edit defaults in the desktop or CLI | [Settings](settings.md) | Per-profile plugin defaults and staged `apply` |
| Inspect JSON and runtime settings | [Configuration](configuration.md) | Loop keys, runtime paths, models, state and environment overrides |
| Run a loop day to day | [Operations](operations.md) | Wiring, watchdog, preflight, selftest, explain, trace, pacing and recovery |
| Investigate a failure | [Troubleshooting](troubleshooting.md) | Symptom → cause → action |
| Add issue automation | [Issues](issues.md) | Opt-in triage and maintainer-label issue fixing |
| Add a notification feed | [Observer](observer.md) | Delivery-only notices, destination binding, batching and retries |
| Evaluate trust and push risks | [Security](security.md) | Host/sandbox/broker boundary and explicit unattended-push policy |
| Understand implementation | [Architecture](architecture.md) | Gates, ledgers, exact-head isolation, adjudication and reconciliation |
| Test or contribute | [Development](development.md) | Development workflow and verification |

## Conventions and boundaries

Shell examples quote angle-bracket placeholders so the shell does not interpret them as redirects. Replace them with your own values, keeping quotes. `ID` and `N` are generic loop-id and PR-number placeholders. Paths using `~/.hermes` assume the default Hermes home; select the operator/gateway home consistently and substitute its paths when using another profile or `HERMES_HOME`.

Fixer pushes are **off by default**. Arming hooks enables eligible reviewer turns; it does not authorize fixer pushes, triage or issue fixes. Commands that run inference can spend model quota. A no-model check is not an offline check, and an offline test result is not live acceptance evidence.

For Hermes itself, use the [authoritative Hermes documentation](https://hermes-agent.nousresearch.com/docs).
