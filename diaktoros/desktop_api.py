"""The Desktop page's backend logic (#369, #630, #371): views over one loop, for
``dashboard/plugin_api.py``. Kept free of FastAPI so it is tested without it.

Every read is here and none of them writes: ``stats`` is the run ledger's half of
``hermes dk stats`` (totals and timings only), ``now`` lists live runs with host-written
reasons and, per row, whether a retry would re-arm it, ``overview`` is that same live state for
every configured loop at once (for the status chip and the cross-loop block), ``pr`` adds the
host's own "what moves it" line (``run_supervisor.next_step``) plus what the ledger recorded,
``health`` is ``doctor``'s checks with the network reads left unprobed, and ``settings`` is the
plugin-level form and each loop's effective answers. ``records`` reads bounded local unresolved
failure records, broker history, observer delivery memory and watchdog sweep/head/stall/main-CI
memory (not live health or CI). Never a turn's output, never a run's error
text. ``hooks`` reads GitHub hook state with an opaque confirmation; ``hooks_action`` calls
the shared ``arm_action`` owner used by CLI ``arm`` to pause/resume, then reads actual state.
``action_view`` previews per-target one-shot eligibility; ``action_perform_view`` invokes
the shared ``one_shot_action`` owner and reports exact escalation read-back. Re-review and
drain remain explicit refusals while their core entry points cannot bind a safe confirmation.

**Ledger-only action (#371):** ``retry`` re-arms a run the CLI's ``retry`` would
re-arm. It is not a second implementation of anything: ``retry_action`` owns which run that is
and ``Supervisor.retry`` owns re-arming it, and this function only re-reads the ledger, refuses a
confirmation whose head has moved, and answers with the host's own words. It calls no GitHub API
and reads no token, so the retry it performs is the ledger-only one — the CLI's own fallback when
its PR read is unavailable.
"""
from __future__ import annotations

import errno
import json
import math
import os
import pathlib
import re
import stat
import time

from . import (arm_action, config, doctor, findings, gate_decisions, pacing, retry_action, route_intent,
               run_supervisor, stats)
from .config import settings_form

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


class Conflict(RuntimeError):
    """A retry the host refuses: the ledger says this run is not one to re-arm (#371)."""


class HookRequest(BadRequest):
    """Malformed hook selection (422), without echoing caller-supplied data."""


def _hooks_loop(loop_id: str | None) -> dict:
    if not isinstance(loop_id, str) or not loop_id or len(loop_id) > 200:
        raise HookRequest('A loop id is required.')
    try:
        return _loop(loop_id)
    except Exception:
        raise Conflict('Loop configuration is unavailable; read loop state again.') from None


def hooks_view(loop_id: str | None) -> dict:
    """Current GitHub hook state, with no credential, hook URL or local path in the response."""
    return arm_action.public_plan(arm_action.plan(_hooks_loop(loop_id)))


def hooks_action_view(loop_id: str | None, action: str, confirmation: str) -> dict:
    """Confirm and execute the same host operation as ``arm [--pause]``."""
    if not isinstance(action, str) or action not in ('pause', 'resume'):
        raise HookRequest('Action must be pause or resume.')
    if (not isinstance(confirmation, str) or len(confirmation) != 64 or
            any(c not in '0123456789abcdef' for c in confirmation)):
        raise HookRequest('A valid hook confirmation is required.')
    loop = _hooks_loop(loop_id)
    try:
        return arm_action.confirmed_perform(loop, action, confirmation)
    except arm_action.StaleConfirmation:
        raise Conflict('Hook confirmation is stale or unavailable; read hook state again.') from None


def _action_target(loop_id, action, pr, seat):
    """Validate shape only; every eligibility rule belongs to the shared owner."""
    if action not in ('review', 'escalate', 'drain') or not isinstance(action, str):
        raise BadRequest('Action must be review, escalate or drain.')
    if action == 'drain':
        if pr is not None or seat not in ('reviewer', 'fixer'):
            raise BadRequest('Drain requires a reviewer or fixer seat, not a PR.')
    elif type(pr) is not int or pr <= 0 or seat is not None:
        raise BadRequest('Review and escalation require a positive PR number, not a seat.')
    return _hooks_loop(loop_id)


def action_view(loop_id: str | None, action: str, pr=None, seat=None) -> dict:
    """Read a per-target one-shot offer. Refusal is a valid read, never authorization."""
    from . import one_shot_action
    loop = _action_target(loop_id, action, pr, seat)
    try:
        result = one_shot_action.public_plan(one_shot_action.plan(loop, action, pr=pr, seat=seat))
    except Exception:
        raise Conflict('Action preview unavailable; nothing authorized.') from None
    return dict(result, loop=loop['id'], repo=loop['repo'], read_at=time.time())


