"""The Desktop page's backend logic (#369, #630): read-only views over one loop, for
``dashboard/plugin_api.py``. Kept free of FastAPI so it is tested without it.

Nothing here writes, calls GitHub or reads a token: ``stats`` is the run ledger's half of
``hermes dk stats`` (totals and timings only), ``now`` lists live runs with host-written
reasons, ``overview`` is that same live state for every configured loop at once (for the status
chip and the cross-loop block), ``pr`` adds the host's own "what moves it" line
(``run_supervisor.next_step``) plus what the ledger recorded, ``health`` is ``doctor``'s
checks with the network reads left unprobed, and ``settings`` is the plugin-level form and each
loop's effective answers. Never a turn's output, never a run's error text.
"""
from __future__ import annotations

import pathlib
import time

from . import config, doctor, findings, gate_decisions, pacing, run_supervisor, stats

SINCE_CHOICES = ("24h", "7d", "30d")
# What the ledger keeps for one PR is its turns; a long-lived PR is bounded here so a single
# pathological one cannot make the page huge.
TURNS_MAX = 50
# How many gate decisions the drawer shows for one PR: the recent trail, not the whole log.
DECISIONS_MAX = 10


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
    # A missing ledger is not a verified zero: ``stats.now`` returns None when there is no
    # ledger, so the view carries ``available`` and the page renders unavailable, never "nothing
    # running". The empty runs and counts below are placeholders for that case only.
    available = view is not None
    if view is None:
        view = {"repo": loop["repo"], "runs": [], "counts": {}}
    view["available"] = available
    # Beside the runs, the live usage-window state: a run's own held line is a snapshot from the
    # moment it was held, and it can outlive the window it names (#650).
    view["pacing"] = pacing.seats(loop)
    return view


def overview_view() -> dict:
    """Every configured loop's live run state, in one read.

    The status chip and the cross-loop block answer "what is waiting on me" across all loops
    without one request per loop. Per loop: its id and repo, the live run counts and the
    uncertain ("needs you") count when the ledger could be read, and whether it could be read at
    all — ``available`` false when there is no ledger, ``error`` true when the read raised. A
    missing ledger is unavailable, never a verified zero, exactly as ``/now`` reports it. One
    loop failing is not the others' failure: it is reported on its own entry.
    """
    found, _ = config.readable_loops()
    db = run_supervisor.production_ledger()
    entries = []
    for loop in found:
        entry = {"id": loop["id"], "repo": loop["repo"], "available": True, "error": False,
                 "counts": {}, "needs_you": 0}
        try:
            view = stats.now(db, loop["repo"])
        except Exception:                   # one loop's read failing must not blank the others
            entry["available"], entry["error"] = False, True
        else:
            if view is None:
                entry["available"] = False
            else:
                entry["counts"] = view["counts"]
                entry["needs_you"] = view["counts"].get("uncertain", 0)
        entries.append(entry)
    return {"loops": entries, "read_at": time.time()}


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
                  "findings": [], "decisions": [], "read_at": time.time()}
    # The loop's own state files, not the ledger: what the reviewer raised, and why each gate
    # did or did not act. Both read-only, both local.
    view["findings"] = _findings(loop, pr)
    view["decisions"] = _decisions(loop, pr)
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
    view["pacing"] = pacing.seats(loop)
    return view


def _findings(loop: dict, pr: int) -> list[dict]:
    """What the reviewer has raised on this PR, across rounds (#475), from the loop's own state
    file: the id, its state, the text, and the head it was raised at. Read, never written."""
    from . import state as state_mod
    try:
        entry = state_mod.state_for(loop).findings_get(pr)
    except Exception:                   # a state dir this reader cannot open is not an answer
        return []
    found = entry.get("findings") if isinstance(entry, dict) else None
    if not isinstance(found, dict):
        return []
    open_ids = set(findings.open_ids(entry))
    out = []
    for fid, item in found.items():
        if not isinstance(fid, str) or not isinstance(item, dict):
            continue
        at = item.get("at")
        out.append({"id": fid, "state": str(item.get("state") or ""), "open": fid in open_ids,
                    # The host's own ``findings.lines`` strips the separator the reviewer
                    # writes after the id, so the same text reads the same in both places.
                    "text": str(item.get("text") or "").lstrip(" :")[:300],
                    "head": str(item.get("head") or "")[:7],
                    "at": float(at) if isinstance(at, (int, float)) else None,
                    "missed": bool(item.get("missed"))})
    out.sort(key=lambda item: int(item["id"][1:]) if item["id"][1:].isdigit() else 0)
    return out


def _decisions(loop: dict, pr: int) -> list[dict]:
    """Why each gate did or did not act on this PR (#209): the gate, the event and action, who
    sent it, the decision and its reason. Oldest first, as the host writes them."""
    try:
        return gate_decisions.for_pr(loop, pr, limit=DECISIONS_MAX)
    except Exception:                   # unreadable is empty, exactly as the host treats it
        return []


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


