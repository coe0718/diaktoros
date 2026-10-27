"""Which run a retry would re-arm, and re-arming it — one owner for the CLI and the Desktop (#53, #371).

``hermes dk retry`` and the Desktop page's Retry button both come through this module, so the two
cannot drift apart: one selection rule (the newest head that still has an offerable row), one
refusal rule (``Supervisor.retry``'s own, called unchanged), one order of side effects (re-arm,
release the fixer scope lock only if a row was re-armed, record the operator action, start a worker).

The caller supplies the three answers only it can give:

* ``resolve_head()`` — the PR's own head, for the supersession tiebreaker (#126). The CLI passes a
  GitHub read. The Desktop passes nothing and takes the ledger-only answer, which is exactly the
  CLI's documented fallback when its read fails.
* ``read_pr()`` — the #723 check that a failed run's PR is still open, when the watchdog's last
  complete listing is stale or silent. The Desktop passes nothing: it acts on the listing alone,
  so a stale listing that still *names* the PR allows the retry (the worker's own claim re-reads
  the PR before any write) and one that omits it refuses.
* ``confirm_head`` — what the operator was shown before confirming. When the head that would be
  re-armed is no longer that head, the retry is refused instead of silently re-arming a different
  run (#371).

Nothing here merges, escalates or drains a PR; nothing here reads a token or posts to GitHub.
"""
from __future__ import annotations

import time

from . import config, gate, scope_lock
from .run_supervisor import (ISSUE_SEATS, WATCH_INTERVAL_S, Supervisor, open_listing,
                             operator_cancelled, policy_cancelled, write_evidence)

# The seats ``retry`` accepts (the CLI parser's own list). A shadow run is retried only by name.
SEATS = ("reviewer", "fixer", "adjudicator", "triage", "issue_fixer", "shadow")

# One refusal is printed on the page and in a 409 body: one bounded line, never a traceback.
REASON_MAX = 600


def bounded(text: object, limit: int = REASON_MAX) -> str:
    """One line: whitespace collapsed, capped at a word boundary with an ellipsis.

    The host's own refusal text names the command that fixes it, so cutting mid-command would
    hide the actionable half; a cut that lands inside a word is worse than a shorter line.
    """
    line = " ".join(str(text).split())
    if len(line) <= limit:
        return line
    cut = line.rfind(" ", 0, limit - 1)
    return line[:cut if cut > 0 else limit - 1] + "…"


def offered(row: dict) -> bool:
    """Whether ``retry`` would *attempt* this row at its head: failed, waiting, uncertain, a
    cancellation the fixer push policy caused, or an operator's cancel (#1148) — the ones an
    operator retry recovers. Any other cancellation is superseded — a new head gets its own turn."""
    return row["state"] in ("failed", "waiting", "uncertain") or (
        row["state"] == "cancelled" and (policy_cancelled(row["error"])
                                         or operator_cancelled(row["error"])))


def rows_for(con, repo: str, pr: int, seat: str | None = None) -> list[dict]:
    """Every ledgered run for one PR, oldest first. A shadow run (#218) is retried only by name:
    it is no part of the PR's loop."""
    rows = [dict(row) for row in con.execute(
        "SELECT id,seat,head,turn_key,state,error,updated FROM runs WHERE repo=? AND pr=? "
        "ORDER BY created,id", (repo, pr))]
    return [row for row in rows
            if row["seat"] == seat or (not seat and row["seat"] != "shadow")]


def newest(rows: list[dict]) -> dict:
    # Not a timestamp: the claim path bumps `updated` when it supersedes a row for a head the PR
    # has since left, so `max(updated)` can name an abandoned head (see ``plan``).
    return max(rows, key=lambda row: (row["updated"] or 0, row["id"]))


