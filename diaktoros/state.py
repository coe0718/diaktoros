"""Per-loop state on disk: seat locks, the queue, in-flight marks, breach markers, and the
observer's delivery ledger.

All of it lives under the loop's own ``state_dir`` (default
``~/.hermes/state/diaktoros/<id>/``), so two loops never share a file and a loop can
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
from .config import settings_form
from .util import log, now_iso

# Per-thread depth of the state lock we already hold, keyed by lock path. ``flock`` is tied to
# the open file description, so a second ``open`` + ``flock`` in the same thread would deadlock
# against itself; nested sections (``take_seat`` queueing under its own claim) reuse the outer one.
_HELD = threading.local()

# watchdog.log is bounded like the other append-only files (#549): once it passes
# LOG_PRUNE_BYTES, a write rewrites it to its last LOG_KEEP lines.
# A line is cut at LOG_LINE_MAX characters and a prune also drops the oldest lines until the
# log fits LOG_PRUNE_BYTES, so a few oversized lines cannot hold it above the bound.
LOG_KEEP = 1000
INFLIGHT_PRUNE_S = 24 * 3600     # floor for how long a recorded in-flight mark is kept (#736)
LOG_PRUNE_BYTES = 256 * 1024
LOG_LINE_MAX = 4096

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
    """Publish ``data`` as JSON to ``path`` whole or not at all, and durably before returning."""
    _atomic_publish(path, json.dumps(data, indent=2))


def _atomic_publish(path: pathlib.Path, text: str) -> None:
    """Publish ``text`` to ``path``: temp file, fsync, ``os.replace``, fsync of the directory.

    The one place the module's durability rule lives; ``_save_queue`` and ``_prune_log`` call it
    rather than repeating the dance, so neither can skip a step.
    """
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=f".{path.stem}-",
                                         delete=False) as file:
            name = file.name
            file.write(text)
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

    A mark dated *after* ``now`` (a clock stepped back, a hand edit, a milliseconds stamp) has a
    negative age that would sit inside any TTL forever, holding the seat at capacity where no
    report could see it (#794): it is expired, like a mark that cannot be aged.
    """
    at = mark_at(entry)
    return at is not None and 0 <= now - at <= ttl


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