def settings_view() -> dict:
    """The plugin-level settings the Desktop form renders, and what each loop runs as today.

    Two halves, from the same functions ``hermes dk settings`` prints from:

    * **``plugin``** — the schema every field needs (label, type, default, description), the
      effective value of each key, and whether it was *set* in the form or is the schema default.
      This is what a form renders and what ``apply --loop`` would push onto a loop.
    * **``loops``** — each configured loop's *effective* seats, budgets, step caps and flags, so
      the page shows what a loop runs as now without applying anything to it.

    Settings are defaults, not a subscription: an existing loop keeps its own numbers until
    ``apply`` pushes the plugin's onto it, so a blank field is "not set here", never "forget what
    this loop already answered".

    Safety, per this module's rule that nothing here writes:

    * **A token is a path, never a value.** ``token_file`` names where a login's PAT is read from
      at use time; the file is never opened, read or returned.
    * **An unreadable settings store is unavailable, not a clean default** — ``available`` is
      false and ``reason`` says why, so the page cannot present schema defaults as if the
      operator had chosen them. A store that reads fine but holds nothing is available,
      all-default, and says so.
    * **One loop's failure is not the others'**: an unreadable loop is reported on its own entry.
    """
    settings, available, reason = _form_settings()
    defaults = config.settings_defaults(settings)
    return {
        "plugin": {
            "available": available,
            "reason": reason,
            "schema": {
                key: {"label": spec.get("label", key), "type": spec.get("type", "str"),
                      "default": spec.get("default"),
                      "description": spec.get("description", "")}
                for key, spec in config.SETTINGS_SCHEMA.items()
            },
            "values": {key: defaults.get(key) for key in config.SETTINGS_SCHEMA},
            "sources": {key: ("set" if str(settings.get(key, "")) not in ("", "None")
                              else "default")
                        for key in config.SETTINGS_SCHEMA},
            "seats": _seat_settings(settings),
            "roles": list(config.ROUTE_ROLES),
            "routed_roles": list(config.ROUTED_ROLES),
        },
        "loops": _loop_settings(),
        "read_at": time.time(),
    }


def _form_settings() -> tuple[dict, bool, str]:
    """The settings the form holds, whether they could be read, and the reason when they could not.

    Read through ``hermes_cli.plugins_settings`` — the form's own reader — so the page and the
    form can never disagree about what is set. That reader is a documented plugin surface that
    holds no process state, so it answers the same in any process, including one where this
    plugin was never registered with its settings.

    Imported lazily and defensively: a Hermes without that surface, or a malformed manifest, is
    reported as ``available=False`` with a reason — never as an empty form.
    """
    try:
        from hermes_cli.plugins_settings import plugin_settings_fields
    except Exception as exc:                    # any import failure is the same answer
        return {}, False, f"settings surface unavailable: {type(exc).__name__}"
    plugin_dir = pathlib.Path(__file__).resolve().parent.parent
    try:
        fields = plugin_settings_fields("diaktoros", plugin_dir)
    except Exception as exc:                    # a bad manifest must not raise into a render
        return {}, False, f"settings could not be read: {type(exc).__name__}"
    values: dict = {}
    for field in fields or []:
        try:
            key = str(field["key"])
        except Exception:                       # a malformed field is skipped, not fatal
            continue
        if field.get("value") not in (None, ""):
            values[key] = field["value"]
    return values, True, ""


def _seat_settings(settings: dict) -> dict:
    """The form's seat mapping: profiles and logins, and token *paths* only.

    Blanks stay blank, because a blank is "not set here" rather than "forget what this loop
    uses". The token file is named, never opened: this module reads no credential.
    """
    mapping = config.seat_mapping(settings)
    out: dict = {}
    for role in config.ROUTE_ROLES:
        entry = mapping.get(role) or {}
        out[role] = {
            "profile": entry.get("profile") or "",
            "login": entry.get("login") or "",
            "token_file": entry.get("token_file") or "",
            "set": bool(entry),
        }
    return out


def _loop_settings() -> list[dict]:
    """What each loop runs as today: effective seats, budgets, step caps and flags.

    One unreadable loop is reported on its own entry and never suppresses the others — the same
    rule ``overview`` follows for the ledger.
    """
    found, _ = config.readable_loops()
    entries: list[dict] = []
    for loop in found:
        entry = {"id": loop.get("id", ""), "repo": loop.get("repo", ""), "error": False}
        try:
            entry["base"] = loop.get("base", "")
            entry["seats"] = {role: _seat_line(loop, role) for role in config.ROUTE_ROLES}
            entry["flags"] = _loop_flags(loop)
        except Exception:                       # one broken loop must not blank the others
            entry["error"] = True
        entries.append(entry)
    return entries


def _seat_line(loop: dict, role: str) -> dict:
    """One seat's effective identity for a loop, its budget and step cap included."""
    entry = loop.get(role) or {}
    return {
        "profile": entry.get("profile") or "",
        "login": entry.get("login") or "",
        "token_file": entry.get("token_file") or "",
        "route": entry.get("route") or "",
        "turn_budget_s": config.turn_budget(loop, role),
        "max_steps": config.max_steps(loop, role),
    }


def _loop_flags(loop: dict) -> dict:
    """The on/off settings the page shows as chips. Absent is off, and off is reported as off."""
    return {
        "review_after_ci": bool(loop.get("review_after_ci")),
        "fixer_check": loop.get("fixer_check") or "",
        "ci_fix": bool(loop.get("ci_fix")),
        "cap": loop.get("cap") or config.SETTINGS_SCHEMA["cap"]["default"],
        "base": loop.get("base") or config.SETTINGS_SCHEMA["base"]["default"],
    }
