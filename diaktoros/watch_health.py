"""The watchdog's GitHub health check: can the sweep read GitHub, as whom, for how long.

Moved from ``scripts/watchdog.py`` unchanged (#1349): the read alert on #54's cadence, the
token-expiry warning, a gate's last failed read, the budget-spent sweep, and settling it all
once the sweep's per-PR reads are in.
"""

from __future__ import annotations

import re
import time

from diaktoros import gate_failures, gh, watch_mode
from diaktoros import state as state_mod

# A 401/403 is a verdict about the token and alerts at once; a 5xx or no answer at all can be a
# blip, so it alerts once reads have failed this many sweeps in a row.
READ_FAILURE_SWEEPS = 3
EXPIRY_WARN_DAYS = 7
EXPIRY_WARN_EVERY_SEC = 86400


def budget_spent_line(where: str, budget: float, exc: BaseException) -> str:
    return (f"⚠️ Diaktoros {where} watchdog stopped: the sweep ran out of its {budget:g}s "
            f"budget — GitHub reads were slow or did not answer ({exc}); the rest of this run "
            f"was skipped, and the next sweep starts fresh")


def sweep_budget_spent(loop: dict, st: state_mod.LoopState, budget: float,
                       exc: BaseException) -> list[str]:
    """A sweep that ran out of its budget is a failed-read sweep in #54's sense — but not proof
    that GitHub gave no answer: thirty slow, answered reads spend it just as surely. So it is
    counted in the health check's ``github_read`` record, described as what it is ("ran out of
    time"), and said like any failure short of a 401/403: after ``READ_FAILURE_SWEEPS`` sweeps
    in a row, then once per cooldown (alert key ``read:slow``). Its end is said once by the
    health check ("GitHub reads work again")."""
    now = time.time()
    watch = st.watch()
    record = watch.get("github_read") if isinstance(watch.get("github_read"), dict) else {}
    sweeps = record["sweeps"] + 1 if type(record.get("sweeps")) is int else 1
    since = watch_mode.valid_clock(record.get("since"), now) or now
    login = str(loop.get("read_token") or "the read token")
    record = {**record, "sweeps": sweeps, "since": since, "status": None, "login": login,
              "error": gh.one_line(f"the sweep ran out of its {budget:g}s budget "
                                   f"(reads slow or unanswered): {exc}", 200)}
    cooldown = 0.0 if watch_mode.TEST else float(loop.get("cooldown_h") or 6) * 3600
    lines = []
    if sweeps >= READ_FAILURE_SWEEPS and watch_mode.alert_due(watch, "read:slow", now, cooldown):
        record["alerted"] = True
        lines.append(budget_spent_line(f"[{loop.get('id', '?')}] {loop.get('repo', '')}".rstrip(),
                                       budget, exc)
                     + f" — reading as {login} ({sweeps} sweep(s) in a row since "
                       f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(since))})")
    watch["github_read"] = record
    st.watch_save(watch)
    st.note(f"run: stopped — the sweep ran out of its {budget:g}s budget ({sweeps} sweep(s))")
    return lines


def parse_expiry(value: str) -> float | None:
    """GitHub's expiry header (``2026-10-01 12:00:00 UTC``, or a numeric offset) as epoch."""
    return gh.parse_token_expiry(value)


READ_ALERT = "cannot read GitHub as {who}"   # the health alert's opening words


class Health:
    """What this sweep's health check found, as data rather than wording.

    ``alerted`` — the read alert fired this sweep (so a per-read line would repeat it);
    ``failing`` — one of the reads it covers failed this sweep. Recovery is not announced
    here: :func:`settle_health` does that once the sweep's other reads are in.
    """

    def __init__(self, lines: list[str], who: str, alerted: bool, failing: bool):
        self.lines, self.who, self.alerted, self.failing = lines, who, alerted, failing