def action_perform_view(loop_id: str | None, action: str, pr, seat, confirmation) -> dict:
    """Execute only the exact host offer and return its verified/partial/unknown read-back."""
    from . import one_shot_action
    loop = _action_target(loop_id, action, pr, seat)
    if (not isinstance(confirmation, str) or len(confirmation) != 64 or
            any(c not in '0123456789abcdef' for c in confirmation)):
        raise BadRequest('A valid action confirmation is required.')
    try:
        result = one_shot_action.confirmed_perform(loop, action, confirmation, pr=pr, seat=seat)
    except Exception:
        raise Conflict('Action outcome unavailable; inspect state before retrying.') from None
    if result['status'] in ('stale', 'refused'):
        raise Conflict('Action confirmation is stale or unavailable; preview again.')
    return dict(result, loop=loop['id'], repo=loop['repo'], read_at=time.time())


# The probe reads a credential and calls GitHub, so it answers only a caller on this machine.
# The dashboard server is the one surface Caddy exposes off-host. A forwarding header of ANY kind
# means a proxy is in the path even when the peer looks local (a same-host nginx, a tunnel), and
# presence is the signal, not the value: a header that is present but empty was still set by
# something in front of us (#648 review, F1).
LOCAL_HOSTS = ("127.0.0.1", "::1", "localhost")
# `Forwarded`, every `X-Forwarded-*` (For, Host, Proto, Port, ...) and `X-Real-IP`: a proxy that sets
# only X-Forwarded-Proto is still a proxy, so the family is matched by prefix, not by a fixed list.
FORWARDING_HEADERS = ("forwarded", "x-real-ip")
FORWARDING_PREFIXES = ("x-forwarded-",)


def _is_forwarding_header(name) -> bool:
    # Raw ASGI header names are bytes; str(b"x") is the repr "b'x'", which would never match.
    if isinstance(name, (bytes, bytearray)):
        name = bytes(name).decode("latin-1")
    name = str(name).lower()
    return name in FORWARDING_HEADERS or name.startswith(FORWARDING_PREFIXES)


def _header_names(headers):
    """The names in ``headers``: a mapping's keys (a dict, Starlette ``Headers``) or the first
    element of each ``(name, value)`` pair, which is how the ASGI scope carries them."""
    if hasattr(headers, "keys"):
        return list(headers.keys())
    return [item[0] if isinstance(item, (tuple, list)) and item else item for item in headers]


def probe_is_local(client_host: str | None, headers=None) -> bool:
    """Whether this caller may run the credential-reading probe: a local peer, and no forwarding
    header of any kind. An empty client_host (an in-process call) counts as local; a header that
    is present at all — even an empty one — does not."""
    if headers is not None and any(_is_forwarding_header(name) for name in _header_names(headers)):
        return False
    return not client_host or client_host in LOCAL_HOSTS


def loops() -> list[dict]:
    """Every configured loop the page can show: id and repository only.

    **A loop whose file does not load is listed too**, with ``unreadable`` true, no repo and a
    bounded ``reason`` (#701). It must be *named* here: the page decides "not read" by comparing
    this list against ``/overview``, so a loop dropped from both would vanish from the chip
    instead of showing as a loop that cannot be read. Every entry carries the same keys.
    """
    found, unreadable = config.readable_loops()
    return [{"id": loop["id"], "repo": loop["repo"], "unreadable": False, "reason": ""}
            for loop in found] + [
        {"id": str(loop_id), "repo": "", "unreadable": True, "reason": _bounded(reason)}
        for loop_id, reason in unreadable]


def _loop(loop_id: str | None) -> dict:
    found, unreadable = config.readable_loops()
    if loop_id:
        for loop in found:
            if loop["id"] == loop_id:
                return loop
        for bad_id, reason in unreadable:
            if bad_id == loop_id:
                raise NotFound("loop config unreadable: " + _bounded(reason))
        raise NotFound("no such loop")
    # An unreadable loop is still a configured one: with one present the default is ambiguous,
    # so the caller is told to name a loop rather than silently handed the one that loads.
    if len(found) != 1 or unreadable:
        names = [loop["id"] for loop in found] + [f"{bad} (unreadable)" for bad, _ in unreadable]
        raise NotFound("name a loop: " + (", ".join(names) or "none"))
    return found[0]


# Local diagnostic artifacts are not safe to pass through their permissive CLI readers:
# those readers intentionally fall back to empty and use unbounded read_text(). Reuse their
# path owners and writer contracts, but keep this admission/read/validation boundary explicit.
RECORDS_MAX = 50
RECORDS_BYTES = 256 * 1024


class _RecordRead(Exception):
    def __init__(self, reason):
        self.reason = reason


