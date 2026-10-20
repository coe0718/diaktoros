# Desktop read diagnostics

The page's Failed/Retry state is not evidence that a particular panel, store or route is the
cause. Distinguish transport failures, rejected payloads, missing prerequisites and actual
unavailable local stores before changing an installation.

## REST contract

`ctx.rest('/stats?loop=widgets')` resolves to the **decoded JSON value**, not a Fetch Response.
The host SDK scopes this to `/api/plugins/diaktoros/stats?loop=widgets`, preserving the query
string, and chooses the active profile/connection. Its options are `method`, `body`, `upload`
and `timeoutMs`; there is no separate query-parameter option. Do not call `.json()` or test
`.ok` on a successful value.

The inspected host transport attaches HTTP failure status as `statusCode`. Raw error messages
can contain response bodies and private transport details: classify supported status values
into fixed public messages instead of rendering messages or backend details. An absent HTTP
status is not proof of a missing route. A 200 HTML SPA response is also not a successful JSON
read. The source SDK contract does not prove which Electron bundle an already-running window
loaded.

The current plugin server query keys include loop/window where applicable but not the host's
active profile/connection. REST routing is profile-aware. Matching loop ids on different hosts
are not the same resource; profile/connection transitions need cache scoping or explicit server
query invalidation. This is a source-contract risk, not a reproduced cause of the reported
failures.

## Automatic health read: network, security and cache

Selecting a loop on the Desktop page now sends one **GitHub-probed** health read automatically:
`GET /health?loop=<id>&probe=1`, the same full set `hermes dk doctor` reads. This is an explicit
product decision — the operator asked for the panel to load its GitHub state without clicking
Probe GitHub — and it is disclosed in the panel: it reads the loop's own tokens from this machine
and makes bounded GitHub reads. It never writes, never creates a PR or mutation, and returns only
the doctor checks (names, statuses, bounded detail/fix lines and counts). Tokens and credential
values are not part of the response; raw transport text is not rendered.

The read is shared by every observer of the same loop and cached for ten minutes
(`staleTime`). It has `retry: false`, no `refetchInterval`, no reconnect refetch, and it is
excluded from the socket's invalidation namespace, so a 30-second local poll or an event hint
cannot turn into a repeated doctor sweep. Switching loops binds a different stable key
(`[diaktoros, health, <loop>, probe]`); a failed read keeps its last-good data and stays
recoverable through the panel's Refresh GitHub or its status Retry, which re-request that exact
loop. A first-load failure renders unavailable with a bounded error category — never an empty
healthy install. Before caching, the response is rejected unless its `loop` matches the selection,
every check has a known status plus string detail/fix/paused fields, `read_at` is valid, and the
counts equal the checks actually returned.

The page does not currently re-scope server query keys by active profile/connection, so a
profile switch can show another profile's cached health until it is refetched. This is a known
unsupported mode, as is automatic probing when the loop is selected on a remote/OAuth connection:
the route refuses a non-local probe (403) and the panel then shows unavailable rather than
pretending the read happened. Health is not polled at all; there is no background refresh after
the ten-minute freshness window (the panel marks it stale and offers Refresh GitHub).

## Verified backend behavior

`tests/test_desktop_reads.py` exercises real normalized loop files and a host-initialized SQLite
schema through ASGI. It also starts an isolated real Hermes application in a child process,
mounts the enabled plugin through the host's own discovery, and makes authenticated reads using
that fixture application's generated session token. It does not contact the running dashboard,
read its credentials, restart it, or initialize the operator's gateway. The child application's
own session-store prerequisite can remain unavailable without preventing these plugin reads.

The initialized fixture returned JSON 200 for loops, stats, health, settings, records, hooks and
overview. Offline health reports missing prerequisites as checks. A hook read with an absent
fixture credential returns an unknown/unavailable hook plan, not an HTTP failure. These results
rule out an unconditional exception in those collectors for a valid initialized fixture; they
do not establish that the operator's live stores or loaded process are healthy.

A **real corrupt SQLite file** reproduced a plaintext HTTP 500 from stats before the fix.
Injected store/runtime exceptions reproduced the same unbounded read-failure boundary across
all read routes. Reads now return HTTP 503 with only:

```json
{"detail":"Desktop read unavailable; retry the read."}
```

This is an unavailable read, never a verified empty result. Existing expected 400/403/404/409/422
refusals retain their status. Overview retains its per-loop error isolation when one ledger read
fails. Confirmed write routes retain their separate exception mapping: a post-write exception
cannot be described as an ordinary safe-to-retry read.

## Deployment evidence versus diagnosis

Read-only local metadata during investigation showed the dashboard running since October 18,
2026 at 16:48:03 EDT, while the installed Diaktoros checkout was `425b6441d905a191429b12cba8e9f51dd2b4b5f0`.
That makes stale imported backend code a candidate, **not a verified route-registration failure**.
The inspected current host source includes a hot-mount fallback for previously unmounted plugin
APIs; a mounted router's new subpath and the code loaded by an older process are different cases.
Do not infer route registration from an unauthenticated request, or from the checkout's SHA alone.
No live authenticated route read or deployment was performed by this investigation.

## Reproduce safely

Use the scratch-only, read-only-host wrapper described by the console skill's local-verification
reference. Keep fixture homes, caches and both temp fallbacks physically scratch-backed; never
weaken the real-home guard to admit a worktree underneath the operator's home. Network isolation
prevents a fixture from reaching GitHub. Required commands inside that wrapper are:

```sh
python3 tests/leakguard.py -v test_home_guard test_harness_doctor_fixture
python3 tests/leakguard.py -v test_desktop_reads test_desktop test_desktop_settings \
  test_desktop_records test_desktop_hooks test_desktop_actions
```

Web dependencies are optional only in standalone shards. Supply FastAPI, HTTPX and Hermes and
set `DIAKTOROS_REQUIRE_HERMES_SOURCE=1` in the dependency-equipped integration lane; a missing
prerequisite must fail there rather than turn into a green skipped acceptance. The real-host
case must be included explicitly in that lane when adding it to CI. Full-suite and real-render
acceptance remain separate from these targeted backend tests.