def _read_failed(loop: dict, watch: dict, now: float, who: str, error: str,
                 status: int | None, skipped: str) -> tuple[list[str], bool]:
    """Count one failed sweep and alert on #54's cadence (401/403 at once, anything else after
    ``READ_FAILURE_SWEEPS`` sweeps in a row, re-raised every cooldown). ``(lines, alerted)``."""
    header = f"[{loop['id']}] {loop['repo']}"
    cooldown = 0.0 if watch_mode.TEST else float(loop.get("cooldown_h") or 6) * 3600
    record = watch.get("github_read") if isinstance(watch.get("github_read"), dict) else {}
    sweeps = record["sweeps"] + 1 if type(record.get("sweeps")) is int else 1
    since = watch_mode.valid_clock(record.get("since"), now) or now
    record = {**record, "sweeps": sweeps, "since": since, "error": error[:200],
              "status": status, "login": who}
    lines: list[str] = []
    if status in (401, 403) or sweeps >= READ_FAILURE_SWEEPS:
        if watch_mode.alert_due(watch, f"read:{status or 'none'}", now, cooldown):
            record["alerted"] = True
            hint = gh.failure_hint(status)
            lines.append(
                f"⚠️ Diaktoros {header}: {READ_ALERT.format(who=who)}: {error}"
                f"{f' — {hint}' if hint else ''} ({sweeps} sweep(s) since "
                f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(since))}). "
                + skipped + "route self-heal and notices continue.")
    watch["github_read"] = record
    return lines, bool(lines)


def _outage_like(error: str) -> bool:
    """A failed read that says GitHub (or the token) is the problem, not this one resource:
    401/403, a 5xx, or no HTTP answer at all."""
    status = gh.status_of(error)
    if status is None:
        return "invalid" not in error and "exceeds" not in error
    return status in (401, 403) or status >= 500


def _pr_list(failures: list[tuple[int, str]]) -> str:
    """Every failed PR, by number: the first three with their reason, the rest as numbers."""
    listed = "; ".join(f"#{n}: {gh.one_line(e, 120)}" for n, e in failures[:3])
    rest = [f"#{n}" for n, _ in failures[3:]]
    return listed + (f"; also {', '.join(rest)}" if rest else "")


def settle_health(loop: dict, watch: dict, now: float, health: Health,
                  pr_failures: list[tuple[int, str]]) -> tuple[list[str], set[int]]:
    """After the sweep's per-PR reads: outage-like per-PR failures count as a failed read
    (same cadence, same alert, naming every one of those PRs); "reads work again" is said only
    when *every* read this sweep made succeeded. ``(lines, covered)`` — the PRs the alert
    named, which therefore need no line of their own."""
    if health.failing:
        return [], set()
    outage = [(n, e) for n, e in pr_failures if _outage_like(e)]
    if outage:
        said, alerted = _read_failed(
            loop, watch, now, health.who,
            gh.one_line(f"PR review read: {_pr_list(outage)}", 1000),
            gh.status_of(outage[0][1]),
            "Those PRs' stall checks are skipped (nothing guessed); ")
        return said, ({n for n, _ in outage} if alerted else set())
    if pr_failures:
        return [], set()                 # not healthy yet, and not an outage: no recovery
    record = watch.get("github_read") if isinstance(watch.get("github_read"), dict) else {}
    lines = []
    if record.get("alerted"):
        lines.append(f"✅ Diaktoros [{loop['id']}] {loop['repo']}: GitHub reads work again as "
                     f"{health.who} (after {record.get('sweeps', '?')} failed sweep(s))")
    watch.pop("github_read", None)
    marks = watch.get("github_alerts")
    if isinstance(marks, dict):
        for key in [k for k in marks if k.startswith("read:")]:
            marks.pop(key)
    return lines, set()


def github_health(loop: dict, st: state_mod.LoopState, watch: dict, now: float,
                  armed: bool | None, armed_error: str, listing_error: str = "") -> list[str]:
    """The health check with nothing else read this sweep (see :func:`read_health`)."""
    health = read_health(loop, st, watch, now, armed, armed_error, listing_error)
    return health.lines + settle_health(loop, watch, now, health, [])[0]


