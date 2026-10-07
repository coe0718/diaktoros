"""Per-loop state on disk: seat locks, the queue, in-flight marks, breach markers, and the
observer's delivery ledger.

All of it lives under the loop's own ``state_dir`` (default
``~/.hermes/state/review-loops/<id>/``), so two loops never share a file and a loop can
be deleted by removing one directory.

Two rules the shapes below encode:

* **A seat is a capacity, not a mutex.** ``concurrency`` says how many PRs that seat may work at
  once (1 = serialized). The ledger is keyed by PR so one PR can never run twice, and it expires,
  because a crashed run must not wedge a loop forever.
* **One wake per head.** Every marker is keyed by PR *and* head sha: a new commit is a new
  situation, the same commit is not.

Every file is written atomically (temp file, fsync, ``os.replace``) and every read-modify-write
of the ledger, the queue and the in-flight marks happens under one per-loop ``flock``. Gates run
as separate processes per webhook; without both, a reader could see a half-written file, fall
back to ``{}``, and the next save would silently drop every other PR's claim.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import pathlib
import tempfile
import threading
import time
import uuid
from collections.abc import Callable

from . import config, hostdirs
from .util import log, now_iso

# Per-thread depth of the state lock we already hold, keyed by lock path. ``flock`` is tied to
# the open file description, so a second ``open`` + ``flock`` in the same thread would deadlock
# against itself; nested sections (``take_seat`` queueing under its own claim) reuse the outer one.
_HELD = threading.local()

# Seat claims this process made, as ``(state, seat, key, at)``. A gate that crashes or runs out
# of time after claiming a seat releases exactly these on its way out (#75, ``gate_failures``),
# so a failed delivery never holds the seat until ``ttl_min`` expires.
CLAIMS: list = []


def release_process_claims() -> list[str]:
    """Free every seat claim this process made that is still the one on disk; name them."""
    freed = []
    while CLAIMS:
        st, seat, key, at = CLAIMS.pop()
        try:
            if st.release_exact(seat, key, at):
                freed.append(f"{seat}:{key}")
        except Exception as exc:  # noqa: BLE001 - best effort; the TTL remains the backstop
            log(f"could not release {seat} claim on {key}: {type(exc).__name__}: {exc}")
    return freed


def _atomic_write(path: pathlib.Path, data) -> None:
    """Publish ``data`` to ``path`` whole or not at all, and durably before returning."""
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=f".{path.stem}-",
                                         delete=False) as file:
            name = file.name
            json.dump(data, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def _mark_live(entry, now: float, ttl: float) -> bool:
    """Is this lock mark one we can age, and is it still inside ``ttl``?

    Every reader of the ledger comes through here — the gate's capacity check, ``explain`` and the
    watchdog's drain — so a mark that cannot be aged answers "expired" instead of raising the
    ``TypeError`` a bare subtraction would: one unreadable entry (a hand edit, an ISO string from
    an older writer, ``null``, a bool, a list) otherwise stops ``--drain`` for *every* loop and
    makes ``explain`` raise instead of reporting. Only a plain number is aged, because the readers
    that take a live set do their own arithmetic on ``at`` — a numeric string is expired, never
    guessed at. ``active`` then writes the drop, so the next read sees a ledger it can read.
    """
    at = mark_at(entry)
    return at is not None and now - at <= ttl


def mark_at(entry) -> float | None:
    """A lock mark's or queue entry's ``at`` epoch, or ``None`` when it cannot be aged.

    The one reading rule for ``at`` (#80), shared by ``_mark_live`` and every reader that ages,
    sorts or prints a mark itself — the watchdog's drain and stuck report, ``died_locks``,
    ``status`` and ``explain``: only a finite plain number is a time. A non-dict entry, a string
    (ISO or numeric), ``null``, a bool, a list or an infinity is junk, which those readers skip
    and the ledger's own prune drops, rather than a ``TypeError`` out of the whole sweep.
    """
    if not isinstance(entry, dict):
        return None
    at = entry.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
        return None
    return float(at)


class LoopState:
    def __init__(self, loop: dict):
        self.loop = loop
        self.dir = config.state_dir(loop)
        self.locks = self.dir / "locks.json"
        self.pending = self.dir / "pending.json"
        self.inflight_file = self.dir / "inflight.json"
        self.breach = self.dir / "breach.json"
        self.observations = self.dir / "observations.json"
        self.watch_file = self.dir / "watchdog.json"
        self.transitions_file = self.dir / "stack-transitions.json"
        # A review's commit_id identifies a head, not the base it reviewed. This ledger
        # has no webhook writer: an external review event cannot create an association.
        self.review_situations = self.dir / "review-situations.json"
        # The last GitHub call a gate (or anything else) could not make: last writer wins.
        self.github_reads = self.dir / "github-reads.json"
        self.log = self.dir / "watchdog.log"

    # -- raw ----------------------------------------------------------------

    def _load(self, path: pathlib.Path, default):
        try:
            return json.loads(path.read_text()) if path.exists() else default
        except Exception:
            return default

    def _save(self, path: pathlib.Path, data) -> None:
        try:
            hostdirs.ensure(path.parent)
            _atomic_write(path, data)
        except Exception as exc:
            log(f"state write failed ({path.name}): {exc}")

    @contextlib.contextmanager
    def locked(self):
        """Hold the loop's state lock across a read-modify-write of locks/pending/inflight.

        One lock for all three files, because the gate's claim spans them (check the ledger,
        write the ledger, drop the queue entry) and must be one step to every other process.
        Reentrant within a thread, so a locked caller can use the ordinary methods.
        """
        path = str(self.dir / "state.lock")
        held = getattr(_HELD, "paths", None)
        if held is None:
            held = _HELD.paths = {}
        if held.get(path):
            held[path] += 1
            try:
                yield
            finally:
                held[path] -= 1
            return
        hostdirs.ensure(self.dir)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            held[path] = 1
            try:
                yield
            finally:
                held.pop(path, None)
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def note(self, message: str) -> None:
        try:
            hostdirs.ensure(self.dir)
            with self.log.open("a") as fh:
                fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}\n")
        except Exception:
            pass

    # -- active runs per seat (the concurrency ledger) ----------------------

    def live_locks(self, seat: str) -> dict:
        """A seat's unexpired entries, read *without* the pruning ``active`` persists.

        ``explain`` is read-only down to the state files: adopting ``active`` there would rewrite
        ``locks.json`` on every question the operator asks, which is a mutation nobody asked for
        and exactly what the acceptance test for a read-only command looks at.
        """
        entries = self._lock_ledger().get(seat)
        if not isinstance(entries, dict):
            return {}
        # ttl_min, raised to the whole worst-case turn — on the budget the claim was taken with,
        # or the loop's now if longer — so a healthy turn never loses its slot (#98). A mark
        # nothing can age answers "expired" here instead of raising out of every reader.
        now = time.time()
        return {k: v for k, v in entries.items()
                if isinstance(v, dict) and _mark_live(
                    v, now, self._seat_ttl(seat, config.claim_budget(v)))}

    def active(self, seat: str) -> dict:
        """This seat's live runs, ``{key: entry}``, expired ones dropped and persisted away.

        The ledger is per *seat* and keyed by PR, because isolation is per *PR*: two PRs may run
        at once when ``concurrency`` allows it, but the same PR never runs twice.

        This is also the one place a mark is dropped from disk, so a ledger entry nothing can read
        is healed here rather than left for the next reader to trip over.
        """
        with self.locked():
            raw = self._load(self.locks, {}) or {}
            data = raw if isinstance(raw, dict) else {}
            stored = data.get(seat)
            entries = stored if isinstance(stored, dict) else {}
            live = self.live_locks(seat)
            # A file or a seat value that is not a mapping holds no runs — an unreadable one is
            # dropped here the same way an expired mark is, so the file heals for every reader.
            unreadable = raw is not data or (stored is not None and not isinstance(stored, dict))
            if unreadable or live != entries:
                if live:
                    data[seat] = live
                else:
                    data.pop(seat, None)
                self._save(self.locks, data)
            return live

    def _lock_ledger(self) -> dict:
        """The lock ledger as a mapping, whatever the file holds.

        ``_load`` already falls back to a default on a file that will not parse; this is the same
        rule for a file that parses into the wrong shape. A ledger that is not a mapping holds no
        runs, and reading it as empty is what keeps one bad file from stopping every reader.
        """
        data = self._load(self.locks, {}) or {}
        return data if isinstance(data, dict) else {}

    def _seat_ttl(self, seat: str | None = None, recorded: float | None = None) -> float:
        """The seat ledger's TTL in seconds: ``ttl_min``, or the whole worst-case turn if longer.

        ``config.seat_ttl_s``'s rule — a claim taken for a turn still inside its own budget never
        loses its slot, on ``seat``'s own clock (#98) — reached through the ledger's own read, so a missing or wrong-typed
        ``ttl_min`` (``config.settings_defaults``' fallback, kept here) cannot propagate into the
        age arithmetic and stop the readers either. ``recorded`` is the budget the claim was taken
        with, which keeps a lowered ``turn_budget_s`` from shortening it.
        """
        default = float(config.SETTINGS_SCHEMA["ttl_min"]["default"])
        raw = self.loop.get("ttl_min")
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raw = default
        try:
            ttl_min = float(raw)
        except (TypeError, ValueError):
            ttl_min = default
        return config.seat_ttl_s({**self.loop, "ttl_min": ttl_min}, seat=seat,
                                 recorded=recorded)

    def active_count(self, seat: str) -> int:
        return len(self.active(seat))

    def is_active(self, seat: str, key: str) -> bool:
        return key in self.active(seat)

    def held_by_other(self, seat: str, key: str) -> str | None:
        """The other seat's name if it is working this PR right now, else ``None``.

        One PR belongs to **one seat at a time**: a review must never run against a PR the fixer
        is mid-fix on, and a fix must not start on a PR under review. This is a PR-level claim
        that sits under the per-seat capacities, not a replacement for them — capacity says how
        many PRs a seat may hold, this says a single PR may not be held by both.

        It is deliberately keyed on PR rather than head: two different heads of the same PR are
        still the same checkout's worth of trouble.
        """
        for other in ("reviewer", "fixer"):
            if other != seat and key in self.active(other):
                return other
        return None

    def acquire(self, seat: str, key: str, head: str = "", why: str = "",
                budget: float | None = None, run: str | None = None) -> None:
        """Claim ``seat`` for ``key``. The isolated worker calls this at launch with its run's
        own budget and id (#98); the claim's TTL never shrinks below the budget it was taken
        with, and only a release naming the same ``run`` may free it."""
        with self.locked():
            data = self._load(self.locks, {}) or {}
            at = time.time()
            entry = {"at": at, "head": head, "why": why,
                     "budget": budget if budget is not None else config.turn_budget(self.loop, seat)}
            if run is not None:
                entry["run"] = run
            data.setdefault(seat, {})[key] = entry
            self._save(self.locks, data)
            CLAIMS.append((self, seat, key, at))

    def release_exact(self, seat: str, key: str, at: float) -> bool:
        """Free one claim only if it is still the very claim made at ``at``."""
        with self.locked():
            data = self._load(self.locks, {}) or {}
            entry = (data.get(seat) or {}).get(key)
            if not isinstance(entry, dict) or entry.get("at") != at:
                return False
            data[seat].pop(key)
            if not data[seat]:
                data.pop(seat, None)
            self._save(self.locks, data)
            return True

    def release_if(self, seat: str, key: str, head: str | None = None,
                   run: str | None = None) -> bool:
        """Free a seat only for *this* PR's turn — never another PR's in-flight work.

        With ``head``, only a claim made for that head is freed: a late verdict on an older
        head must not end a newer run on the same PR. With ``run``, only the claim that run
        wrote is freed (#98): a run's own release (its worker ending it, or an operator
        reconciling it) must never free a newer run's claim on the same seat, PR and head.
        """
        with self.locked():
            data = self._load(self.locks, {}) or {}
            entry = (data.get(seat) or {}).get(key)
            if entry is None or (head is not None and not (
                    isinstance(entry, dict) and entry.get("head") == head)):
                return False
            if run is not None and not (isinstance(entry, dict) and entry.get("run") == run):
                return False
            data[seat].pop(key)
            if not data[seat]:
                data.pop(seat, None)
            self._save(self.locks, data)
            return True

    def release_all(self, seat: str) -> int:
        with self.locked():
            data = self._load(self.locks, {}) or {}
            count = len(data.pop(seat, {}) or {})
            if count:
                self._save(self.locks, data)
            return count

    # -- queue --------------------------------------------------------------

    @contextlib.contextmanager
    def _queue_lock(self):
        # The queue shares the loop's state lock: a seat claim spans the ledger and the queue,
        # and two locks over one file would let a claim and a queue edit interleave.
        with self.locked():
            yield

    def _save_queue(self, data: dict) -> None:
        """Publish queue bytes atomically so unlocked readers never see a partial file."""
        fd, temp = tempfile.mkstemp(prefix=".pending-", dir=self.dir)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(data, output, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, self.pending)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def queue_add(self, seat: str, key: str, head: str, url: str, reason: str) -> None:
        with self._queue_lock():
            data = self._load(self.pending, {}) or {}
            data.setdefault(seat, {})[key] = {"at": time.time(), "head": head, "url": url,
                                              "reason": reason, "id": uuid.uuid4().hex}
            self._save_queue(data)

    def queue_replace_if(self, seat: str, key: str, expected: dict | None,
                         head: str, url: str, reason: str) -> bool:
        """Record a failed dispatch only if no newer event replaced its queue entry."""
        with self._queue_lock():
            data = self._load(self.pending, {}) or {}
            if (data.get(seat) or {}).get(key) != expected:
                return False
            data.setdefault(seat, {})[key] = {"at": time.time(), "head": head, "url": url,
                                              "reason": reason, "id": uuid.uuid4().hex}
            self._save_queue(data)
            return True

    def queue_items(self, seat: str) -> dict:
        """This seat's queue entries, ``{key: entry}``.

        A seat value that is not a mapping — a hand edit, a truncated or older-shape write —
        reads as an empty queue instead of raising out of every reader of it (``explain``'s
        ``_explain_state`` and ``observer.hold_reason`` both call this and then ``.get`` a
        key), the same rule ``live_locks`` applies to ``locks.json``. The next write of a
        well-formed entry replaces it (#167).
        """
        data = self._load(self.pending, {}) or {}
        items = data.get(seat) if isinstance(data, dict) else None
        return items if isinstance(items, dict) else {}

    def queue_drop_unreadable(self, seat: str) -> list[str]:
        """Drop this seat's queue entries nothing can age, and name them (#80).

        The queue's counterpart to ``active`` healing ``locks.json``: an entry that is not a
        mapping, or whose ``at`` fails ``mark_at``, cannot be ordered, aged or trusted as a
        request, so the drain drops it here instead of raising out of its sort for every entry
        behind it. A later event for that PR queues a well-formed request of its own.
        """
        with self._queue_lock():
            data = self._load(self.pending, {}) or {}
            items = data.get(seat) if isinstance(data, dict) else None
            if not isinstance(items, dict):
                return []
            junk = [key for key, entry in items.items() if mark_at(entry) is None]
            if junk:
                for key in junk:
                    items.pop(key)
                if not items:
                    data.pop(seat, None)
                self._save_queue(data)
            return junk

    def queue_all(self) -> dict:
        return self._load(self.pending, {}) or {}

    def queue_pop(self, seat: str, key: str) -> None:
        with self._queue_lock():
            data = self._load(self.pending, {}) or {}
            items = data.get(seat) or {}
            if items.pop(key, None) is not None:
                if not items:
                    data.pop(seat, None)
                self._save_queue(data)

    def queue_pop_head(self, seat: str, key: str, head: str) -> bool:
        """Do not discard a newer head while removing a stale queued request."""
        with self._queue_lock():
            data = self._load(self.pending, {}) or {}
            items = data.get(seat) or {}
            entry = items.get(key)
            if not isinstance(entry, dict) or entry.get("head") != head:
                return False
            items.pop(key)
            if not items:
                data.pop(seat, None)
            self._save_queue(data)
            return True

    def queue_pop_if(self, seat: str, key: str, expected: dict | None) -> bool:
        """Acknowledge only the entry observed before enqueue, never its replacement."""
        with self._queue_lock():
            data = self._load(self.pending, {}) or {}
            items = data.get(seat) or {}
            if expected is None or items.get(key) != expected:
                return False
            items.pop(key)
            if not items:
                data.pop(seat, None)
            self._save_queue(data)
            return True

    # -- in-flight marks ----------------------------------------------------

    def inflight(self, key: str, record: bool = False) -> bool:
        """Has a run for this exact head already been armed, and is it still plausibly out?

        Guards the burst the platform cannot see: several events for the same head arriving
        before the first verdict lands. TTL-bounded so a crashed run cannot wedge the head.
        """
        now = time.time()
        if record:
            with self.locked():
                data = self._load(self.inflight_file, {}) or {}
                data[key] = now
                data = {k: v for k, v in data.items() if now - v < 24 * 3600}
                self._save(self.inflight_file, data)
            return False
        data = self._load(self.inflight_file, {}) or {}
        return now - data.get(key, 0) < self.loop["inflight_ttl_min"] * 60

    def inflight_clear(self, key: str) -> None:
        """Drop one in-flight mark: its run has ended (the isolated worker's release, #98)."""
        with self.locked():
            data = self._load(self.inflight_file, {}) or {}
            if data.pop(key, None) is not None:
                self._save(self.inflight_file, data)

    def inflight_at(self, key: str) -> float:
        """When this head's in-flight mark was armed, or 0.0 — the mark's own clock, read-only.

        ``inflight()`` answers yes/no; an operator asking "how long has this been out" needs the
        timestamp, and recomputing the TTL comparison anywhere else would be a second rule.
        """
        return float((self._load(self.inflight_file, {}) or {}).get(key, 0) or 0)

    def quarantine(self, number: int, head: str) -> None:
        """Erase head-only run and escalation tokens after a base transition."""
        with self.locked():
            data = self._load(self.inflight_file, {}) or {}
            for prefix in (f"review:{number}:{head}", f"fix:{number}:{head}"):
                data.pop(prefix, None)
            self._save(self.inflight_file, data)
        key = f"{self.loop['repo']}#{number}"
        with self._breach_lock():
            markers = self._load(self.breach, {}) or {}
            if key in markers:
                markers.pop(key)
                self._breach_save(markers)

    # -- breach markers -----------------------------------------------------

    @contextlib.contextmanager
    def _breach_lock(self):
        hostdirs.ensure(self.dir)
        fd = os.open(self.dir / "breach.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _breach_save(self, data: dict) -> None:
        """Persist a claim before releasing the lock or allowing the route to fire.

        Unlike ``_save`` this raises: a breach claim that did not reach disk must not fire.
        """
        _atomic_write(self.breach, data)

    def breach_get(self, number: int) -> dict:
        return (self._load(self.breach, {}) or {}).get(f"{self.loop['repo']}#{number}") or {}

    def breach_set(self, number: int, entry: dict) -> dict:
        key = f"{self.loop['repo']}#{number}"
        with self._breach_lock():
            data = self._load(self.breach, {}) or {}
            prior = data.get(key) or {}
            # Duplicate cap events cannot re-arm an already claimed head.
            if prior.get("head") == entry.get("head"):
                return prior
            data[key] = entry
            self._breach_save(data)
            return prior

    def breach_deliver(self, number: int, entry: dict, current: Callable[[], bool],
                       send: Callable[[dict], bool], reserved: Callable[[dict], None] | None = None) -> str:
        """Reserve a pending delivery under lock, then POST without holding it.

        A synchronous gateway must be able to claim the marker before answering
        the POST. The attempt token prevents concurrent deliveries; its lease lets
        a watchdog retry if the sender dies. Finalization is compare-and-swap so
        a late response cannot overwrite a newer head or a claimed wake.
        """
        key = f"{self.loop['repo']}#{number}"
        head = entry["head"]
        with self._breach_lock():
            if not current():
                return "stale"
            data = self._load(self.breach, {}) or {}
            prior = data.get(key) or {}
            new = prior.get("head") != head
            if not new and (prior.get("status") != "delivery-pending"
                            or (prior.get("delivery_token")
                                and time.time() - prior.get("delivery_at", 0) < 60)):
                return "already"
            # Keep the original reason/rounds when retrying a pending marker.
            marker = {**(entry if new else prior), "status": "delivery-pending",
                      "delivery_token": uuid.uuid4().hex, "delivery_at": time.time()}
            data[key] = marker
            self._breach_save(data)
        if reserved:
            try:
                reserved(marker)
            except Exception as exc:
                log(f"breach observer notification failed: {exc}")
        try:
            delivered = send(marker)
        except Exception as exc:
            log(f"adjudicator delivery failed: {exc}")
            delivered = False
        with self._breach_lock():
            data = self._load(self.breach, {}) or {}
            latest = data.get(key) or {}
            if (latest.get("head") == head
                    and latest.get("delivery_token") == marker["delivery_token"]):
                latest = {k: v for k, v in latest.items()
                          if k not in {"delivery_token", "delivery_at"}}
                if latest.get("status") == "delivery-pending" and delivered:
                    latest["status"] = "awaiting-adjudication"
                # A gateway may already have moved this marker to adjudicating.
                data[key] = latest
                self._breach_save(data)
        return "new" if new else "retry"


    def breach_claim(self, number: int, head: str) -> dict | None:
        """Claim exactly one wake for this PR/head across gateway processes."""
        key = f"{self.loop['repo']}#{number}"
        with self._breach_lock():
            data = self._load(self.breach, {}) or {}
            marker = data.get(key)
            if (not isinstance(marker, dict) or marker.get("pr") != number
                    or marker.get("head") != head
                    or not (marker.get("status") == "awaiting-adjudication"
                            or (marker.get("status") == "delivery-pending"
                                and marker.get("delivery_token")))):
                return None
            data[key] = {**marker, "status": "adjudicating", "adjudicating_at": now_iso()}
            self._breach_save(data)
            return marker

    def breach_start(self, number: int, head: str, rounds: int) -> dict | None:
        """Mark the breach at this head as being ruled on by an isolated adjudicator turn.

        The run ledger's unique turn index is what makes a ruling at-most-once; this marks the
        marker so the watchdog stops retrying delivery and ``explain`` says a ruling is out.
        Unlike ``breach_claim`` it does not need a live delivery token: the enqueue may have
        been durable even when its delivery attempt reported failure (a spawn error), and that
        attempt's token is gone by the time the worker runs. A marker already claimed by
        anyone else — including a legacy gateway route — refuses.
        """
        key = f"{self.loop['repo']}#{number}"
        with self._breach_lock():
            data = self._load(self.breach, {}) or {}
            marker = data.get(key)
            if (not isinstance(marker, dict) or marker.get("pr") != number
                    or marker.get("head") != head or marker.get("rounds") != rounds
                    or marker.get("status") not in ("delivery-pending", "awaiting-adjudication")):
                return None
            data[key] = {k: v for k, v in marker.items()
                         if k not in {"delivery_token", "delivery_at"}}
            data[key]["status"] = "adjudicating"
            # When the ruling started: the watchdog's stall clock for it counts from here.
            data[key]["adjudicating_at"] = now_iso()
            self._breach_save(data)
            return marker

    def breach_resume(self, number: int, head: str, rounds: int) -> dict | None:
        """Undo ``breach_start`` for an isolated adjudicator turn that failed before recording
        any ruling (issue #53), so its retry can start it again. Only this head and round
        count, and only from ``adjudicating``; anything else is left alone."""
        key = f"{self.loop['repo']}#{number}"
        with self._breach_lock():
            data = self._load(self.breach, {}) or {}
            marker = data.get(key)
            if (not isinstance(marker, dict) or marker.get("pr") != number
                    or marker.get("head") != head or marker.get("rounds") != rounds
                    or marker.get("status") != "adjudicating"):
                return None
            data[key] = {**marker, "status": "awaiting-adjudication"}
            self._breach_save(data)
            return data[key]

    def breach_all(self) -> dict:
        return self._load(self.breach, {}) or {}

    # -- watchdog memory ----------------------------------------------------

    def watch(self) -> dict:
        return self._load(self.watch_file, {}) or {}

    def watch_save(self, data: dict) -> None:
        self._save(self.watch_file, data)

    # -- held issue-fix hand-offs (#324): {issue number: origin PR} ----------

    def fix_holds(self) -> dict:
        # No lock: writes are atomic replaces, and `explain` must stay read-only (it may not
        # even create state.lock).
        raw = self._load(self.dir / "held-fixes.json", {})
        out = {}
        for key, pr in (raw.items() if isinstance(raw, dict) else ()):
            if str(key).isdigit() and type(pr) is int:
                out[int(key)] = pr
        return out

    def fix_hold_get(self, number: int) -> int | None:
        return self.fix_holds().get(number)

    def fix_hold_set(self, number: int, pr: int) -> None:
        with self.locked():
            data = {str(k): v for k, v in self.fix_holds().items()}
            data[str(number)] = pr
            self._save(self.dir / "held-fixes.json", data)

    def fix_hold_drop(self, number: int) -> None:
        with self.locked():
            data = {str(k): v for k, v in self.fix_holds().items()}
            if data.pop(str(number), None) is not None:
                self._save(self.dir / "held-fixes.json", data)

    def fix_hold_said(self, number: int) -> bool:
        """Whether the one "waiting for PR" comment was already posted for this issue."""
        with self.locked():
            raw = self._load(self.dir / "held-fixes-said.json", [])
        return number in (raw if isinstance(raw, list) else ())

    def fix_hold_say(self, number: int) -> None:
        with self.locked():
            raw = self._load(self.dir / "held-fixes-said.json", [])
            raw = [n for n in raw if type(n) is int] if isinstance(raw, list) else []
            if number not in raw:
                self._save(self.dir / "held-fixes-said.json", sorted(raw + [number]))

    # -- review-only verdict cap: its one notice and granted rounds ----------

    def _review_caps(self) -> dict:
        raw = self._load(self.dir / "review-only-caps.json", {})
        return raw if isinstance(raw, dict) else {}

    def review_cap_entry(self, number: int) -> dict:
        entry = self._review_caps().get(str(number))
        return entry if isinstance(entry, dict) else {}

    def review_cap_notice_once(self, number: int, head: str) -> bool:
        """Record the cap notice for this PR and head; True only the first time."""
        with self.locked():
            data = self._review_caps()
            entry = data.get(str(number)) if isinstance(data.get(str(number)), dict) else {}
            if entry.get("notified") == head:
                return False
            data[str(number)] = {**entry, "notified": head}
            self._save(self.dir / "review-only-caps.json", data)
            return True

    def review_cap_grant(self, number: int, head: str, rounds: int) -> bool:
        """Allow exactly one more verdict at ``head`` once ``rounds`` verdicts are spent.

        False when this head already holds an unspent grant for these rounds.
        """
        with self.locked():
            data = self._review_caps()
            entry = data.get(str(number)) if isinstance(data.get(str(number)), dict) else {}
            if entry.get("grant") == {"head": head, "rounds": rounds}:
                return False
            data[str(number)] = {**entry, "grant": {"head": head, "rounds": rounds}}
            self._save(self.dir / "review-only-caps.json", data)
            return True

    def review_cap_granted(self, number: int, head: str, rounds: int) -> bool:
        """Whether a maintainer's grant covers the next verdict: same head, no verdict since."""
        return self.review_cap_entry(number).get("grant") == {"head": head, "rounds": rounds}

    def github_failure_record(self, entry: dict) -> None:
        """Keep the most recent failed GitHub call (see ``gh.record_failure``)."""
        self._save(self.github_reads, {"last_failure": entry})

    def github_failure(self) -> dict:
        data = self._load(self.github_reads, {})
        entry = data.get("last_failure") if isinstance(data, dict) else None
        return entry if isinstance(entry, dict) else {}

    def transition_get(self, number: int) -> dict:
        return (self._load(self.transitions_file, {}) or {}).get(str(number)) or {}

    def transition_set(self, number: int, entry: dict) -> dict:
        """Separate durable ledger: whole-watchdog snapshots cannot erase holds."""
        return self._transition_write(number, lambda prior: None
                                      if isinstance(prior, dict) and prior.get("head") == entry["head"]
                                      else entry)

    def transition_update(self, number: int, head: str, fields: dict) -> dict | None:
        """Merge bookkeeping into the hold for exactly this head; never create or move one.

        The boundary facts themselves (head, from_base, at, old_review_ids) are not
        writable here: only a new ``record`` for a new head may replace them.
        """
        protected = {"head", "from_base", "observed_stacked_head", "at", "old_review_ids"}
        if protected & set(fields):
            raise ValueError("transition boundary facts are immutable")
        result = self._transition_write(
            number, lambda prior: {**prior, **fields}
            if isinstance(prior, dict) and prior.get("head") == head else None)
        return result if isinstance(result, dict) and result.get("head") == head else None

    def _transition_write(self, number: int, change) -> dict:
        hostdirs.ensure(self.dir)
        with (self.dir / "stack-transitions.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self._load(self.transitions_file, {}) or {}
            prior = data.get(str(number))
            entry = change(prior)
            if entry is None:
                return prior
            data[str(number)] = entry
            fd, name = tempfile.mkstemp(dir=self.dir, prefix=".stack-transitions-")
            try:
                with os.fdopen(fd, "w") as out:
                    json.dump(data, out)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(name, self.transitions_file)
                directory = os.open(self.dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            return entry

    def associated_review(self, number: int, identity: str, review_id: int) -> bool:
        """No trusted host receipt issuer exists yet: *all* associations are unknown.

        A JSON record's ``source`` string is caller-forgeable, including the former
        ``trusted-submission-receipt`` label. In particular a reviewer's process can
        write state under the same HOME; neither file permissions nor a label attest
        that the gateway dispatched this run. Never authorize stacked readiness from
        this disk file until an actual host-attested issuer and verifier are integrated.
        """
        return False


def state_for(loop: dict) -> LoopState:
    return LoopState(loop)


def artifacts_path(loop: dict, number: int) -> pathlib.Path:
    return config.artifacts_dir(loop, number)