def plan(con, repo: str, pr: int, seat: str | None = None, resolve_head=None) -> dict:
    """What re-arming this PR (or this seat's run) would pick, from the ledger.

    The head is the newest one that still has an offerable row — the ledger-only answer, which is
    also the CLI's fallback when its PR read fails. When a supersession bump makes the newest
    *offerable* head disagree with the newest head of any row, ``resolve_head()`` (the CLI's PR
    read) is the tiebreaker; without one the ledger-only answer stands and ``disputed`` says so,
    so a caller that must show the target can still name it exactly.

    Every row is classified: ``rearmable`` ones would actually be re-armed (write evidence is
    checked here, with the same function ``Supervisor.retry`` re-checks inside its transaction),
    and ``blocked`` maps an offered row's id to why it would be refused.
    """
    rows = rows_for(con, repo, pr, seat)
    out = {"head": "", "head7": "", "rows": rows, "offered": [], "rearmable": [], "blocked": {},
           "disputed": False, "message": ""}
    if not rows:
        out["message"] = (f"#{pr}: no isolated run on record"
                          + (f" for the {seat} seat" if seat else ""))
        return out
    offerable_rows = [row for row in rows if offered(row)]
    ledger_head = (newest(offerable_rows) if offerable_rows else newest(rows))["head"]
    head = ledger_head
    out["disputed"] = bool(offerable_rows) and ledger_head != newest(rows)["head"]
    if out["disputed"] and resolve_head is not None:
        head = resolve_head() or ledger_head
    out["head"], out["head7"] = str(head or ""), str(head or "")[:7]
    out["offered"] = [row for row in rows if row["head"] == head and offered(row)]
    if not out["offered"]:
        stuck = ", ".join(f"{row['seat']} {row['state']}" for row in rows if row["head"] == head)
        out["message"] = f"#{pr} @ {out['head7']}: nothing to retry — {stuck}"
        return out
    for row in out["offered"]:
        wrote = write_evidence(con, row["id"])
        if wrote is None:
            out["rearmable"].append(row)
        else:
            out["blocked"][row["id"]] = bounded(wrote)
    return out


def fixer_policy(loop: dict) -> str:
    """Why a fixer retry would be refused under the push policy in force now, or ``""``.

    The decision is ``Supervisor.retry``'s, and this is the same config read it makes — no
    second rule. It exists so the page can withhold a button that could only be refused: a
    fixer turn cannot publish while unattended pushes are off, and the retry says so.
    """
    try:
        if config.unattended_fixer_push_enabled(loop):
            return ""
        return ("unattended fixer pushes are off for " + str(loop.get("repo") or "this loop")
                + ", so a fixer turn could not publish; run `"
                + config.fixer_push_enable_command(loop) + "` first, then retry")
    except Exception:                        # an unusable loop config refuses a fixer retry too
        return "the loop configuration is unusable, so a fixer turn could not publish"


def page_targets(con, repo: str, loop: dict | None = None) -> dict:
    """``{(pr, seat, head7, state): entry}`` for the page's Retry buttons, from one ledger read.

    Each entry says whether a retry of that row would succeed, the head it would re-arm, and —
    when it would not — the host's own bounded reason (the write evidence that blocks it, or a
    newer head that gets its own turn). It is the same plan ``perform`` runs, so a button the
    page draws is a button the host would honour; ``eligible`` is true only for a row the retry
    actually re-arms, never for one it would refuse. Shadow runs are left out: the page has no
    control for them.
    """
    rows = [dict(row) for row in con.execute(
        "SELECT id,pr,seat,head,state,error,updated FROM runs WHERE repo=? AND seat!='shadow' "
        "ORDER BY created,id", (repo,))]
    groups: dict = {}
    for row in rows:
        groups.setdefault((row["pr"], row["seat"]), []).append(row)
    out: dict = {}
    policy_off = fixer_policy(loop) if loop is not None else ""
    listing = open_listing(loop) if loop is not None else None
    for (pr, seat), group in groups.items():
        offerable_rows = [row for row in group if offered(row)]
        head = (newest(offerable_rows) if offerable_rows else newest(group))["head"]
        # The refusal ``perform`` makes for a PR the watchdog's complete listing shows closed:
        # one verdict for every offered row at the head, from the same function.
        closed = (closed_refusal(loop, pr, [row for row in offerable_rows if row["head"] == head],
                                 listing) if loop is not None else "")
        for row in group:
            entry = {"eligible": False, "head": str(head or ""), "reason": ""}
            if row["head"] != head:
                entry["reason"] = f"not the newest run for this PR: retry targets {str(head)[:7]}"
            elif not offered(row):
                entry["reason"] = f"{row['state']}: retry does not re-arm this run"
            elif closed:
                entry["reason"] = bounded(closed)
            elif seat == "fixer" and policy_off:
                entry["reason"] = bounded(policy_off)
            else:
                wrote = write_evidence(con, row["id"])
                if wrote is None:
                    entry["eligible"] = True
                else:
                    entry["reason"] = bounded(wrote)
            key = (row["pr"], row["seat"], str(row["head"] or "")[:7], row["state"])
            if key not in out or (entry["eligible"] and not out[key]["eligible"]):
                out[key] = entry
    return out