def read_health(loop: dict, st: state_mod.LoopState, watch: dict, now: float,
                armed: bool | None, armed_error: str, listing_error: str = "") -> Health:
    """Can this sweep read GitHub at all, as whom, and for how much longer?

    Runs every armed-or-unknown sweep, before anything that needs GitHub. Blindness is said out
    loud (with the login and HTTP status), re-raised every cooldown until reads work, and its
    end is said once (by :func:`settle_health`, after the sweep's per-PR reads). Also surfaces
    a gate's last failed read, which otherwise reached only the gateway's stderr while the
    event it could not verify was dropped as "unavailable".
    """
    lines: list[str] = []
    header = f"[{loop['id']}] {loop['repo']}"
    cooldown = 0.0 if watch_mode.TEST else float(loop.get("cooldown_h") or 6) * 3600
    configured = str(loop.get("read_token") or "the read token")
    probe = gh.auth_probe(loop)
    who = (probe.data.get("login") if isinstance(probe.data, dict) else None) or configured

    error, status = "", None
    if probe.error:
        error, status = probe.error, probe.status if probe.status is not None else gh.status_of(probe.error)
    elif listing_error:
        # The open-PR listing is a read like the others: a 403 there (a token that can see the
        # hooks but not the pulls) is the same blindness, on the same cadence.
        error, status = f"open PR list: {listing_error}", gh.status_of(listing_error)
    error = gh.one_line(error, 300)

    alerted = False
    if error:
        said, alerted = _read_failed(
            loop, watch, now, who, error, status,
            "Open PRs unknown: stall scan and queue drain are skipped; "
            if listing_error and not probe.error else "")
        lines.extend(said)

    expiry = (probe.headers or {}).get(gh.TOKEN_EXPIRY_HEADER) if not probe.error else None
    expires = parse_expiry(expiry) if expiry else None
    if expires is not None and expires - now < EXPIRY_WARN_DAYS * 86400:
        if watch_mode.alert_due(watch, f"expiry:{int(expires)}", now,
                     0.0 if watch_mode.TEST else EXPIRY_WARN_EVERY_SEC):
            days = (expires - now) / 86400
            when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(expires))
            named = who if who == configured else f"{who} ({configured})"
            lines.append(f"⚠️ Diaktoros {header}: the read token for {named} "
                         + (f"expires {when} — in {days:.1f} day(s); rotate it before then or "
                            "this loop goes blind" if days > 0 else
                            f"expired {when}; rotate it now"))

    failure = st.github_failure()
    at = watch_mode.valid_clock(failure.get("at"), now)
    seen = watch_mode.valid_clock(watch.get("gate_failure_seen"), now) or 0.0
    # A gate's failed read that its gate-failure entry owns is alerted (and re-driven) by that
    # ledger's sweep; saying it here too would report one read twice.
    # Owned only while a ledger still backs the owner: a marker whose entry is gone (its ledger
    # was moved aside) must not keep this read silent.
    owner = gate_failures.owner_state(loop, failure) if failure.get("owned_by") else ""
    done = bool(failure.get("resolved_by")) or owner == "resolved"
    settled = done or owner == "open"        # "gone" or "unreadable": nothing else will say it
    if at is not None and at > seen and done:
        watch["gate_failure_seen"] = at      # its gate-failure entry said it, and it resolved
    if at is not None and at > seen and not settled:
        watch["gate_failure_seen"] = at
        status = failure.get("status") if type(failure.get("status")) is int else None
        if watch_mode.alert_due(watch, f"gate:{failure.get('where')}:{status or 'none'}", now,
                                cooldown):
            hint = gh.failure_hint(status)
            number = re.search(r"/pulls/(\d+)", str(failure.get("path") or ""))
            lines.append(
                f"⚠️ Diaktoros {header}: {failure.get('where') or 'a gate'} could not "
                f"{failure.get('method') or 'GET'} {failure.get('path') or '?'} as "
                f"{failure.get('login') or configured} at "
                f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(at))}: "
                f"{gh.one_line(failure.get('error') or 'unknown error', 300)}"
                f"{f' — {hint}' if hint else ''}; "
                + ("it treated the PR as unavailable and started nothing"
                   if (failure.get("method") or "GET") == "GET" else
                   gh.write_outcome(str(failure.get("method")), str(failure.get("path") or ""),
                                    status))
                + (f" — `hermes dk explain --loop {loop['id']} --pr {number.group(1)}`"
                   " shows it" if number else ""))
    return Health(lines, who, alerted, bool(error))


def finish_reads(loop: dict, watch: dict, now: float, health: Health | None,
                 pr_failures: list[tuple[int, str]], lines: list[str]) -> None:
    """Say which PRs' review reads failed this sweep, and settle the health check now that
    every read is in. Every failed PR appears in some line: the ones the read alert named are
    not repeated, every other one is in a single bounded line."""
    covered: set[int] = set()
    if health is not None:
        said, covered = settle_health(loop, watch, now, health, pr_failures)
        lines.extend(said)
    rest = [(n, e) for n, e in pr_failures if n not in covered]
    if rest:
        lines.append(gh.one_line(
            f"⚠️ Diaktoros [{loop['id']}] {loop['repo']}: could not read reviews for "
            f"{len(rest)} PR(s) — {_pr_list(rest)} — their stall check skipped this sweep "
            f"(no verdict guessed)", 1000))
