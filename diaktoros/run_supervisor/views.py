"""The ledger's read-only views (#1349, split from the core): the queue and run listings
(``pending_view``, ``runs_view``, ``read_only_view``, ``view_view``), the single reads the gates
and the watchdog make (``turn_state``, ``ruling_at``, ``dispute_at``), the open-PR listing that
tells history from work, and the operator's wording of a run (``describe_run``,
``hold_explanation``, ``next_step``). Every reader connects through ``read_only_connect``, so
none migrates or creates a WAL. The core's names are reached through the package
(``run_supervisor.X``), so a test patching ``run_supervisor.X`` still reaches this code; callers
use ``views.<name>``.
"""
from __future__ import annotations

import re
from pathlib import Path
import sqlite3
import time

from .. import config, ledger, run_supervisor
from . import claims
from ..util import epoch


def pending_view(db: str | Path, repo: str) -> list[dict] | None:
    """The loop's pending runs in claim order, read-only, each with ``why``. None: unreadable."""
    try:
        con = read_only_connect(db)
        if con is None:
            return None
        try:
            now = time.time()
            # A pending shadow (#218) is no part of the PR's queue.
            rows = con.execute("SELECT * FROM runs WHERE state='pending' AND repo=? AND "
                               + run_supervisor.NOT_SHADOW, (repo,)).fetchall()
            return [{**dict(r), 'why': claims.claim_why(r, now)} for r in claims.claim_order(rows, now)]
        finally:
            con.close()
    except sqlite3.Error:
        return None