def pr_open(loop: dict, pr: int, listing: dict | None = None, read_pr=None):
    """Whether a failed run's PR is still open (#723): True, False, or None when it cannot be told.

    The watchdog's last COMPLETE listing decides when it is fresh and names the PR. A stale or
    silent listing is confirmed with one PR read when the caller has one (the CLI); without one
    (the Desktop) a PR the listing still names is left to the worker's own claim-time read, and a
    PR it omits is refused. No listing at all is not evidence either way.
    """
    listing = open_listing(loop) if listing is None else listing
    if listing is None:
        return None
    absent = pr not in listing["numbers"]
    if absent or time.time() - listing["at"] > WATCH_INTERVAL_S:
        if read_pr is None:
            return False if absent else None
        live = read_pr()
        if isinstance(live, dict):
            return live.get("state") == "open"
        return not absent
    return True


def closed_refusal(loop: dict, pr: int, offered_rows: list[dict], listing: dict | None = None,
                   read_pr=None) -> str:
    """The whole-command refusal for a PR that is no longer open, or ``""`` (#723).

    Only a failed PR-seat row among those offered is judged; issue seats are keyed by issue
    number and never are. ``perform`` refuses with this text and the Desktop's row verdict
    (``page_targets``) withholds its button with the same text.
    """
    listing = open_listing(loop) if listing is None else listing
    if listing is not None and any(row["state"] == "failed" and row["seat"] not in ISSUE_SEATS
                                   for row in offered_rows):
        if pr_open(loop, pr, listing, read_pr) is False:
            return f"[{loop['id']}] PR #{pr} is not open (merged or closed); nothing to re-arm"
    return ""


