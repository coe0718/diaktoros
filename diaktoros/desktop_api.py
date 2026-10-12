"""The Desktop page's backend logic (#369, #630): read-only views over one loop, for
``dashboard/plugin_api.py``. Kept free of FastAPI so it is tested without it.

Nothing here writes, calls GitHub or reads a token: ``stats`` is the run ledger's half of
``hermes dk stats`` (totals and timings only), ``now`` lists live runs with host-written
reasons, ``pr`` adds the host's own "what moves it" line (``run_supervisor.next_step``) plus
what the ledger recorded, and ``health`` is ``doctor``'s checks with the network reads left
unprobed. Never a turn's output, never a run's error text.
"""
from __future__ import annotations

import time

from . import config, doctor, run_supervisor, stats

SINCE_CHOICES = ("24h", "7d", "30d")
# What the ledger keeps for one PR is its turns; a long-lived PR is bounded here so a single
# pathological one cannot make the page huge.
TURNS_MAX = 50


class NotFound(LookupError):
    pass


class BadRequest(ValueError):
    pass


class Forbidden(PermissionError):
    pass


# The probe reads a credential and calls GitHub, so it answers only a caller on this machine.
# The dashboard server is the one surface Caddy exposes off-host. A forwarding header of ANY kind
# means a proxy is in the path even when the peer looks local (a same-host nginx, a tunnel), and
# presence is the signal, not the value: a header that is present but empty was still set by
# something in front of us (#648 review, F1).
LOCAL_HOSTS = ("127.0.0.1", "::1", "localhost")
FORWARDING_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip")


def probe_is_local(client_host: str | None, headers=None) -> bool:
    """Whether this caller may run the credential-reading probe: a local peer, and no forwarding
    header of any kind. An empty client_host (an in-process call) counts as local; a header that
    is present at all — even an empty one — does not."""
    if headers is not None and any(name in headers for name in FORWARDING_HEADERS):
        return False
    return not client_host or client_host in LOCAL_HOSTS


def loops() -> list[dict]:
    """The configured loops the page can show: id and repository only."""
    found, _ = config.readable_loops()
    return [{"id": loop["id"], "repo": loop["repo"]} for loop in found]


def _loop(loop_id: str | None) -> dict:
    found, _ = config.readable_loops()
    if loop_id:
        for loop in found:
            if loop["id"] == loop_id:
                return loop
        raise NotFound("no such loop")
    if len(found) != 1:
        raise NotFound("name a loop: " + (", ".join(loop["id"] for loop in found) or "none"))
    return found[0]


def stats_view(loop_id: str | None, since: str = "7d") -> dict:
    if since not in SINCE_CHOICES:
        raise BadRequest(f"since must be one of {', '.join(SINCE_CHOICES)}")
    loop = _loop(loop_id)
    return stats.collect(loop, run_supervisor.production_ledger(), stats.parse_since(since),
                         with_github=False)


def now_view(loop_id: str | None) -> dict:
    loop = _loop(loop_id)
    view = stats.now(run_supervisor.production_ledger(), loop["repo"])
    return view if view is not None else {"repo": loop["repo"], "runs": [], "counts": {}}


def health_view(loop_id: str | None, probe: bool = False, client_host: str | None = None,
                headers=None) -> dict:
    """``doctor``'s checks for one loop, read-only.

    Offline by default: every check that would need the network — the gateway probe, the hooks,
    token expiry and scopes, the labels — reports itself unprobed instead of reaching out, so
    opening the page fires no route and reads no token's meaning. ``probe`` runs the full set:
    it reads the loop's own tokens and makes a few GitHub reads, and it is refused for any caller
    that is not on this machine.

    Slower than the other views — seconds, not milliseconds: it stats profiles, the cron store
    and the venvs (and, when probing, reads GitHub) — so the page asks on demand rather than on
    every refresh.
    """
    loop = _loop(loop_id)
    if probe and not probe_is_local(client_host, headers):
        raise Forbidden("the probe is local-only: it reads the loop's tokens")
    checks = [{"name": check.name, "status": check.status, "detail": check.detail,
               "fix": check.fix, "paused": check.paused}
              for check in doctor.check_loop(loop, offline=not probe)]
    counts: dict[str, int] = {}
    for check in checks:
        counts[check["status"]] = counts.get(check["status"], 0) + 1
    return {"repo": loop["repo"], "loop": loop["id"], "offline": not probe, "checks": checks,
            "counts": counts, "read_at": time.time()}