def dispute_at(db: str | Path, repo: str, pr: int, head: str) -> float | None:
    """When the fixer's latest dispute (#401) at this head was recorded, if its answers comment
    was posted (or may have been: ``uncertain``); None otherwise or when the ledger can't be read.
    Read-only: the watchdog's sweep (#894) decides from it, the dispute's own notice is apart."""
    try:
        con = read_only_connect(db)
        if con is None:
            return None
        try:
            row = con.execute("SELECT MAX(created) FROM disputes WHERE repo=? AND pr=? AND head=? "
                              "AND comment IN ('posted','uncertain')", (repo, pr, head)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return float(row[0]) if row and row[0] is not None else None


def disputed(db: str | Path, repo: str, pr: int, head: str, verdict: dict | None) -> float | None:
    """When the fixer answered ``verdict`` with a dispute (#401): its dispute at this head, newer
    than the verdict; None otherwise. The sweep (#894) and ``explain`` both decide from this."""
    at = dispute_at(db, repo, pr, head)
    return at if at is not None and at > epoch((verdict or {}).get("submitted_at")) else None


def dispute_choices(loop_id: str, pr: int) -> str:
    """The operator's moves on a dispute no adjudicator rules on."""
    return (f"dismiss the review and `hermes dk review --loop {loop_id} --pr {pr}`, `hermes dk "
            f"escalate --loop {loop_id} --pr {pr}`, or merge on its evidence")


def runs_view(con, repo: str | None = None, pr: int | None = None) -> list[dict]:
    """Up to 100 failed, waiting, uncertain, push-policy-cancelled and operator-cancelled runs,
    oldest first. Each row carries ``write``: why it may have written (never re-armed), or None — then ``retry``
    re-arms it (a push-policy-cancelled fixer run under the policy in force at that moment)."""
    # A fixer run the push policy held (cancelled at claim, or held before its turn) is dead
    # for its head until an operator acts, so it is listed too (review on #97); a superseded
    # cancellation (head moved, PR closed) is not, since a new head gets its own turn.
    marks = ','.join('?' * len(run_supervisor.POLICY_CANCELLATIONS))
    held = [run_supervisor.FIXER_NOT_ADMITTED.split(":")[0] + "%", run_supervisor.FIXER_PUSH_REVOKED.split(":")[0] + "%",
            run_supervisor.ISSUE_FIX_OFF + ":%"]
    # An operator's cancel (#1148) is listed as such, with the `retry` that re-arms it.
    where, args = ("(r.state IN ('failed','uncertain','waiting') OR "
                   f"(r.state='cancelled' AND r.error IN ({marks})) OR "
                   "(r.state='cancelled' AND r.error LIKE ?) OR "
                   f"(r.state='failed' AND (r.error LIKE ? OR r.error LIKE ? OR r.error LIKE ?)))"), \
        list(run_supervisor.POLICY_CANCELLATIONS) + [run_supervisor.OPERATOR_CANCEL + "%"] + held
    # A shadow run (#218) is never one to act on: status, explain and the Desktop's stuck list
    # leave it out, and `explain` shows shadow reviews on their own line.
    where += " AND r.seat != 'shadow'"
    if repo is not None:
        where += ' AND r.repo=?'
        args.append(repo)
    if pr is not None:
        where += ' AND r.pr=?'
        args.append(pr)
    # The turn budget (#49) and the dependency prefetch line (#51) are read when the ledger has
    # them; a read-only view never migrates.
    columns = {c[1] for c in con.execute('PRAGMA table_info(runs)')}
    budget = 'r.budget' if 'budget' in columns else 'NULL AS budget'
    deps = 'r.deps' if 'deps' in columns else 'NULL AS deps'
    rows = [dict(row) for row in con.execute(
        "SELECT r.id,r.repo,r.pr,r.head,r.seat,r.turn_key,r.state,r.pid,r.error,"
        f"r.detail,r.retries,r.retry_at,r.outcome,{budget},{deps},n.state AS notice FROM runs r "
        "LEFT JOIN operator_notices n ON n.run_id=r.id WHERE " + where +
        " ORDER BY r.created,r.id LIMIT 100", args)]
    for row in rows:
        row['write'] = run_supervisor.write_evidence(con, row['id'])
    return rows


# The watchdog sweeps every 15 minutes; a listing older than that is not trusted alone to say a
# PR is closed (`retry` confirms with one PR read, #723).
WATCH_INTERVAL_S = 900


def open_listing(loop: dict) -> dict | None:
    """The watchdog's last COMPLETE open-PR listing for the loop: ``{'at', 'numbers'}``, or
    None when no sweep has recorded one. Read from the loop's watch state, never GitHub."""
    try:
        from .. import state as state_mod
        raw = state_mod.state_for(loop).watch().get('open_prs')
    except Exception:
        return None
    if (not isinstance(raw, dict) or isinstance(raw.get('at'), bool)
            or not isinstance(raw.get('at'), (int, float))
            or not isinstance(raw.get('numbers'), list)):
        return None
    return {'at': float(raw['at']),
            'numbers': {n for n in raw['numbers'] if isinstance(n, int) and not isinstance(n, bool)}}


def closed_history(row: dict, listing: dict | None) -> bool:
    """Whether a failed run is history: its PR is absent from the last complete open listing.
    Issue seats are keyed by issue number and never judged by it. No listing: False."""
    return (listing is not None and row.get('state') == 'failed'
            and row.get('seat') not in run_supervisor.ISSUE_SEATS and row.get('pr') not in listing['numbers'])


def split_history(rows: list[dict], loop: dict) -> tuple[list[dict], list[dict], bool]:
    """(rows still to act on, failed rows on closed PRs, whether openness is unknown) for
    ``runs_view`` rows. Nothing is written: ``report`` and ``stats`` still count them failed."""
    listing = open_listing(loop)
    history = [row for row in rows if closed_history(row, listing)]
    return [row for row in rows if row not in history], history, listing is None


def read_only_connect(db: str | Path):
    """A connection that writes nothing — no migration, no WAL creation — or None when the ledger
    is absent. With no live WAL, ``immutable`` keeps even a read-only connection from creating
    -wal/-shm files; with one, those files already exist.

    The report readers go through here, ``runs_view``'s callers included; every read-only open
    is ``ledger.read_only``, so the policy has one home."""
    path = Path(db)
    if not path.is_file():
        return None
    try:
        con = ledger.read_only(path, immutable=not Path(f'{path}-wal').exists())
    except sqlite3.OperationalError:
        if path.exists():
            raise
        return None                       # removed since the check: absent, as above
    con.row_factory = sqlite3.Row
    return con


def read_only_view(db: str | Path, repo: str, pr: int | None = None) -> list[dict] | None:
    """``runs_view`` without writing anything — no schema migration, no WAL creation — for
    ``status``/``explain``. None when the ledger is absent or unreadable."""
    try:
        con = read_only_connect(db)
        if con is None:
            return None
        try:
            return runs_view(con, repo, pr)
        finally:
            con.close()
    except sqlite3.Error:
        return None


def turn_state(db: str | Path, repo: str, pr: int, head: str, seat: str) -> str | None:
    """The newest ledger state of ``seat``'s turn on this PR head, read-only (#98).

    None when the ledger is absent, unreadable or holds no such turn. The watchdog uses it to
    tell a ruling still in flight from a breach marker nobody is working on.
    """
    try:
        con = read_only_connect(db)
        if con is None:
            return None
        try:
            row = newest_turn(con, repo, pr, head, seat)
            return row[0] if row else None
        finally:
            con.close()
    except sqlite3.Error:
        return None


def newest_turn(con, repo: str, pr: int, head: str, seat: str):
    """``(state, turn_key)`` of ``seat``'s newest row at this PR head by ``updated``, or None: the
    row ``turn_state`` reports. ``explain`` reads its key too, to name which of several turns at one
    head it shows (a shadow-fix, CI-fix or conflict row beside the verdict turn, #1615)."""
    return con.execute("SELECT state, turn_key FROM runs WHERE repo=? AND pr=? AND head=? AND "
                       "seat=? ORDER BY updated DESC, created DESC LIMIT 1",
                       (repo, pr, head, seat)).fetchone()


# A turn's own words in the merge check (#753): one clause, bounded under GitHub's 140-character
# status description with the reason it follows (``merge_check.description`` cuts the middle).
TURN_NOTE_MAX = 90


def turn_note(db: str | Path, repo: str, pr: int, head: str, seat: str, held=lambda: None) -> str:
    """The newest ``seat`` turn at this PR head as one clause for the merge check and ``ready``
    (#753): queued (or held by a maintainer: ``held()`` gives the ``pr_hold`` hold, read only for
    a queued turn), running, held (its host-written ``held:`` line, which says when a pacing hold
    resumes), waiting to retry (and why), failed, cancelled or uncertain. "" when there is none,
    it succeeded, or the ledger can't be read: the note never changes what the check decides.

    Newest by ``created``: the attempt that is current. ``turn_state`` orders by ``updated``
    because it asks what last happened at the head; here an older row retired after a newer one
    was queued would speak for the wrong turn (#2314)."""
    from .. import gh, pacing, pr_hold
    try:
        con = read_only_connect(db)
        if con is None:
            return ''
        try:
            row = con.execute("SELECT state, error, retries, retry_at FROM runs WHERE repo=? AND "
                              "pr=? AND head=? AND seat=? ORDER BY created DESC LIMIT 1",
                              (repo, pr, head, seat)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return ''
    if row is None:
        return ''
    state, error, retries, retry_at = row
    error = gh.one_line(error or '', TURN_NOTE_MAX)
    turn = f"{seat} turn"
    if state == 'pending':
        try:
            hold = held()
        except Exception:       # an unreadable hold says nothing; the claim still waits on it
            hold = None
        return f"{turn} {pr_hold.short(hold)}, until released" if hold else f"{turn} queued"
    if state in ('claimed', 'launching', 'running'):
        return f"{turn} running"
    if state == 'waiting' and error.startswith('held: '):
        # The held line names its seat too ("held: reviewer usage window …"): said once.
        return f"{turn} held: {error.removeprefix('held: ').removeprefix(f'{seat} ')}"
    if state == 'waiting':
        at = f", next check {pacing.when(retry_at)}" if type(retry_at) in (int, float) else ''
        return (f"{turn} waiting to retry (attempt {(retries or 0) + 1} of "
                f"{run_supervisor.MAX_RETRIES}{at})" + (f": {error}" if error else ''))
    if state in ('failed', 'cancelled'):
        return f"{turn} {state}: {error or 'no reason recorded'}"
    if state == 'uncertain':
        return f"{turn} outcome uncertain: a person checks it"
    return ''


def ruling_at(db: str | Path, repo: str, pr: int, head: str,
              turn_key: str | None = None) -> str | None:
    """The newest recorded ruling's verdict on this PR head, read-only, or None. With
    ``turn_key``, only the ruling of that turn (one round of a re-armed marker, #1111).

    A ruling ends adjudication: the marker stays ``adjudicating`` at its head (the decision is a
    person's now), so the watchdog and ``explain`` read this to tell "ruled, waiting for you" from
    a ruling that never came.
    """
    try:
        con = read_only_connect(db)
        if con is None:
            return None
        try:
            row = con.execute("SELECT verdict FROM rulings WHERE repo=? AND pr=? AND head=? "
                              + ("AND turn_key=? " if turn_key is not None else "")
                              + "ORDER BY created DESC LIMIT 1",
                              (repo, pr, head, *(() if turn_key is None else (turn_key,)))
                              ).fetchone()
            return str(row[0]) if row else None
        finally:
            con.close()
    except sqlite3.Error:
        return None


def ruling_row(db: str | Path, repo: str, pr: int) -> dict | None:
    """The newest ruling on this PR, at whatever head, read-only, as ``{verdict, created,
    comment, comment_id, head, run_id}``, or None. #905 acts on it only when its head is the PR's
    current one: a later ruling on another head voids an older one, even if the branch returns."""
    keys = ("verdict", "created", "comment", "comment_id", "head", "run_id")
    try:
        con = read_only_connect(db)
        if con is None:
            return None
        try:
            row = con.execute(f"SELECT {', '.join(keys)} FROM rulings WHERE repo=? AND pr=? "
                              "ORDER BY created DESC LIMIT 1", (repo, pr)).fetchone()
            return dict(zip(keys, row)) if row else None
        finally:
            con.close()
    except sqlite3.Error:
        return None


def ruled_approvals(db: str | Path, repo: str, pr: int, head: str) -> tuple[list[dict], str]:
    """``(rows, error)``: how the adjudicator acted on each ACCEPT at this head (#905), one row
    per ruling, oldest first, read-only. No rows and no error: it has not acted. An error is never
    "not acted"."""
    try:
        con = read_only_connect(db)
        if con is None:
            return [], ""
        try:
            cur = con.execute("SELECT * FROM ruled_approvals WHERE repo=? AND pr=? AND head=? "
                              "ORDER BY created, rowid", (repo, pr, head))
            keys = [c[0] for c in cur.description]
            return [dict(zip(keys, row)) for row in cur.fetchall()], ""
        finally:
            con.close()
    except sqlite3.Error as exc:
        if "no such table" in str(exc):
            return [], ""                  # a ledger from before #905: nothing acted on
        return [], f"{type(exc).__name__}: {exc}"

def _recorded_runs(db: str | Path, repo: str, pr: int | None, limit: int, column: str,
                   recorded: str) -> list[dict] | None:
    """The newest runs (at most ``limit``, clamped to 1–20) of ``repo``, and of ``pr`` when
    given, where ``recorded`` holds, as ``id,repo,pr,head,seat,state,<column>,updated`` rows.

    The shared read of ``dependency_view`` and ``view_view``: read-only; None when the ledger is
    absent or unreadable, [] when the ledger predates ``column`` or no row matches.
    """
    try:
        con = read_only_connect(db)
        if con is None:
            return None
        try:
            if column not in {c[1] for c in con.execute('PRAGMA table_info(runs)')}:
                return []
            where, args = "repo=? AND " + recorded, [repo]
            if pr is not None:
                where += " AND pr=?"
                args.append(pr)
            return [dict(row) for row in con.execute(
                f"SELECT id,repo,pr,head,seat,state,{column},updated FROM runs WHERE " + where
                + " ORDER BY updated DESC, id LIMIT ?", (*args, max(1, min(limit, 20))))]
        finally:
            con.close()
    except sqlite3.Error:
        return None


def dependency_view(db: str | Path, repo: str, pr: int | None = None,
                    limit: int = 5) -> list[dict] | None:
    """The newest runs that recorded a dependency prefetch, for ``status``/``explain`` (#51).

    Read-only; None when the ledger is absent or unreadable, [] when nothing was recorded.
    """
    return _recorded_runs(db, repo, pr, limit, 'deps', "deps IS NOT NULL")


# The reason a turn killed at its budget records (#49); ``next_step`` keys on it.
BUDGET_KILL = 'turn budget (sandbox stopped'


def describe_run(row: dict, loop_id: str = 'LOOP') -> str:
    """One operator line: state, reason and the next step for a ``runs_view`` row."""
    text = (f"{row['seat']} #{row['pr']} @ {str(row['head'])[:7]} {row['state']}"
            + (f" ({row['turn_key']})" if row.get('turn_key') else '')
            + f" — {row['error'] or 'no reason recorded'}")
    return f"{text}; {next_step(row, loop_id)}; {hold_explanation(row)['text']}"


# The structured hold reasons a waiting ledger row can carry (#674), keyed by
# ``run_supervisor.waiting_hold``: what it waits for and how it resumes. A time-based hold
# (provider window, daily cap) resumes at its recorded time, which ``next_eligible_start``
# names; an event-based one (CI) names the event in ``waiting_for``. Most CI waits are also
# time-bound: past ``run_supervisor.CI_WAIT_MAX_S`` the host stops holding for CI whether or not
# the event came (``after_event_or_timeout``); only a red head's hold has no limit. Either way
# the host only rechecks it then: a start still depends on host eligibility, and ``next_step``
# stays the one piece of advice.
HOLD_REASONS = {
    'ci': ('ci_wait', 'ci_checks_or_new_head', 'after_event_or_timeout'),
    'ci_failed': ('ci_failed_wait', 'failed_checks_pass_or_new_head', 'after_event'),
    'provider_window': ('provider_window', 'provider_window_reset', 'after_recorded_time'),
    'daily_cap': ('daily_cap', 'daily_cap_reset', 'after_recorded_time'),
}
# ``hold_explanation.text``'s fixed copy, per reason code and per ``waiting_for``. The Desktop
# validator (``validHoldExplanation`` in desktop/plugin.js) rebuilds the text from the same
# tables, so a structured field and its words cannot disagree.
HOLD_COPY = {
    'external_write_guard': 'Recorded run has external write evidence or an uncertain outcome.',
    'run_waiting': 'Recorded run is waiting; the policy-specific reason is not structured yet.',
    'ci_wait': ('Recorded run is held for CI at its recorded head, for at most the CI wait limit; '
                'a new head queues a fresh run instead.'),
    'ci_failed_wait': ('Recorded run is held for failed required checks at its recorded head, '
                       'with no time limit; a new head queues a fresh run instead.'),
    'provider_window': ('Recorded run is held: its provider account is closed (a usage window or '
                        'an unavailable provider).'),
    'daily_cap': "Recorded run is held: its seat's daily turn cap or token budget is reached.",
    'run_failed': 'Recorded run failed.',
    'run_cancelled': 'Recorded run was cancelled.',
    'unknown': 'The ledger does not establish a supported hold explanation.',
}
WAITING_FOR_COPY = {
    'host_recheck': 'host recheck', 'operator_inspection': 'operator inspection',
    'unknown': 'unknown', 'ci_checks_or_new_head': 'CI checks to settle or a new head',
    'failed_checks_pass_or_new_head': 'the failed checks to pass or a new head',
    'provider_window_reset': 'the provider account to reopen',
    'daily_cap_reset': 'the daily cap to reset',
}


def hold_explanation(row: dict) -> dict:
    """Safe explanation of a runs_view artifact (#674, ledger slice).

    State and write evidence are host facts, and write evidence always wins: an uncertain or
    possibly-written row is ``external_write_guard``, never resumed or replayed. A waiting row
    whose recorded line is one of the host's own hold lines (``run_supervisor.waiting_hold``:
    CI, a closed provider account, a daily cap) gets that structured reason; any other error
    prose is NOT a reason-code API and stays ``run_waiting`` with the cause unknown. A recorded
    retry_at is an earliest recheck, never proof that the next turn will start. This formatter
    neither grants retry eligibility nor reads or changes state.
    """
    raw_state = row.get('state')
    state = raw_state if isinstance(raw_state, str) else ''
    uncertain = state == 'uncertain' or row.get('write') is not None
    code = {'waiting': 'run_waiting', 'failed': 'run_failed',
            'cancelled': 'run_cancelled'}.get(state, 'unknown')
    if 'write' not in row:
        code = 'unknown'
    waiting = 'host_recheck' if code == 'run_waiting' else 'unknown'
    automatic = 'unknown'
    if code == 'run_waiting' and not uncertain:
        held = HOLD_REASONS.get(run_supervisor.waiting_hold(row.get('error')))
        if held is not None:
            code, waiting, automatic = held
    if uncertain:
        code, waiting, automatic = 'external_write_guard', 'operator_inspection', 'never'
    text = HOLD_COPY[code]
    due = row.get('retry_at')
    # Bound to datetime's public epoch range as well as JSON's finite numeric domain.
    due = (due if waiting not in ('unknown', 'operator_inspection') and type(due) in (int, float)
           and 0 <= due <= 253402300799 else None)
    head = row.get('head')
    head = head if isinstance(head, str) and re.fullmatch(r'[0-9a-f]{40}', head) else None
    run_id = row.get('id')
    run_id = (run_id if isinstance(run_id, str)
              and re.fullmatch(r'[0-9a-f]{32}', run_id) else None)
    due_text = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(due)) if due is not None else 'unknown'
    text += (f" Ledger only (partial). Waiting for: {WAITING_FOR_COPY[waiting]}. "
             f"Recorded earliest recheck: {due_text}.")
    return {'schema': 1, 'reason_code': code, 'run_id': run_id, 'head': head,
            'evidence_source': 'run_ledger', 'completeness': 'partial',
            'waiting_for': waiting, 'automatic_resume': automatic,
            'next_eligible_start': due, 'operator_action': 'inspect', 'text': text}


def _held_step(row: dict, loop_id: str) -> str:
    """A pacing hold's next step (#619): when its recorded recheck falls, by the clock as well
    as in seconds, and how to run it sooner. An account hold (a usage window or an unavailable
    provider) is reopened by ``retry --clear-hold``; a daily cap by raising it, then ``retry``."""
    from .. import pacing
    error = str(row.get('error') or '')
    due_at = row.get('retry_at')
    due = max(0, int((due_at or 0) - time.time()))
    at = f"at {pacing.when(due_at)} (in {due}s)" if due_at else f"in {due}s"
    step = (f"no retry spent — recorded earliest recheck {at} on the next event or armed "
            "watchdog sweep; a start still depends on host eligibility")
    if run_supervisor.account_hold(error):
        return (f"{step}; to run it sooner once the account is usable again: `hermes dk retry "
                f"--loop {loop_id} --seat {row['seat']} --clear-hold`")
    flag = run_supervisor.hold_remedy(row['seat'], error)
    if flag:
        return (f"{step}; to run it sooner, raise the cap ({flag}), then `hermes dk retry "
                f"--loop {loop_id} --pr {row['pr']} --seat {row['seat']}`")
    return step


def next_step(row: dict, loop_id: str = 'LOOP') -> str:
    """What moves a failed, waiting, uncertain or push-policy-cancelled ``runs_view`` row on
    (#53)."""
    # Presentation must not recommend a reset/re-admission over durable write evidence.
    # The admission policy is unchanged; Supervisor.retry remains authoritative.
    if row.get('write') is not None and row['state'] in ('waiting', 'cancelled'):
        return f"may have written ({row['write']}) — never replayed; inspect external state"
    if row['state'] == 'uncertain' and row.get('write') is None:
        return 'outcome uncertain — never replayed; inspect external state'
    if row['state'] == 'waiting' and str(row.get('error') or '').startswith('held: '):
        return _held_step(row, loop_id)
    if row['state'] == 'waiting':
        due = max(0, int((row['retry_at'] or 0) - time.time()))
        return (f"attempt {(row['retries'] or 0) + 1} of {run_supervisor.MAX_RETRIES} due in {due}s "
                "(recorded earliest recheck on the next event or armed watchdog sweep; "
                "a start still depends on host eligibility)")
    if row['state'] == 'cancelled' and run_supervisor.operator_cancelled(row['error']) and row.get('write') is None:
        # Not a failure (#1148): nothing re-arms it but this, on purpose.
        return (f"cancelled by an operator, no external write — a new head queues a fresh run; "
                f"to re-arm this one: hermes dk retry --loop {loop_id} --pr {row['pr']} "
                f"--seat {row['seat']}")
    if row.get('write') is None and ((row['state'] == 'cancelled' and run_supervisor.policy_cancelled(row['error']))
                                 or run_supervisor.policy_hold(row['error'])):
        # The retry names the row's own seat: `retry --seat fixer` filters an issue_fixer row out.
        if row['seat'] == 'issue_fixer':
            return (f"no external write — issue fixes are off or no longer allowed: they need "
                    f"triage on, a fix label (`hermes dk triage --loop {loop_id} --fix-label "
                    f"LABEL --maintainer LOGIN`) and unattended fixer pushes "
                    f"(`{config.fixer_push_enable_command(loop_id)}`); once they "
                    f"are on, re-admit it: `hermes dk retry --loop {loop_id} --pr "
                    f"{row['pr']} --seat issue_fixer` (a redelivered event never does)")
        return (f"no external write — if unattended fixer pushes are off, turn them on "
                f"(`{config.fixer_push_enable_command(loop_id)}`), then re-admit it: `hermes dk retry --loop "
                f"{loop_id} --pr {row['pr']} --seat {row['seat']}` (an operator retry admits it "
                f"under the policy then in force; a redelivered event never does)")
    if row.get('write') is None:
        rearm = (f"re-arm: hermes dk retry --loop {loop_id} "
                 f"--pr {row['pr']} --seat {row['seat']}")
        if row['state'] == 'failed' and BUDGET_KILL in (row['error'] or ''):
            # The same budget would run out again (#49): raise it first; the re-arm takes it.
            flag = {'reviewer': '--reviewer-turn-budget', 'fixer': '--fixer-turn-budget'}.get(
                row['seat'], '--turn-budget')
            return (f"no external write — raise the turn budget (now "
                    f"{int(row.get('budget') or 0) or '?'}s): hermes dk set --loop "
                    f"{loop_id} {flag} N, then {rearm}")
        return f"no external write — {rearm}"
    if row['state'] == 'failed':
        return f"may have written ({row['write']}) — never replayed; a new head gets a fresh turn"
    return (f"may have written ({row['write']}) — inspect the PR, then "
            f"python -m diaktoros.run_supervisor reconcile DB {row['id']} --reason "
            "REASON --acknowledge-no-live-worker")


def view_view(db: str | Path, repo: str, pr: int | None = None,
              limit: int = 5) -> list[dict] | None:
    """The newest runs whose seat could not see the whole change (#93, #110), for ``explain``.

    Read-only; None when the ledger is absent or unreadable, [] when every recorded view was whole.
    """
    return _recorded_runs(db, repo, pr, limit, 'partial_view',
                          "partial_view IS NOT NULL AND partial_view != ''")


def describe_view(row: dict) -> str:
    """One ``explain`` line for a turn whose seat could not see the whole change."""
    what = ("the broker refuses its approval, so this head can only get REQUEST_CHANGES: "
            "review it by hand (or split the PR) — the loop cannot approve it"
            if row['seat'] == 'reviewer' else "the seat worked from a partial view")
    return (f"{row['seat']} #{row['pr']} @ {str(row['head'])[:7]} {row['state']} — could not see "
            f"the whole change: {row['partial_view']}; {what}")


def describe_dependencies(row: dict) -> str:
    deps = str(row['deps'] or '')
    if deps.startswith('fetching —') and row['state'] not in ('launching', 'running'):
        # A row from before the outcome was always recorded (#361): the prefetch ended with the
        # turn, so never show a finished run as still fetching.
        deps = 'prefetch outcome not recorded (' + deps.split(',', 1)[0] + ')'
    return f"{row['seat']} #{row['pr']} @ {str(row['head'])[:7]} {row['state']} — {deps}"