def _record_bytes(path, *, tail=False):
    """No-follow at EVERY component, nonblocking, regular files only, bounded bytes.

    A JSONL tail may start mid-line; discard that fragment, never a complete malformed line.
    No locks (writers atomically replace JSON and append JSONL), no directory enumeration.
    """
    path = pathlib.Path(path).absolute()
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            if part in ('.', '..'):
                raise _RecordRead('unsafe_file')
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                          os.O_NONBLOCK, dir_fd=fd)
            os.close(fd)
            fd = nxt
        leaf = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(leaf)
            if not stat.S_ISREG(info.st_mode):
                raise _RecordRead('unsafe_file')
            if not tail and info.st_size > RECORDS_BYTES:
                raise _RecordRead('too_large')
            start = max(0, info.st_size - RECORDS_BYTES) if tail else 0
            raw = os.pread(leaf, RECORDS_BYTES + (0 if tail else 1), start)
            if len(raw) > RECORDS_BYTES:
                raise _RecordRead('too_large')
            if start:
                # A previous byte determines whether the first line is complete.
                if os.pread(leaf, 1, start - 1) != b'\n':
                    if b'\n' not in raw:
                        raise _RecordRead('too_large')
                    raw = raw.split(b'\n', 1)[1]
            return raw
        finally:
            os.close(leaf)
    except FileNotFoundError:
        raise _RecordRead('not_recorded') from None
    except OSError as exc:
        reason = 'unsafe_file' if exc.errno in (errno.ELOOP, errno.ENOTDIR) else 'read_failed'
        raise _RecordRead(reason) from None
    finally:
        os.close(fd)


def _record_section(reader):
    try:
        return {'status': 'available', 'reason': 'recorded', 'items': reader()}
    except _RecordRead as exc:
        return {'status': 'missing' if exc.reason == 'not_recorded' else 'unreadable',
                'reason': exc.reason, 'items': []}
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return {'status': 'unreadable', 'reason': 'malformed', 'items': []}
    except Exception:
        return {'status': 'unreadable', 'reason': 'read_failed', 'items': []}


def _record_json(path):
    return json.loads(_record_bytes(path))


def _record_number(value, *, integer=False, nullable=False):
    if nullable and value is None:
        return None
    if (type(value) not in ((int,) if integer else (int, float)) or
            value < 0 or value > 10**15 or not math.isfinite(value)):
        raise ValueError('number')
    return value


def _record_sha(value):
    if not isinstance(value, str) or (value and not re.fullmatch(r'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})', value)):
        raise ValueError('head')
    return value


def _record_enum(value, allowed):
    if not isinstance(value, str):
        raise ValueError('category')
    return value if value in allowed else 'unknown'


def _record_failures(loop):
    from . import gate_failures
    owners = [gate_failures.loop_ledger(loop), gate_failures.fallback_ledger()]
    found, present = {}, False
    for index, owner in enumerate(owners):
        try:
            data = _record_json(owner.path)
        except _RecordRead as exc:
            if exc.reason == 'not_recorded':
                continue
            raise
        present = True
        if not isinstance(data, dict) or len(data) > 1000:
            raise ValueError('entries')
        for key, entry in data.items():
            if (not isinstance(entry, dict) or not key or key.startswith('_') or
                    type(entry.get('resolved')) is not bool):
                raise ValueError('entry')
            if entry['resolved']:
                continue
            # Fallback belongs to many loops: never leak a different repository's failures.
            if index and entry.get('repo') != loop['repo']:
                continue
            if not re.fullmatch(r'(?:[0-9a-f]{16}|ledger-[0-9a-f]{12})', key):
                raise ValueError('identity')
            item = {'id': key,
                    'gate': _record_enum(entry.get('gate'),
                                         ('gate_reviewer', 'gate_fixer', 'gate_adjudicator', 'ledger')),
                    'kind': _record_enum(entry.get('kind'),
                                         ('crash', 'timeout', 'stopped', 'incomplete', 'deferred', 'ledger')),
                    'pr': _record_number(entry.get('pr'), integer=True, nullable=True),
                    'head': _record_sha(entry.get('head', '')),
                    'first_at': _record_number(entry.get('first_at')),
                    'last_at': _record_number(entry.get('last_at')),
                    'attempts': _record_number(entry.get('attempts'), integer=True),
                    'redrives': _record_number(entry.get('redrives', 0), integer=True)}
            found.setdefault(key, item)
    if not present:
        raise _RecordRead('not_recorded')
    return sorted(found.values(), key=lambda item: (item['last_at'], item['id']), reverse=True)[:RECORDS_MAX]