def pr_view(loop_id: str | None, pr: int) -> dict:
    """One PR's ledger record: its turns, what the loop recorded about them, and — for a run
    that is not moving — the host's own line for what would move it.

    All of it is local. The verdicts the gates count are read from GitHub's reviews, where a
    person's verdict counts too, so ``verdicts`` here is what this ledger recorded: a floor,
    not the gate's own count.
    """
    if isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0:
        raise BadRequest("pr must be a positive integer")
    loop = _loop(loop_id)
    repo = loop["repo"]
    view: dict = {"repo": repo, "loop": loop["id"], "pr": pr, "cap": loop.get("cap"),
                  "stuck": [], "turns": [], "verdicts": {}, "rulings": [], "breach": None,
                  "read_at": time.time()}
    db = run_supervisor.production_ledger()
    con = run_supervisor.read_only_connect(db)
    if con is None:                                 # no ledger: an empty record, not an error
        view["breach"] = _breach(loop, pr)
        return view
    try:
        # runs_view carries what moves a run (its retry state, its write evidence); the times
        # come from the same rows keyed by id, since that view does not select them.
        stamp = {row["id"]: (row["created"], row["updated"]) for row in con.execute(
            "SELECT id, created, updated FROM runs WHERE repo=? AND pr=?", (repo, pr))}
        # The host's own "what moves it" line, for the runs that are actually not moving.
        for row in run_supervisor.runs_view(con, repo, pr):
            created, _ = stamp.get(row["id"], (None, None))
            view["stuck"].append({"seat": row["seat"], "head": str(row["head"] or "")[:7],
                                  "state": row["state"], "since": created,
                                  "next": run_supervisor.next_step(row, loop["id"])})
        for row in con.execute(
                "SELECT seat, head, state, created, updated FROM runs WHERE repo=? AND pr=? "
                "ORDER BY created, id LIMIT ?", (repo, pr, TURNS_MAX)):
            view["turns"].append({"seat": row["seat"], "head": str(row["head"] or "")[:7],
                                  "state": row["state"], "created": row["created"],
                                  "updated": row["updated"]})
        # Every stored receipt's verdict counts: review_receipt.submit refuses a COMMENT before
        # its durable point, so nothing that spent the reviewer's write without moving the loop
        # can be in here. Stored as GitHub's own state (APPROVED / CHANGES_REQUESTED).
        for row in con.execute(
                "SELECT rr.verdict AS verdict FROM review_receipts rr "
                "JOIN runs r ON r.id=rr.run_id WHERE r.repo=? AND r.pr=? ORDER BY rr.created",
                (repo, pr)):
            verdict = str(row["verdict"] or "")
            if verdict:
                view["verdicts"][verdict] = view["verdicts"].get(verdict, 0) + 1
        for row in con.execute(
                "SELECT verdict, head, created FROM rulings WHERE repo=? AND pr=? "
                "ORDER BY created LIMIT ?", (repo, pr, TURNS_MAX)):
            view["rulings"].append({"verdict": str(row["verdict"] or "")[:80],
                                    "head": str(row["head"] or "")[:7], "created": row["created"]})
    finally:
        con.close()
    view["breach"] = _breach(loop, pr)
    return view


def _breach(loop: dict, pr: int) -> dict | None:
    """The loop's cap marker for one PR, when it has one: the head that spent the cap and the
    rounds it took. Read through the state files' own reader, which persists nothing."""
    from . import state as state_mod
    try:
        marker = state_mod.state_for(loop).breach_get(pr)
    except Exception:                       # a state dir this reader cannot open is not an answer
        return None
    if not isinstance(marker, dict) or not marker:
        return None
    return {"head": str(marker.get("head") or "")[:7], "status": str(marker.get("status") or ""),
            "rounds": marker.get("rounds")}