def _clock(value) -> float | None:
    """A bare epoch (an in-flight mark's value), or None when it is not a finite plain number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _inflight_live(value, now: float, ttl_s: float) -> bool:
    """Is this in-flight mark one we can age, and is it inside ``ttl_s``?

    The one rule for the read and the write-path prune. As in ``_mark_live`` (#794), a mark dated
    *after* ``now`` (a clock stepped back, a hand edit, a milliseconds stamp) has a negative age
    that would sit inside any TTL forever: it is expired, never held and never kept (#940).
    """
    at = _clock(value)
    return at is not None and 0 <= now - at < ttl_s


DELIVERY_LEASE_S = 60.0
# How far *ahead* of the reader's clock a ``delivery_at`` may be and still hold the lease: modest
# skew (an NTP step, a suspend/resume, two processes off by milliseconds) must not release a
# delivery that is in flight. Anything further ahead is a bad stamp and holds nothing (#942).
DELIVERY_SKEW_S = 30.0


def _delivery_in_flight(marker: dict, now: float | None = None) -> bool:
    """Whether a breach marker's delivery lease is still live: it holds an attempt token stamped
    within the last ``DELIVERY_LEASE_S`` seconds. A stamp up to ``DELIVERY_SKEW_S`` *ahead* of the
    clock still holds (clock skew); a stamp that is missing, junk or further ahead holds nothing,
    so a bad ``delivery_at`` can never wedge the retry on ``already``."""
    if not marker.get("delivery_token"):
        return False
    at = _clock(marker.get("delivery_at"))
    if at is None:
        return False
    age = (time.time() if now is None else now) - at
    return -DELIVERY_SKEW_S <= age < DELIVERY_LEASE_S


def _mapping(raw) -> dict:
    """A state file's top level as a dict: a file that is not a mapping holds nothing (#80)."""
    return raw if isinstance(raw, dict) else {}


def _seat(data: dict, seat: str) -> dict:
    """``data[seat]`` for writing, replacing a value that is not a mapping (#547, #548): a writer
    heals the shape its readers already ignore, instead of raising on it."""
    items = data.get(seat)
    if not isinstance(items, dict):
        items = data[seat] = {}
    return items


def _seat_read(data: dict, seat: str) -> dict:
    """``data[seat]`` for reading; {} when it is not a mapping."""
    items = data.get(seat)
    return items if isinstance(items, dict) else {}


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

    def _save_strict(self, path: pathlib.Path, data) -> None:
        """Publish ``data`` or raise: for a write the caller must not act without (#726)."""
        hostdirs.ensure(path.parent)
        _atomic_write(path, data)

    def _save(self, path: pathlib.Path, data) -> None:
        """Best-effort publish: a failure is logged and swallowed. A caller that proceeds as if
        the write landed (a seat claim) uses ``_save_strict`` instead."""
        try:
            self._save_strict(path, data)
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
            if len(message) > LOG_LINE_MAX:
                message = message[:LOG_LINE_MAX] + "...[truncated]"
            # The append and the prune's read-and-replace are one step to every other writer:
            # a line appended between the prune's read and its replace would be lost (#732).
            with self._log_lock():
                with self.log.open("a") as fh:
                    fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}\n")
                self._prune_log()
        except Exception:
            pass

    @contextlib.contextmanager
    def _log_lock(self):
        """Serialize ``note`` across processes. A leaf lock of its own, never the state lock:
        ``note`` is called from inside ``locked()`` sections and must not wait on others."""
        fd = os.open(self.dir / "watchdog.log.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing releases the flock

    def _prune_log(self) -> None:
        """Past ``LOG_PRUNE_BYTES``, keep the last ``LOG_KEEP`` lines that fit in that many bytes.

        The caller holds ``_log_lock``: that is what keeps appends out of the read-to-replace
        window and gives the fixed temp name a single writer."""
        if self.log.stat().st_size <= LOG_PRUNE_BYTES:
            return
        lines = self.log.read_text(errors="replace").splitlines(keepends=True)
        lines = lines[-LOG_KEEP:]
        size = sum(len(l.encode()) for l in lines)
        while len(lines) > 1 and size > LOG_PRUNE_BYTES:
            size -= len(lines.pop(0).encode())
        _atomic_publish(self.log, "".join(lines))

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
        ``ttl_min`` (``settings_form.settings_defaults``' fallback, kept here) cannot propagate into the
        age arithmetic and stop the readers either. ``recorded`` is the budget the claim was taken
        with, which keeps a lowered ``turn_budget_s`` from shortening it.
        """
        return config.seat_ttl_s({**self.loop, "ttl_min": self._minutes("ttl_min")}, seat=seat,
                                 recorded=recorded)

    def _minutes(self, key: str) -> float:
        """A minutes knob (``ttl_min``, ``inflight_ttl_min``) as a finite number, never an error.

        ``normalize()`` does not coerce these and the settings form can write them raw, so a
        string that is not a number, ``None``, a bool, a non-finite number or a missing key
        falls back to the setting's default (``settings_form.SETTINGS_SCHEMA``); a numeric
        string reads as its number. One healing rule for every reader of a TTL.
        """
        default = float(settings_form.SETTINGS_SCHEMA[key]["default"])
        raw = self.loop.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            return default
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):   # an int past float's range overflows
            return default
        return value if math.isfinite(value) else default

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
                budget: float | None = None, run: str | None = None,
                exclusive: bool = False) -> bool:
        """Claim ``seat`` for ``key``. The isolated worker calls this at launch with its run's
        own budget and id (#98); the claim's TTL never shrinks below the budget it was taken
        with, and only a release naming the same ``run`` may free it.

        By default the claim overwrites whatever is there (last writer wins). With ``exclusive``
        it is a compare-and-swap (#727): under the state lock, a *live* claim on ``(seat, key)``
        is left untouched, nothing is written and the answer is ``False``, so two check-then-act
        callers that both saw the seat free cannot evict the claim that won. The one claim a
        caller may replace is the gate's own run-less claim, and only when it names a ``run``
        (the worker stamping its run on the claim the gate made at launch), or its own earlier
        claim. An expired claim is not live and is replaced. Returns whether it was written.

        Raises when the claim cannot be written (#726)."""
        with self.locked():
            data = _mapping(self._load(self.locks, {}))
            if exclusive:
                held = self.live_locks(seat).get(key)
                if held is not None and not (run is not None and held.get("run") in (None, run)):
                    return False
            at = time.time()
            entry = {"at": at, "head": head, "why": why,
                     "budget": budget if budget is not None else config.turn_budget(self.loop, seat)}
            if run is not None:
                entry["run"] = run
            _seat(data, seat)[key] = entry
            # A claim that did not reach disk is no claim: a second gate would take the same
            # seat. Raise (#726) so the caller does not run, and never record it in CLAIMS.
            self._save_strict(self.locks, data)
            CLAIMS.append((self, seat, key, at))
            return True

    def release_exact(self, seat: str, key: str, at: float) -> bool:
        """Free one claim only if it is still the very claim made at ``at``."""
        with self.locked():
            data = _mapping(self._load(self.locks, {}))
            entry = _seat_read(data, seat).get(key)
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
            data = _mapping(self._load(self.locks, {}))
            entry = _seat_read(data, seat).get(key)
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
            data = _mapping(self._load(self.locks, {}))
            stored = data.pop(seat, {})
            count = len(stored) if isinstance(stored, dict) else 0
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
        _atomic_write(self.pending, data)

    def queue_add(self, seat: str, key: str, head: str, url: str, reason: str) -> None:
        with self._queue_lock():
            data = _mapping(self._load(self.pending, {}))
            _seat(data, seat)[key] = {"at": time.time(), "head": head, "url": url,
                                      "reason": reason, "id": uuid.uuid4().hex}
            self._save_queue(data)

    def queue_replace_if(self, seat: str, key: str, expected: dict | None,
                         head: str, url: str, reason: str) -> bool:
        """Record a failed dispatch only if no newer event replaced its queue entry."""
        with self._queue_lock():
            data = _mapping(self._load(self.pending, {}))
            if _seat_read(data, seat).get(key) != expected:
                return False
            _seat(data, seat)[key] = {"at": time.time(), "head": head, "url": url,
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
        return _mapping(self._load(self.pending, {}))

    def queue_pop(self, seat: str, key: str) -> None:
        with self._queue_lock():
            data = _mapping(self._load(self.pending, {}))
            items = _seat_read(data, seat)
            if items.pop(key, None) is not None:
                if not items:
                    data.pop(seat, None)
                self._save_queue(data)

    def queue_pop_head(self, seat: str, key: str, head: str) -> bool:
        """Do not discard a newer head while removing a stale queued request."""
        with self._queue_lock():
            data = _mapping(self._load(self.pending, {}))
            items = _seat_read(data, seat)
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
            data = _mapping(self._load(self.pending, {}))
            items = _seat_read(data, seat)
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
        ttl_s = self._minutes("inflight_ttl_min") * 60
        if record:
            with self.locked():
                # Read the clock only once the lock is held: a mark another process wrote while
                # we waited is dated after a ``now`` taken before the wait, and would be pruned
                # below as future-dated (#940), losing a live in-flight mark.
                now = time.time()
                data = _mapping(self._load(self.inflight_file, {}))
                data[key] = now
                # #545: an unageable mark is dropped, never subtracted from. #736: keep a mark
                # at least as long as the read path honors it, so a configured TTL beyond a
                # day is not cut short here.
                keep_s = max(INFLIGHT_PRUNE_S, ttl_s)
                data = {k: v for k, v in data.items() if _inflight_live(v, now, keep_s)}
                self._save_strict(self.inflight_file, data)
            return False
        return _inflight_live(_mapping(self._load(self.inflight_file, {})).get(key), now, ttl_s)

    def inflight_clear(self, key: str) -> None:
        """Drop one in-flight mark: its run has ended (the isolated worker's release, #98)."""
        with self.locked():
            data = _mapping(self._load(self.inflight_file, {}))
            if data.pop(key, None) is not None:
                self._save(self.inflight_file, data)

    def inflight_at(self, key: str) -> float:
        """When this head's in-flight mark was armed, or 0.0 — the mark's own clock, read-only.

        ``inflight()`` answers yes/no; an operator asking "how long has this been out" needs the
        timestamp, and recomputing the TTL comparison anywhere else would be a second rule.
        """
        return _clock(_mapping(self._load(self.inflight_file, {})).get(key)) or 0.0

    def quarantine(self, number: int, head: str) -> None:
        """Erase head-only run and escalation tokens after a base transition.

        Both files are rewritten under one hold of the state lock and the breach lock (the
        order ``migrate`` and ``cleanup`` take them), so no other process sees one cleared and
        the other not. The breach marker goes first: it is the one that could be claimed
        afterwards, while a surviving in-flight mark merely ages out. A crash between the two
        writes cannot be made atomic across two files; this order makes it the harmless one.
        """
        key = f"{self.loop['repo']}#{number}"
        with self.locked(), self._breach_lock():
            markers = _mapping(self._load(self.breach, {}))
            if key in markers:
                markers.pop(key)
                self._breach_save(markers)
            data = _mapping(self._load(self.inflight_file, {}))
            for prefix in (f"review:{number}:{head}", f"fix:{number}:{head}"):
                data.pop(prefix, None)
            self._save_strict(self.inflight_file, data)

    # -- human_paths holds (#478) --------------------------------------------

    def _human_holds(self) -> dict:
        raw = self._load(self.dir / "human-holds.json", {})
        return raw if isinstance(raw, dict) else {}

    def human_hold_set(self, number: int, head: str, why: str) -> None:
        """Record that ``head`` waits for a person's approval (one entry per PR, newest head)."""
        with self.locked():
            data = self._human_holds()
            data[str(number)] = {"head": head, "why": str(why)[:300], "at": time.time()}
            self._save_strict(self.dir / "human-holds.json", data)

    def human_hold(self, number: int, head: str) -> dict | None:
        """The hold at this exact head, or None; a new head is reviewed afresh."""
        entry = self._human_holds().get(str(number))
        return entry if isinstance(entry, dict) and entry.get("head") == head else None

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
        return _mapping(_mapping(self._load(self.breach, {})).get(f"{self.loop['repo']}#{number}"))

    def breach_set(self, number: int, entry: dict) -> dict:
        key = f"{self.loop['repo']}#{number}"
        with self._breach_lock():
            data = _mapping(self._load(self.breach, {}))
            prior = _mapping(data.get(key))
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
            data = _mapping(self._load(self.breach, {}))
            prior = _mapping(data.get(key))
            new = prior.get("head") != head
            if not new and (prior.get("status") != "delivery-pending"
                            or _delivery_in_flight(prior)):
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
            data = _mapping(self._load(self.breach, {}))
            latest = _mapping(data.get(key))
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


    def breach_clear_pending(self, number: int, head: str) -> bool:
        """Drop a ``delivery-pending`` marker at ``head`` that no longer holds it (a raised cap
        or a dismissed verdict). Only that status, that head, and no delivery in flight."""
        key = f"{self.loop['repo']}#{number}"
        with self._breach_lock():
            data = _mapping(self._load(self.breach, {}))
            marker = _mapping(data.get(key))
            if (marker.get("head") != head or marker.get("status") != "delivery-pending"
                    or _delivery_in_flight(marker)):
                return False
            data.pop(key)
            self._breach_save(data)
            return True

    def breach_claim(self, number: int, head: str) -> dict | None:
        """Claim exactly one wake for this PR/head across gateway processes."""
        key = f"{self.loop['repo']}#{number}"
        with self._breach_lock():
            data = _mapping(self._load(self.breach, {}))
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
            data = _mapping(self._load(self.breach, {}))
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
            data = _mapping(self._load(self.breach, {}))
            marker = data.get(key)
            if (not isinstance(marker, dict) or marker.get("pr") != number
                    or marker.get("head") != head or marker.get("rounds") != rounds
                    or marker.get("status") != "adjudicating"):
                return None
            data[key] = {**marker, "status": "awaiting-adjudication"}
            self._breach_save(data)
            return data[key]

    def breach_reopen(self, number: int, head: str, rounds: int, reason: str) -> bool:
        """Re-arm a ruled-on marker at ``head`` for a further ruling, as round ``rounds + 1``
        (#1111: the shadow dissented again after the ruling). Compare-and-swap on this head,
        round and ``adjudicating``, so one caller re-arms it; ``gate.breach`` then delivers the
        new round's turn (its own turn key)."""
        key = f"{self.loop['repo']}#{number}"
        with self._breach_lock():
            data = _mapping(self._load(self.breach, {}))
            marker = data.get(key)
            if (not isinstance(marker, dict) or marker.get("pr") != number
                    or marker.get("head") != head or marker.get("rounds") != rounds
                    or marker.get("status") != "adjudicating"):
                return False
            data[key] = {**{k: v for k, v in marker.items() if k != "adjudicating_at"},
                         "rounds": rounds + 1, "reason": reason, "at": now_iso(),
                         "status": "delivery-pending"}
            self._breach_save(data)
            return True

    def breach_all(self) -> dict:
        return _mapping(self._load(self.breach, {}))

    # -- watchdog memory ----------------------------------------------------

    def watch(self) -> dict:
        return _mapping(self._load(self.watch_file, {}))

    def watch_save(self, data: dict) -> None:
        self._save(self.watch_file, data)

    # -- PRs a maintainer holds (#477): {PR number: {"by", "at", "how"}} ------

    def pr_holds(self) -> dict:
        raw = self._load(self.dir / "held-prs.json", {})
        return {int(k): v for k, v in (raw.items() if isinstance(raw, dict) else ())
                if str(k).isdigit() and isinstance(v, dict)}

    def pr_hold_set(self, number: int, hold: dict) -> bool:
        """Record the hold; True when the PR was not held before (a hold began)."""
        with self.locked():
            data = {str(k): v for k, v in self.pr_holds().items()}
            began = str(number) not in data
            if data.get(str(number)) != hold:
                data[str(number)] = hold
                self._save_strict(self.dir / "held-prs.json", data)
            return began

    def pr_hold_drop(self, number: int) -> None:
        with self.locked():
            data = {str(k): v for k, v in self.pr_holds().items()}
            if data.pop(str(number), None) is not None:
                self._save_strict(self.dir / "held-prs.json", data)

    # -- fixer scope-lock holds (#482): {PR: {"head", "files", "released"}} ---

    def scope_holds(self) -> dict:
        raw = self._load(self.dir / "held-scope.json", {})
        return {int(k): v for k, v in (raw.items() if isinstance(raw, dict) else ())
                if str(k).isdigit() and isinstance(v, dict)}

    def scope_hold_set(self, number: int, hold: dict) -> bool:
        """Record the hold; True when it is new or its head or files changed (say it once)."""
        with self.locked():
            data = {str(k): v for k, v in self.scope_holds().items()}
            changed = data.get(str(number)) != hold
            if changed:
                data[str(number)] = hold
                self._save_strict(self.dir / "held-scope.json", data)
            return changed

    def scope_hold_snapshot(self, number: int) -> dict | None:
        """Read the hold approved by a retry, separately from any ledger operation."""
        with self.locked():
            return self.scope_holds().get(number)

    def scope_hold_release(self, number: int, expected: dict | None) -> dict | None:
        """Release only an unchanged snapshot; never approve a new head or file set.

        The compare and save share the writers' state-file lock. Do not call this while
        holding a ledger transaction: workers can take that lock before the state lock.
        """
        if not expected:
            return None
        with self.locked():
            data = {str(k): v for k, v in self.scope_holds().items()}
            if data.get(str(number)) != expected:
                return None
            held = {**expected, "released": True}
            data[str(number)] = held
            self._save_strict(self.dir / "held-scope.json", data)
            return held

    def scope_hold_drop(self, number: int) -> None:
        with self.locked():
            data = {str(k): v for k, v in self.scope_holds().items()}
            if data.pop(str(number), None) is not None:
                self._save_strict(self.dir / "held-scope.json", data)

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
            self._save_strict(self.dir / "held-fixes.json", data)

    def fix_hold_drop(self, number: int) -> None:
        with self.locked():
            data = {str(k): v for k, v in self.fix_holds().items()}
            if data.pop(str(number), None) is not None:
                self._save_strict(self.dir / "held-fixes.json", data)
            self.fix_hold_unsay(number)       # a later hold on this issue says so again (#796)

    # -- issues the recovery sweep triaged late (#849): {issue number: {"at", "outcome"}} --

    LATE_TRIAGE_KEEP = 20

    def late_triage(self) -> dict:
        """Read-only (`status` and `explain` call it): the newest few issues whose triage the
        watchdog queued because the webhook delivery never arrived."""
        raw = self._load(self.dir / "late-triage.json", {})
        out = {}
        for key, entry in (raw.items() if isinstance(raw, dict) else ()):
            if str(key).isdigit() and isinstance(entry, dict) and isinstance(
                    entry.get("at"), (int, float)) and not isinstance(entry.get("at"), bool):
                out[int(key)] = {"at": float(entry["at"]), "outcome": str(entry.get("outcome"))[:80]}
        return out

    def late_triage_note(self, number: int, at: float, outcome: str) -> None:
        with self.locked():
            data = self.late_triage()
            data[number] = {"at": at, "outcome": outcome}
            newest = sorted(data, key=lambda n: data[n]["at"])[-self.LATE_TRIAGE_KEEP:]
            self._save_strict(self.dir / "late-triage.json",
                              {str(n): data[n] for n in newest})

    def late_triage_resolve(self, numbers) -> list[int]:
        """Drop the entries for ``numbers`` (triage ran since); return those that were noted."""
        with self.locked():
            data = self.late_triage()
            gone = sorted(n for n in set(numbers) if n in data)
            if gone:
                for n in gone:
                    del data[n]
                self._save_strict(self.dir / "late-triage.json",
                                  {str(n): data[n] for n in data})
            return gone

    def _said_claim(self, name: str, number: int) -> bool:
        """Record ``number`` in the one-time-notice ledger ``name``; True only for the caller that
        recorded it. The check and the write are one step under the lock. A claim that did not reach
        disk is not a claim (False), so nothing is posted and a later sweep retries."""
        with self.locked():
            raw = self._load(self.dir / name, [])
            raw = [n for n in raw if type(n) is int] if isinstance(raw, list) else []
            if number in raw:
                return False
            try:
                hostdirs.ensure(self.dir)
                _atomic_write(self.dir / name, sorted(raw + [number]))   # raises, unlike _save
            except Exception as exc:
                log(f"state write failed ({name}): {exc}")
                return False                  # fail closed: an unrecorded claim must not post
            return True

    def _said_release(self, name: str, number: int) -> None:
        with self.locked():
            raw = self._load(self.dir / name, [])
            raw = [n for n in raw if type(n) is int] if isinstance(raw, list) else []
            if number in raw:
                self._save(self.dir / name, [n for n in raw if n != number])

    def fix_hold_said(self, number: int) -> bool:
        """Whether the one "waiting for PR" comment was claimed for this issue."""
        with self.locked():
            raw = self._load(self.dir / "held-fixes-said.json", [])
        return number in (raw if isinstance(raw, list) else ())

    def fix_hold_say(self, number: int) -> bool:
        """Claim the one "waiting for PR" comment *before* posting it: True only for the first
        caller, so two processes cannot both post. Call ``fix_hold_unsay`` if the post fails."""
        return self._said_claim("held-fixes-said.json", number)

    def fix_hold_unsay(self, number: int) -> None:
        self._said_release("held-fixes-said.json", number)

    # -- the issue fixer's question or plan (#483): one open ask per issue ----------
    # fix-asks.json: {"open": {issue: {kind, comment_id, text}}, "count": {issue: n},
    # "approved": [issue], "handoff": {issue: {kind, text, reply, by}}}. No lock on read, like
    # ``fix_holds``: writes are atomic replaces.

    def _fix_asks(self) -> dict:
        raw = self._load(self.dir / "fix-asks.json", {})
        raw = raw if isinstance(raw, dict) else {}
        out = {}
        for name in ("open", "count", "handoff"):
            part = raw.get(name)
            out[name] = ({str(k): v for k, v in part.items() if str(k).isdigit()
                          and (name == "count") == (type(v) is int)}
                         if isinstance(part, dict) else {})
        approved = raw.get("approved")
        out["approved"] = [n for n in approved if type(n) is int] if isinstance(approved, list) else []
        return out

    def fix_ask_open(self) -> dict:
        """``{issue: {"kind", "comment_id", "text"}}`` of the asks waiting for a maintainer."""
        return {int(k): v for k, v in self._fix_asks()["open"].items() if isinstance(v, dict)}

    def fix_ask_count(self, number: int) -> int:
        return self._fix_asks()["count"].get(str(number), 0)

    def fix_plan_approved(self, number: int) -> bool:
        return number in self._fix_asks()["approved"]

    def fix_handoff(self, number: int) -> dict | None:
        value = self._fix_asks()["handoff"].get(str(number))
        return value if isinstance(value, dict) else None

    def _fix_ask_edit(self, change) -> None:
        with self.locked():
            data = self._fix_asks()
            change(data)
            self._save_strict(self.dir / "fix-asks.json", data)

    def fix_ask_set(self, number: int, kind: str, comment_id: int, text: str) -> None:
        def change(data):
            data["open"][str(number)] = {"kind": kind, "comment_id": comment_id, "text": text}
            data["count"][str(number)] = data["count"].get(str(number), 0) + 1
            data["handoff"].pop(str(number), None)
        self._fix_ask_edit(change)

    def fix_ask_resolve(self, number: int, reply: str, by: str, approve: bool) -> None:
        """Save the handoff (the ask and its reply) for the resumed turn; the ask stays open until
        ``fix_ask_drop`` (a resume that could not be queued is retried)."""
        def change(data):
            ask = data["open"].get(str(number)) or {}
            data["handoff"][str(number)] = {"kind": ask.get("kind", ""), "text": ask.get("text", ""),
                                            "reply": reply, "by": by}
            if approve and number not in data["approved"]:
                data["approved"].append(number)
        self._fix_ask_edit(change)

    def fix_ask_drop(self, number: int) -> None:
        self._fix_ask_edit(lambda data: data["open"].pop(str(number), None))

    # -- issue dependencies (#671): {issue: {prerequisite issue: {"by", "at"}}} ----------

    def issue_deps(self) -> dict:
        """Declared dependencies: issue B waits for issue A's implementation to merge. No lock
        on read, like ``fix_holds``: writes are atomic replaces."""
        raw = self._load(self.dir / "issue-deps.json", {})
        out: dict = {}
        for key, deps in (raw.items() if isinstance(raw, dict) else ()):
            if str(key).isdigit() and isinstance(deps, dict):
                kept = {int(a): v for a, v in deps.items() if str(a).isdigit() and isinstance(v, dict)}
                if kept:
                    out[int(key)] = kept
        return out

    def issue_dep_add(self, issue: int, prerequisite: int, record: dict) -> bool:
        """Declare the dependency; False when it was already declared."""
        with self.locked():
            data = {str(b): {str(a): v for a, v in deps.items()}
                    for b, deps in self.issue_deps().items()}
            deps = data.setdefault(str(issue), {})
            if str(prerequisite) in deps:
                return False
            deps[str(prerequisite)] = dict(record)
            self._save_strict(self.dir / "issue-deps.json", data)
            return True

    def issue_dep_drop(self, issue: int, prerequisite: int) -> bool:
        """Remove (waive) the dependency; False when there was none."""
        with self.locked():
            data = {str(b): {str(a): v for a, v in deps.items()}
                    for b, deps in self.issue_deps().items()}
            deps = data.get(str(issue)) or {}
            if deps.pop(str(prerequisite), None) is None:
                return False
            if not deps:
                data.pop(str(issue), None)
            self._save_strict(self.dir / "issue-deps.json", data)
            return True

    def dep_waits(self) -> list[int]:
        """Issues whose hand-off waits for a prerequisite (#671), re-checked by the sweep."""
        raw = self._load(self.dir / "dep-waits.json", [])
        return sorted({n for n in raw if type(n) is int}) if isinstance(raw, list) else []

    def dep_wait_set(self, issue: int) -> None:
        with self.locked():
            waits = set(self.dep_waits()) | {issue}
            self._save_strict(self.dir / "dep-waits.json", sorted(waits))

    def dep_wait_drop(self, issue: int) -> None:
        with self.locked():
            waits = set(self.dep_waits())
            if issue in waits:
                self._save_strict(self.dir / "dep-waits.json", sorted(waits - {issue}))
            said = self._load(self.dir / "dep-waits-said.json", [])
            said = [n for n in said if type(n) is int] if isinstance(said, list) else []
            if issue in said:
                self._save(self.dir / "dep-waits-said.json", [n for n in said if n != issue])

    def dep_wait_said(self, issue: int) -> bool:
        with self.locked():
            raw = self._load(self.dir / "dep-waits-said.json", [])
        return issue in (raw if isinstance(raw, list) else ())

    def dep_wait_say(self, issue: int) -> bool:
        """Claim the one dependency-wait comment *before* posting it: True only for the first
        caller. Call ``dep_wait_unsay`` if the post fails."""
        return self._said_claim("dep-waits-said.json", issue)

    def dep_wait_unsay(self, issue: int) -> None:
        self._said_release("dep-waits-said.json", issue)

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
            self._save_strict(self.dir / "review-only-caps.json", data)
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
            self._save_strict(self.dir / "review-only-caps.json", data)
            return True

    def review_cap_granted(self, number: int, head: str, rounds: int) -> bool:
        """Whether a maintainer's grant covers the next verdict: same head, no verdict since."""
        return self.review_cap_entry(number).get("grant") == {"head": head, "rounds": rounds}

    # -- numbered review findings (#475) --------------------------------------

    def findings_all(self) -> dict:
        raw = self._load(self.dir / "findings.json", {})
        return raw if isinstance(raw, dict) else {}

    def findings_get(self, number: int) -> dict:
        entry = self.findings_all().get(str(number))
        return entry if isinstance(entry, dict) else {}

    def findings_put(self, number: int, entry: dict) -> None:
        with self.locked():
            data = self.findings_all()
            data[str(number)] = entry
            self._save_strict(self.dir / "findings.json", data)

    # The findings ledger as a shadow review was queued (#218): its prompt shows only the
    # reviews posted before then, so the broker judges its review against this, never against
    # what the real review of the same head recorded meanwhile. First snapshot wins.
    SHADOW_FINDINGS_KEPT = 300

    def shadow_findings_put(self, number: int, head: str, entry: dict,
                            keep: "set[tuple[int, str]] | None" = None) -> None:
        """Keep the snapshot for this PR and head. Past ``SHADOW_FINDINGS_KEPT`` the oldest are
        dropped, but never one in ``keep`` (the (number, head) of a shadow run that has not
        ended: its broker would fall back to the live ledger, #1013). ``keep=None`` means the
        caller could not tell which runs are open, so nothing is dropped."""
        with self.locked():
            path = self.dir / "shadow-findings.json"
            data = self._load(path, {})
            data = data if isinstance(data, dict) else {}
            key = f"{number}:{head}"
            if key in data:
                return
            data[key] = {"entry": entry if isinstance(entry, dict) else {}, "at": time.time()}
            if keep is not None and len(data) > self.SHADOW_FINDINGS_KEPT:
                held = {f"{n}:{h}" for n, h in keep} | {key}
                oldest = sorted((k for k in data if k not in held),
                                key=lambda k: (data[k] or {}).get("at") or 0)
                for old in oldest[:len(data) - self.SHADOW_FINDINGS_KEPT]:
                    data.pop(old, None)
            self._save_strict(path, data)

    def shadow_findings_get(self, number: int, head: str) -> dict | None:
        """The snapshot for this PR and head, or None (none was taken)."""
        data = self._load(self.dir / "shadow-findings.json", {})
        item = data.get(f"{number}:{head}") if isinstance(data, dict) else None
        entry = item.get("entry") if isinstance(item, dict) else None
        return entry if isinstance(entry, dict) else None

    def github_failure_record(self, entry: dict) -> None:
        """Keep the most recent failed GitHub call (see ``gh.record_failure``)."""
        self._save(self.github_reads, {"last_failure": entry})

    def github_failure(self) -> dict:
        data = self._load(self.github_reads, {})
        entry = data.get("last_failure") if isinstance(data, dict) else None
        return entry if isinstance(entry, dict) else {}

    def transition_get(self, number: int) -> dict:
        return _mapping(_mapping(self._load(self.transitions_file, {})).get(str(number)))

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
            data = _mapping(self._load(self.transitions_file, {}))
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