def _record_broker(loop):
    raw = _record_bytes(config.state_dir(loop) / 'broker-audit.jsonl', tail=True)
    if raw and not raw.endswith(b'\n'):
        raise ValueError('incomplete append')
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            raise ValueError('empty audit line')
        entry = json.loads(line)
        if not isinstance(entry, dict) or not isinstance(entry.get('repo'), str):
            raise ValueError('audit')
        if entry['repo'] != loop['repo']:
            continue
        rows.append({'at': _record_number(entry.get('at')),
                     'pr': _record_number(entry.get('pr'), integer=True),
                     'head': _record_sha(entry.get('head', entry.get('old_head', ''))),
                     'role': _record_enum(entry.get('role'),
                                          ('reviewer', 'fixer', 'triage', 'adjudicator', 'issue_fixer',
                                           'review_only_update')),
                     'operation': _record_enum(entry.get('operation'),
                                               ('review', 'push', 'answers', 'labels', 'comment',
                                                'open_pr', 'request_review', 'issue_comment',
                                                'file_issue', 'ruling_comment', 'issue_branch',
                                                'update_branch')),
                     'phase': _record_enum(entry.get('phase', 'recorded'),
                                           ('recorded', 'attempt', 'reconciled')),
                     'outcome': _record_enum(entry.get('outcome', 'recorded' if 'phase' not in entry
                                                       else 'unknown'),
                                             ('recorded', 'published', 'not_published', 'unchanged', 'unknown',
                                              'published_pr_unverified'))})
    # File order, not wall clock, identifies the latest append (clocks can go backward).
    return list(reversed(rows[-RECORDS_MAX:]))


def _record_main_ci(loop):
    from . import main_check
    data = _record_json(config.state_dir(loop) / main_check.FILE)
    if not isinstance(data, dict) or not data or not set(data) <= {'green', 'notified'}:
        raise ValueError('main memory')
    if (any(not data[key] for key in data) or
            ('required_checks' in loop and not isinstance(loop['required_checks'], list))):
        raise ValueError('main configuration')
    required = config.required_checks(loop)
    if len(required) > RECORDS_MAX:
        raise _RecordRead('too_large')
    # Check names are configuration labels, not prose/error text. Never expose paths, URLs,
    # credential-shaped values or controls, even in hand-edited configuration.
    for name in required:
        if (not isinstance(name, str) or not name or len(name) > 100 or
                any(ord(c) < 32 for c in name) or
                re.search(r'gh[pousr]_|github_pat_|sk-|://|^/|/home/|/etc/|\\\\', name)):
            raise ValueError('required checks')
    return [{'green': _record_sha(data.get('green', '')),
             'notified': _record_sha(data.get('notified', '')),
             'required_checks': required}]


def _record_observer(loop):
    from . import observer
    data = _record_json(config.state_dir(loop) / 'observations.json')
    if not isinstance(data, dict) or not isinstance(data.get('entries'), dict):
        raise ValueError('observer entries')
    if len(data['entries']) > 1000:
        raise _RecordRead('too_large')
    rows = []
    for entry in data['entries'].values():
        if not isinstance(entry, dict):
            raise ValueError('observer entry')
        number = entry.get('number')
        # Digests have no single number; reports and main notices use zero.
        number = None if number in ('', None) else _record_number(number, integer=True)
        issue = entry.get('issue')
        if issue is not None and type(issue) is not bool:
            raise ValueError('observer issue')
        rows.append({'event': _record_enum(entry.get('event'), (*observer.EVENTS, 'digest', 'report')),
                     'number': number, 'head': _record_sha(entry.get('head', '')),
                     'status': _record_enum(entry.get('status'),
                                           ('queued', 'digesting', 'pending', 'delivered',
                                            'failed', 'uncertain')),
                     'at': _record_number(entry.get('at')),
                     'attempts': _record_number(entry.get('attempts'), integer=True),
                     'issue': issue})
    # No key/identity, summary, outcome, destination, message, URL or delivery id escapes.
    return sorted(rows, key=lambda row: row['at'], reverse=True)[:RECORDS_MAX]