def perform(loop: dict, pr: int, seat: str | None = None, *, ledger=None, confirm_head: str | None = None,
            resolve_head=None, read_pr=None, reveal_errors: bool = False) -> dict:
    """Re-arm what ``retry`` would re-arm, and answer in structured form.

    The result carries the lines the CLI prints and the facts the Desktop page shows:
    ``rearmed`` and ``refused`` entries (seat, head, the state it was in), ``message`` for a
    whole-command refusal, the fixed ``worker`` line, and whether the scope lock moved.

    ``reveal_errors`` is the CLI's: its line names the run's recorded error, which a page must
    never render (a turn's error can carry transport text). The page's answers are built from
    the states and the retry's own refusal message only.
    """
    if seat is not None and seat not in SEATS:
        raise ValueError(f"seat must be one of {', '.join(SEATS)}")
    ledger = config.host_path("ledger") if ledger is None else ledger
    out = {"ok": False, "loop": str(loop.get("id") or ""), "pr": pr, "seat": seat or "",
           "head": "", "head7": "", "rearmed": [], "refused": [], "message": "", "released": None,
           "worker": "", "read_at": time.time()}
    if not ledger.exists():
        out["message"] = f"no run ledger at {ledger} — nothing to retry"
        return out
    sup = Supervisor(ledger)
    # Snapshot before planning or re-arming. Never hold the JSON lock across a ledger
    # transaction (workers may take the locks in the opposite order).
    held = scope_lock.snapshot(loop, pr)
    with sup._connect() as con:
        found = plan(con, loop["repo"], pr, seat, resolve_head=resolve_head)
    out["head"], out["head7"] = found["head"], found["head7"]
    if found["message"]:
        out["message"] = f"[{loop['id']}] " + found["message"]
        return out
    # A confirmation is about one run at one head. If that head has moved, refuse rather than
    # silently re-arm whatever is newest now (#371).
    if confirm_head and not found["head"].startswith(str(confirm_head).strip().lower()):
        out["message"] = (f"[{loop['id']}] #{pr}: refused: the run's head moved since you "
                          f"confirmed {str(confirm_head)[:7]} (it is {found['head7']} now) — "
                          "nothing was re-armed; check the row and retry")
        return out
    closed = closed_refusal(loop, pr, found["offered"], read_pr=read_pr)
    if closed:
        out["message"] = closed
        return out
    rearmed, refused = 0, 0
    for row in found["offered"]:
        entry = {"id": row["id"], "seat": row["seat"], "head": found["head"],
                 "head7": found["head7"], "state": row["state"],
                 "turn_key": str(row.get("turn_key") or "")}
        if reveal_errors:
            entry["error"] = row["error"] or ""
        try:
            # The loop's budget now (#49): a turn killed at its budget reruns on the raised one.
            sup.retry(row["id"], budget=config.turn_budget(loop, row["seat"]))
        except ValueError as exc:
            refused += 1
            entry["reason"] = bounded(exc)
            out["refused"].append(entry)
            continue
        rearmed += 1
        out["rearmed"].append(entry)
    if rearmed:
        # #482: retry releases the PR's hold only after at least one committed re-arm, even
        # on CLI partial success. The plan is only a preview: retry re-checks write evidence
        # and policy under its transaction and can still refuse every offered row. It never
        # spawns; release before asking resume_isolated to start this retry's worker.
        # A worker may already have replaced the hold: approve only the exact snapshot
        # at this retry's head, atomically compared under the state-file writers' lock.
        expected = held if held and held.get("head") == found["head"] else None
        out["released"] = scope_lock.release(loop, pr, expected=expected)
        # Best effort by its own contract (a ledger that refuses the note never fails the step);
        # an unknown action name is the only thing it raises for, and "retry" is one of them.
        sup.record_operator_action(loop["repo"], pr, "retry")
        try:
            started = gate.resume_isolated(loop)
        except Exception as exc:
            out["worker"] = f"worker not started: {type(exc).__name__}: {exc}"
        else:
            out["worker"] = ("started" if started else
                             "no private runtime file — the run stays pending until one exists")
    out["ok"] = bool(rearmed and not refused)
    return out


def answer(result: dict) -> str:
    """The page's one-line answer about a retry: re-armed, or why not — the host's own words.

    Built from the states and the retry's refusal message, never from a run's recorded error
    (that is ``reveal_errors``' territory, for a terminal).
    """
    if result.get("rearmed"):
        lines = [f"{entry['seat']} #{result['pr']} @ {entry['head7']} re-armed (was {entry['state']})"
                 for entry in result["rearmed"]]
        if result.get("refused"):
            lines.append(f"{len(result['refused'])} refused: " + result["refused"][0]["reason"])
        if result.get("worker"):
            lines.append(result["worker"])
        return bounded(" · ".join(lines))
    if result.get("refused"):
        entry = result["refused"][0]
        return bounded(f"{entry['seat']} #{result['pr']} @ {entry['head7']} "
                        f"{entry['state']}: {entry['reason']}")
    return bounded(result.get("message") or "nothing to retry")
