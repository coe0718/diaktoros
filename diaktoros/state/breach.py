"""The adjudicator's breach markers, one per PR in ``breach.json`` (#68, #1213): a ruling's claim,
start, resume and reopen under ``breach.lock``.

A mixin of ``LoopState`` (split from ``state/__init__.py``, which sits at its size ceiling), like
``FixAsks``: it uses the state's ``dir``, ``breach``, ``_load`` and ``_save_strict``, and adds no
state of its own. The state module's helpers (``_atomic_write``, ``log``, the marker checks, ...)
are read through the package at call time, so a patch on ``diaktoros.state`` still reaches them.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import time
import uuid
from typing import Callable

from .. import state as _state
from ..util import now_iso


class Breaches:
    @contextlib.contextmanager
    def breach_lock(self):
        _state.hostdirs.ensure(self.dir)
        fd = os.open(self.dir / _state.BREACH_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
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
        _state._atomic_write(self.breach, data)

    def breach_get(self, number: int) -> dict:
        return _state._mapping(_state._mapping(self._load(self.breach, {})).get(f"{self.loop['repo']}#{number}"))

    def breach_set(self, number: int, entry: dict) -> dict:
        key = f"{self.loop['repo']}#{number}"
        with self.breach_lock():
            data = _state._mapping(self._load(self.breach, {}))
            prior = _state._mapping(data.get(key))
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
        with self.breach_lock():
            if not current():
                return "stale"
            data = _state._mapping(self._load(self.breach, {}))
            prior = _state._mapping(data.get(key))
            new = prior.get("head") != head
            if not new and (prior.get("status") != "delivery-pending"
                            or _state._delivery_in_flight(prior)):
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
                _state.log(f"breach observer notification failed: {exc}")
        try:
            delivered = send(marker)
        except Exception as exc:
            _state.log(f"adjudicator delivery failed: {exc}")
            delivered = False
        with self.breach_lock():
            data = _state._mapping(self._load(self.breach, {}))
            latest = _state._mapping(data.get(key))
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


    def breach_clear_pending(self, number: int, head: str, shadow_rounds: int | None = None) -> bool:
        """Drop a ``delivery-pending`` marker at ``head`` that no longer holds it (a raised cap
        or a dismissed verdict). Only that status, that head, and no delivery in flight. With
        ``shadow_rounds``: only a shadow escalation of that round, awaiting its ruling too, whose
        shadow review agrees by the ruling's launch (#2688), so a later dissent escalates anew. Its
        run clears it only once the ledger shows that run failed (#2688 review F1): a dissent that
        escalates after that re-arms the run, and one before it finds this marker ("already"), so
        none is parked on a run that was already ending."""
        key = f"{self.loop['repo']}#{number}"
        with self.breach_lock():
            data = _state._mapping(self._load(self.breach, {}))
            marker = _state._mapping(data.get(key))
            statuses = ("delivery-pending",)
            if shadow_rounds is not None:
                if ((marker.get("escalated") or {}).get("by") != "shadow"
                        or int(marker.get("rounds") or 0) != shadow_rounds):
                    return False
                statuses += ("awaiting-adjudication",)
            if (marker.get("head") != head or marker.get("status") not in statuses
                    or _state._delivery_in_flight(marker)):
                return False
            data.pop(key)
            self._breach_save(data)
            return True

    def breach_claim(self, number: int, head: str) -> dict | None:
        """Claim exactly one wake for this PR/head across gateway processes."""
        key = f"{self.loop['repo']}#{number}"
        with self.breach_lock():
            data = _state._mapping(self._load(self.breach, {}))
            marker = data.get(key)
            if (not isinstance(marker, dict) or marker.get("pr") != number
                    or marker.get("head") != head
                    or not (marker.get("status") == "awaiting-adjudication"
                            or (marker.get("status") == "delivery-pending"
                                and marker.get("delivery_token")))):
                return None
            data[key] = {**{k: v for k, v in marker.items() if k != _state.BREACH_ATTEMPT},
                         "status": "adjudicating", "adjudicating_at": now_iso()}
            self._breach_save(data)
            return marker

    def breach_start(self, number: int, head: str, rounds: int,
                     attempt: str | None = None) -> dict | None:
        """Mark the breach at this head as being ruled on by an isolated adjudicator turn.

        The run ledger's unique turn index is what makes a ruling at-most-once; this marks the
        marker so the watchdog stops retrying delivery and ``explain`` says a ruling is out.
        Unlike ``breach_claim`` it does not need a live delivery token: the enqueue may have
        been durable even when its delivery attempt reported failure (a spawn error), and that
        attempt's token is gone by the time the worker runs. A marker already claimed by
        anyone else — including a legacy gateway route — refuses.

        ``attempt`` (``claims.attempt_of``) is stamped on the marker as the launch that started
        it, so a hand-back naming that attempt (``breach_resume``) can tell its own start from
        another actor's or a retry's (#1767).
        """
        key = f"{self.loop['repo']}#{number}"
        with self.breach_lock():
            data = _state._mapping(self._load(self.breach, {}))
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
            data[key].pop(_state.BREACH_ATTEMPT, None)
            if attempt is not None:
                data[key][_state.BREACH_ATTEMPT] = attempt
            self._breach_save(data)
            return marker

    def breach_resume(self, number: int, head: str, rounds: int,
                      attempt: str | None = None) -> dict | None:
        """Undo ``breach_start`` for an isolated adjudicator turn that failed before recording
        any ruling (issue #53), so its retry can start it again. Only this head and round
        count, and only from ``adjudicating``; anything else is left alone.

        With ``attempt``, only a marker that attempt started (its ``breach_start`` stamp) is
        handed back (#1767): never one a legacy gateway route claimed, another run started,
        or the same run's retry started again since."""
        key = f"{self.loop['repo']}#{number}"
        with self.breach_lock():
            data = _state._mapping(self._load(self.breach, {}))
            marker = data.get(key)
            if (not isinstance(marker, dict) or marker.get("pr") != number
                    or marker.get("head") != head or marker.get("rounds") != rounds
                    or marker.get("status") != "adjudicating"
                    or (attempt is not None and marker.get(_state.BREACH_ATTEMPT) != attempt)):
                return None
            data[key] = {**{k: v for k, v in marker.items() if k != _state.BREACH_ATTEMPT},
                         "status": "awaiting-adjudication"}
            self._breach_save(data)
            return data[key]

    def breach_reopen(self, number: int, head: str, rounds: int, reason: str) -> bool:
        """Re-arm a ruled-on marker at ``head`` for a further ruling, as round ``rounds + 1``
        (#1111: the shadow dissented again after the ruling). Compare-and-swap on this head,
        round and ``adjudicating``, so one caller re-arms it; ``gate.breach`` then delivers the
        new round's turn (its own turn key)."""
        key = f"{self.loop['repo']}#{number}"
        with self.breach_lock():
            data = _state._mapping(self._load(self.breach, {}))
            marker = data.get(key)
            if (not isinstance(marker, dict) or marker.get("pr") != number
                    or marker.get("head") != head or marker.get("rounds") != rounds
                    or marker.get("status") != "adjudicating"):
                return False
            data[key] = {**{k: v for k, v in marker.items()
                            if k not in ("adjudicating_at", _state.BREACH_ATTEMPT)},
                         "rounds": rounds + 1, "reason": reason, "at": now_iso(),
                         "status": "delivery-pending"}
            self._breach_save(data)
            return True

    def breach_all(self) -> dict:
        return _state._kept(self._load(self.breach, {}), _state._breach_marker)