def _record_watchdog(loop):
    """A small projection of sweep memory, NOT daemon health or a current stall verdict.

    Arbitrary queue/lock alert keys and base branch names are deliberately not projected.
    Cooldown keys contain only seven head characters, so never join them to a current head.
    """
    from datetime import datetime
    data = _record_json(config.state_dir(loop) / 'watchdog.json')
    if not isinstance(data, dict):
        raise ValueError('watchdog memory')
    rows = []
    if 'last_run' in data:
        stamp = data['last_run']
        if (not isinstance(stamp, str) or not re.fullmatch(
                r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)', stamp)):
            raise ValueError('sweep timestamp')
        rows.append({'kind': 'sweep', 'at': _record_number(
            datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp())})
    for source, kind in (('heads', 'head'), ('head_history', 'head_history')):
        entries = data.get(source, {})
        if not isinstance(entries, dict):
            raise ValueError('watchdog heads')
        if len(entries) > 1000:
            raise _RecordRead('too_large')
        heads = []
        for key, entry in entries.items():
            number = key.split(':', 1)[0] if source == 'head_history' else key
            if not re.fullmatch(r'[1-9][0-9]{0,14}', number) or not isinstance(entry, dict):
                raise ValueError('watchdog head')
            heads.append({'kind': kind, 'pr': _record_number(int(number), integer=True),
                          'head': _record_sha(entry['sha']),
                          'observed_at': _record_number(entry['observed_at'], nullable=True),
                          'at': _record_number(entry['last_seen_at'])})
        rows.extend(sorted(heads, key=lambda row: row['at'], reverse=True)[:RECORDS_MAX])
    alerts = data.get('alerts', {})
    if not isinstance(alerts, dict):
        raise ValueError('watchdog alerts')
    if len(alerts) > 1000:
        raise _RecordRead('too_large')
    stalls = []
    for key, stamp in alerts.items():
        at = _record_number(stamp)
        if key.startswith(('queue:', 'lock:')):
            continue
        match = re.fullmatch(r'([1-9][0-9]{0,14}):([0-9a-fA-F]{7}):(.{1,24})', key)
        if not match:
            raise ValueError('watchdog stall key')
        raw_kind = match[3]
        category = 'unknown'
        for prefix, label in (('reviewer never posted', 'reviewer_missing'),
                              ('fixer never pushed', 'fixer_missing'),
                              ('parked awaiting adjudica', 'awaiting_adjudication'),
                              ('adjudicating ', 'adjudicating'),
                              ('fixer held — unattended', 'fixer_push_disabled'),
                              ('# verdicts, no approval', 'missing_escalation'),
                              ('disputed ', 'disputed')):
            if raw_kind.startswith(prefix):
                category = label
                break
        stalls.append({'kind': 'stall', 'pr': _record_number(int(match[1]), integer=True),
                       'head7': match[2], 'category': category, 'at': at})
    rows.extend(sorted(stalls, key=lambda row: row['at'], reverse=True)[:RECORDS_MAX])
    # A global cap keeps the combined response bounded as well as each source above.
    return sorted(rows, key=lambda row: row['at'], reverse=True)[:RECORDS_MAX]


def records_view(loop_id: str | None) -> dict:
    """Bounded LOCAL records only; no GitHub reads, tokens, bodies or raw failure text.

    Main-CI is the watchdog's recorded green/notified heads, not current CI conclusions.
    Observer statuses are recorded claims/receipts, not independently verified chat delivery.
    Watchdog heads may be retained after a PR disappears; stall marks are cooldown memory,
    not current stalls or evidence that a watchdog process is healthy. No log prose is read.
    A quiet gate decline has no failure record; these are unresolved failures only.
    """
    if loop_id is not None and (not isinstance(loop_id, str) or not loop_id or len(loop_id) > 200):
        raise BadRequest('A valid loop id is required.')
    try:
        loop = _loop(loop_id)
    except Exception:
        raise NotFound('Loop configuration unavailable.') from None
    sections = {'failures': _record_section(lambda: _record_failures(loop)),
                'broker': _record_section(lambda: _record_broker(loop)),
                'main_ci': _record_section(lambda: _record_main_ci(loop)),
                'observer': _record_section(lambda: _record_observer(loop)),
                'watchdog': _record_section(lambda: _record_watchdog(loop))}
    return {'loop': loop['id'], 'repo': loop['repo'], 'read_at': time.time(), **sections}


def stats_view(loop_id: str | None, since: str = "7d") -> dict:
    if since not in SINCE_CHOICES:
        raise BadRequest(f"since must be one of {', '.join(SINCE_CHOICES)}")
    loop = _loop(loop_id)
    return stats.collect(loop, run_supervisor.production_ledger(), stats.parse_since(since),
                         with_github=False)


def now_view(loop_id: str | None) -> dict:
    loop = _loop(loop_id)
    ledger = run_supervisor.production_ledger()
    view = stats.now(ledger, loop["repo"])
    # A missing ledger is not a verified zero: ``stats.now`` returns None when there is no
    # ledger, so the view carries ``available`` and the page renders unavailable, never "nothing
    # running". The empty runs and counts below are placeholders for that case only.
    available = view is not None
    if view is None:
        view = {"repo": loop["repo"], "runs": [], "counts": {}}
    view["available"] = available
    # Per row, the one question the page's Retry button asks: would ``retry`` re-arm this run?
    # Answered by the same plan the POST and the CLI run, so a button the page draws is a button
    # the host would honour — and no row gets one for a run that may have written.
    view["runs"] = _with_retry(ledger, loop["repo"], view["runs"], loop)
    # Beside the runs, the live usage-window state: a run's own held line is a snapshot from the
    # moment it was held, and it can outlive the window it names (#650).
    view["pacing"] = pacing.seats(loop)
    return view


def _with_retry(db, repo: str, runs: list[dict], loop: dict) -> list[dict]:
    """Each ``/now`` row with ``retry``: whether a retry would re-arm it, its head, and why not.

    The verdict is ``retry_action``'s — never a state the page matches on its own. A row with no
    ledger row behind it (or an unreadable ledger) carries ``eligible`` false with the reason,
    so the page shows no button rather than a button that would fail.
    """
    def without(reason: str) -> list[dict]:
        return [dict(run, retry={"eligible": False, "head": "", "reason": reason})
                for run in runs]
    out = without("no run ledger to read")
    if not runs:
        return out
    try:
        con = run_supervisor.read_only_connect(db)
        if con is None:
            return out
        try:
            found = retry_action.page_targets(con, repo, loop)
        finally:
            con.close()
    except Exception:
        # A read that failed is not the same answer as a ledger that is not there, and neither
        # offers an action: the reason says which, so no row grows a button on a guess.
        return without("the retry state could not be read")
    for run in out:
        entry = found.get((run["pr"], run["seat"], run["head"], run["state"]))
        if entry is not None:
            run["retry"] = {"eligible": bool(entry["eligible"]),
                            "head": str(entry["head"] or ""),
                            "reason": _bounded(entry["reason"])}
    return out


def retry_view(loop_id: str | None, pr: int, seat: str, head: str | None = None) -> dict:
    """Re-arm the run ``hermes dk retry --loop … --pr … --seat …`` would re-arm (#371).

    This is the page's one write. It re-reads the ledger and decides from that alone — the
    client's view is never trusted beyond ``head``, which is only a *confirmation*: when the head
    that would be re-armed is no longer the head the operator saw, the retry is refused with
    ``Conflict`` instead of silently re-arming a different run. Eligibility, the refusals and the
    re-arm itself are ``retry_action.perform``'s, which is the CLI's code path; nothing here
    calls GitHub, reads a token or invents a second rule. The response carries the host's own
    answer line and the state read back from the ledger after the write, so the page shows what
    happened rather than what it asked for.
    """
    if isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0:
        raise BadRequest("pr must be a positive integer")
    seat = str(seat or "")
    if seat not in retry_action.SEATS:
        raise BadRequest("seat must be one of " + ", ".join(retry_action.SEATS))
    head = str(head or "").strip().lower()
    if not head:
        raise BadRequest("head is required: the retry re-arms the head the operator confirmed")
    if len(head) < 7 or len(head) > 40 or any(c not in "0123456789abcdef" for c in head):
        raise BadRequest("head must be a commit SHA (7 to 40 hex characters)")
    loop = _loop(loop_id)
    ledger = run_supervisor.production_ledger()
    result = retry_action.perform(loop, pr, seat, ledger=ledger, confirm_head=head or None)
    view = {"loop": loop["id"], "repo": loop["repo"], "pr": pr, "seat": seat,
            "head": result["head"], "head7": result["head7"],
            "rearmed": [{"seat": e["seat"], "head7": e["head7"], "state": e["state"]}
                        for e in result["rearmed"]],
            "refused": [{"seat": e["seat"], "head7": e["head7"], "state": e["state"],
                         "reason": retry_action.bounded(e["reason"])} for e in result["refused"]],
            "worker": retry_action.bounded(result["worker"]),
            "answer": retry_action.answer(result),
            "runs": _runs_after(ledger, loop["repo"], pr), "read_at": time.time()}
    # ``perform``'s no-ledger line names the file, which a terminal wants and a page must not
    # render (the same rule the read views follow): the page gets the fact, not the path.
    refusal = retry_action.bounded(str(result["message"]).replace(str(ledger), "the run ledger"))
    if result["message"]:
        raise Conflict(refusal)
    if not result["rearmed"]:
        raise Conflict(retry_action.answer(result))
    return view


def _runs_after(db, repo: str, pr: int) -> list[dict]:
    """This PR's ledger rows as they stand now — the read-back a successful retry answers with."""
    try:
        con = run_supervisor.read_only_connect(db)
        if con is None:
            return []
        try:
            rows = retry_action.rows_for(con, repo, pr)
        finally:
            con.close()
    except Exception:
        return []
    return [{"seat": row["seat"], "head7": str(row["head"] or "")[:7], "state": row["state"]}
            for row in rows]


def overview_view() -> dict:
    """Every configured loop's live run state, in one read.

    The status chip and the cross-loop block answer "what is waiting on me" across all loops
    without one request per loop. Per loop: its id and repo, the live run counts and the
    uncertain ("needs you") count when the ledger could be read, and whether it could be read at
    all — ``available`` false when there is no ledger, ``error`` true when the read raised. A
    missing ledger is unavailable, never a verified zero, exactly as ``/now`` reports it. One
    loop failing is not the others' failure: it is reported on its own entry. A loop whose config
    file does not load has no repo and no ledger to read: it is listed with ``unreadable`` true
    and a bounded ``reason`` (``available`` false, ``error`` true, no counts), so the page names
    it instead of dropping it (#701).
    """
    found, unreadable = config.readable_loops()
    db = run_supervisor.production_ledger()
    entries = []
    for loop in found:
        entry = {"id": loop["id"], "repo": loop["repo"], "available": True, "error": False,
                 "unreadable": False, "reason": "", "counts": {}, "needs_you": 0}
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
    entries += [{"id": str(loop_id), "repo": "", "available": False, "error": True,
                 "unreadable": True, "reason": _bounded(reason), "counts": {}, "needs_you": 0}
                for loop_id, reason in unreadable]
    return {"loops": entries, "read_at": time.time()}


def health_view(loop_id: str | None, probe: bool = False, client_host: str | None = None,
                headers=None) -> dict:
    """``doctor``'s checks for one loop, read-only.

    Offline by default: every check that would need the network — the gateway probe, the hooks,
    token expiry and scopes, the labels — reports itself unprobed instead of reaching out, so
    ``probe`` runs the full set — the Desktop page asks for it by default, from this machine only:
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
                  "stuck": [], "turns": [], "turns_total": 0, "verdicts": {}, "rulings": [],
                  "rulings_total": 0, "breach": None, "ledger_available": False,
                  "findings": [], "decisions": [], "shadows": [], "shadows_older": 0,
                  "read_at": time.time()}
    # The loop's own state files, not the ledger: what the reviewer raised, and why each gate
    # did or did not act. Both read-only, both local.
    view["findings"] = _findings(loop, pr)
    view["decisions"] = _decisions(loop, pr)
    db = run_supervisor.production_ledger()
    con = run_supervisor.read_only_connect(db)
    if con is None:                                 # no ledger: unavailable, not verified empty
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
            view["stuck"].append({"seat": row["seat"], "head": _recorded_head(row["head"]),
                                  "state": row["state"], "since": created,
                                  "next": run_supervisor.next_step(row, loop["id"]),
                                  "explanation": run_supervisor.hold_explanation(row)})
        # The newest TURNS_MAX rows, shown oldest first: the latest turns are the ones that say
        # whether the PR is still moving. ``turns_total`` lets the page say the list is cut.
        view["turns_total"] = _count(con, "runs", repo, pr)
        for row in reversed(con.execute(
                "SELECT seat, head, state, created, updated FROM runs WHERE repo=? AND pr=? "
                "ORDER BY created DESC, id DESC LIMIT ?", (repo, pr, TURNS_MAX)).fetchall()):
            view["turns"].append({"seat": row["seat"], "head": _recorded_head(row["head"]),
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
        view["rulings_total"] = _count(con, "rulings", repo, pr)
        for row in reversed(con.execute(
                "SELECT verdict, head, created FROM rulings WHERE repo=? AND pr=? "
                "ORDER BY created DESC, rowid DESC LIMIT ?", (repo, pr, TURNS_MAX)).fetchall()):
            view["rulings"].append({"verdict": str(row["verdict"] or "")[:80],
                                    "head": _recorded_head(row["head"]), "created": row["created"]})
    finally:
        con.close()
    view["breach"] = _breach(loop, pr)
    view["pacing"] = pacing.seats(loop)
    # Shadow reviews (#218): a second model's would-be verdicts, recorded and never posted.
    from . import stats
    view["shadows"], view["shadows_older"] = stats.shadow_reviews_for(db, repo, pr)
    view["ledger_available"] = True
    return view


def _recorded_head(value) -> str:
    """A safe recorded identity, or unavailable; never echo malformed ledger contents."""
    return value[:7] if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40}', value) else ''


def _count(con, table: str, repo: str, pr: int) -> int:
    """How many ledger rows ``table`` (``runs`` or ``rulings``) holds for this PR, so a list cut
    at TURNS_MAX can say how many there are."""
    assert table in ("runs", "rulings")
    return int(con.execute(f"SELECT COUNT(*) FROM {table} WHERE repo=? AND pr=?",
                           (repo, pr)).fetchone()[0])


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

    * **Credentials are paths, never contents.** PAT-only ``token_file`` and App-only
      ``key_file`` name the credential without opening it; ``credential_kind`` and ``app_id``
      distinguish them. Form choices may be partial; effective loop credentials are typed.
    * **An unreadable settings store is unavailable, not a clean default** — ``available`` is
      false and ``reason`` says why, so the page cannot present schema defaults as if the
      operator had chosen them. A store that reads fine but holds nothing is available,
      all-default, and says so.
    * **One loop's failure is not the others'**: an unreadable loop is reported on its own entry.
    """
    settings, available, reason = _form_settings()
    defaults = settings_form.settings_defaults(settings)
    return {
        "plugin": {
            "available": available,
            "reason": reason,
            "schema": {
                key: {"label": spec.get("label", key), "type": spec.get("type", "str"),
                      "default": spec.get("default"),
                      "description": spec.get("description", "")}
                for key, spec in settings_form.SETTINGS_SCHEMA.items()
            },
            "values": {key: defaults.get(key) for key in settings_form.SETTINGS_SCHEMA},
            "sources": {key: ("set" if str(settings.get(key, "")) not in ("", "None")
                              else "default")
                        for key in settings_form.SETTINGS_SCHEMA},
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
            **_form_credential(entry),
            "set": bool(entry),
        }
    return out


def _form_credential(entry: dict) -> dict:
    """Unapplied form choices, including partial App fields; never resolve a credential."""
    app_id = entry.get("app_id") or ""
    key_file = entry.get("app_key_file") or ""
    token_file = entry.get("token_file") or ""
    return {"credential_kind": "app" if app_id or key_file else "pat" if token_file else "none",
            "app_id": str(app_id), "key_file": key_file, "token_file": token_file}


def _loop_settings() -> list[dict]:
    """What each loop runs as today: effective seats, budgets, step caps and flags.

    **Every configured loop answers**, including one whose file cannot be read at all: that loop is
    reported on its own entry, with the reason, and never suppresses the loops that do load — the
    rule ``overview`` follows for the ledger. A loop missing from this list is a loop the page
    cannot report at all, which is the one thing a status surface must never do.
    """
    found, unreadable = config.readable_loops()
    return [_loop_entry(loop) for loop in found] + [
        {"id": str(loop_id), "repo": "", "base": "", "error": True,
         "reason": _bounded(reason), "seats": {}, "flags": {}}
        for loop_id, reason in unreadable]


def _loop_entry(loop: dict) -> dict:
    """One loop's effective answers, or its own error entry when a reader raised for it.

    Every entry carries the same keys whatever happened, so a renderer can show a loop that
    answered and a loop that did not without a second code path — and a partial entry can never be
    mistaken for a loop with no seats.
    """
    entry = {"id": str(loop.get("id", "")), "repo": str(loop.get("repo", "")),
             "base": str(loop.get("base") or ""), "error": False, "reason": "",
             "seats": {}, "flags": {}}
    try:
        entry["seats"] = {role: _seat_line(loop, role) for role in config.ROUTE_ROLES}
        entry["flags"] = _loop_flags(loop)
    except Exception as exc:                    # one broken loop must not blank the others
        entry["error"] = True
        entry["reason"] = _bounded(type(exc).__name__)
    return entry


def _bounded(text: object, limit: int = 200) -> str:
    """One line of diagnostic text: whitespace collapsed, capped, never a traceback.

    A loop's load failure carries the config's own message (the same one ``hermes dk list`` prints),
    and this is what keeps that to a bounded category on a page: no traceback, no second line.
    """
    return " ".join(str(text).split())[:limit]


def _seat_line(loop: dict, role: str) -> dict:
    """One seat's effective identity for a loop: who serves it, as what login, woken by which route.

    Read through the helpers the CLI and the gate shims read — ``seat_profile``/``seat_login``/
    ``adjudicator_login`` for the identity, ``route_intent.routes_of`` for the route the seat owns,
    ``turn_budget``/``max_steps`` for its limits — so the page cannot answer differently from
    ``hermes dk show``. Those readers resolve the real shape: a seat's identity lives in
    ``loop["seats"][role]`` (the adjudicator's profile and route live at the loop's top level),
    never at ``loop[role]``.
    """
    login = config.seat_login(loop, role)
    if role == "adjudicator":
        login = login or config.adjudicator_login(loop)
    return {
        "profile": config.seat_profile(loop, role),
        "login": login,
        **_seat_credential(loop, login),
        "route": route_intent.routes_of(loop).get(role) or "",
        "turn_budget_s": config.turn_budget(loop, role),
        "max_steps": config.max_steps(loop, role),
    }


def _seat_credential(loop: dict, login: str) -> dict:
    """Typed effective credential metadata; a PEM path is never a PAT token_file."""
    from . import gh

    result = {"credential_kind": "none", "app_id": None, "key_file": "", "token_file": ""}
    mapped = config.credentials_map(loop)
    actual = login if login in mapped else next(
        (name for name in mapped if name.casefold() == login.casefold()), "")
    if not login or not actual:
        return result
    path = gh.credential_path(loop, actual)
    if path is None:
        return result
    app = (loop.get("apps") or {}).get(actual)
    if app:
        result.update(credential_kind="app", app_id=app["app_id"], key_file=str(path))
    else:
        result.update(credential_kind="pat", token_file=str(path))
    return result


def _loop_flags(loop: dict) -> dict:
    """The on/off settings the page shows as chips. Absent is off, and off is reported as off.

    Keyed by the names the loop file actually carries (``fix_ci``, ``review_after_ci``,
    ``fixer_check``): a flag read under a name nothing writes is a chip that is always off, which
    is exactly the kind of quiet wrong answer this projection exists to avoid.
    """
    return {
        "review_after_ci": bool(loop.get("review_after_ci")),
        "fixer_check": loop.get("fixer_check") or "",
        "fix_ci": bool(loop.get("fix_ci")),
        "cap": loop.get("cap") or settings_form.SETTINGS_SCHEMA["cap"]["default"],
        "base": loop.get("base") or settings_form.SETTINGS_SCHEMA["base"]["default"],
    }
