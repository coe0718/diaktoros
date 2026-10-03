# Documentation

Start with the [project README](../README.md): what the loop is, what you need, and the install.

**New here? Read in this order:**

1. [concepts.md](concepts.md): how the loop works in plain words, and what every term means.
2. [accounts.md](accounts.md): the GitHub accounts and tokens, step by step.
3. The [install and first run](../README.md#install) in the README.
4. [commands.md](commands.md): every command, what it changes, and every flag.
5. [troubleshooting.md](troubleshooting.md): when something doesn't happen, by symptom.

| page | what it covers |
|---|---|
| [concepts.md](concepts.md) | how it works: a glossary, one PR from start to finish, the switches, what the agents can and cannot do, where things live |
| [accounts.md](accounts.md) | why several GitHub accounts, creating them and their tokens, storing tokens safely, checking them |
| [commands.md](commands.md) | every `hermes review-loop` command, what it changes, and its flags (generated from the CLI) |
| [troubleshooting.md](troubleshooting.md) | symptom → cause → fix: nothing happened, held turns, failed or uncertain runs, the runtime file, notices |
| [operations.md](operations.md) | what `init` writes, everyday commands, the `doctor` preflight, `selftest`, `explain`, `trace`, pacing, burst handling |
| [settings.md](settings.md) | the desktop settings form, seat identity defaults, `settings` / `apply` |
| [observer.md](observer.md) | the observer feed: notices to your phone, how to turn it on, its rules |
| [configuration.md](configuration.md) | reference for every loop-config key, the observer block, adjudication, plugin settings, seat identity, state files, environment overrides |
| [architecture.md](architecture.md) | design: the seats, isolation, escalation, the watchdog, `explain`, the observer feed, preflight |
| [issue-16-boundary.md](issue-16-boundary.md) | the isolated route-to-agent boundary (issue #16), selftest no-write guarantees, remaining blockers |
| [issue-1-route-self-heal.md](issue-1-route-self-heal.md) | the webhook-registry race (issue #1): intent record, self-heal, conflict-checked writes, the remaining window |
| [stacked-submission-boundary.md](stacked-submission-boundary.md) | the stacked reviewer submission boundary (not enabled) |
| [directsdk-validation.md](directsdk-validation.md) | validating the experimental Claude-subscription (DirectSDK) backend |
