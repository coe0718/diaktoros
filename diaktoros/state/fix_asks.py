"""The issue fixer's question or plan (#483): one open ask per issue, in ``fix-asks.json``.

A mixin of ``LoopState`` (split from ``state/__init__.py``, #2051 review F2): it uses the
state's ``dir``, ``locked``, ``_load`` and ``_save_strict``, and adds no state of its own.
"""

from __future__ import annotations


class FixAsks:
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

    def fix_ask_set(self, number: int, kind: str, comment_id: int, text: str,
                    run: str = "") -> None:
        """Open an ask. ``run``: the issue-fix run whose pushed branch ``continue`` posted as
        the plan (#2334); its approval is recorded, and no new fix turn is queued for it."""
        def change(data):
            data["open"][str(number)] = {"kind": kind, "comment_id": comment_id, "text": text,
                                         **({"run": run} if run else {})}
            data["count"][str(number)] = data["count"].get(str(number), 0) + 1
            data["handoff"].pop(str(number), None)
        self._fix_ask_edit(change)

    def fix_ask_claim(self, number: int, kind: str, text: str, run: str, check) -> dict:
        """Reserve the issue's one ask for ``run`` before its comment is posted (#2334 review
        F3/F4): under the state lock, ``check(self, number)`` (``fix_hold.check_ask``) raises
        when another ask is open or the asks are spent, else a ``pending`` ask with no comment
        yet is saved and counted. When ``run``'s own pending claim is already open (an earlier
        call never confirmed its comment), it is returned with ``reconcile`` set instead, and
        nothing else is counted. The caller confirms with ``fix_ask_confirm``."""
        with self.locked():
            ask = self.fix_ask_open().get(number)
            if isinstance(ask, dict) and ask.get("pending") and ask.get("run") == run:
                return {**ask, "reconcile": True}
            check(self, number)
            claim = {"kind": kind, "comment_id": None, "text": text, "run": run, "pending": True}

            def change(data):
                data["open"][str(number)] = claim
                data["count"][str(number)] = data["count"].get(str(number), 0) + 1
                data["handoff"].pop(str(number), None)
            self._fix_ask_edit(change)
            return dict(claim)

    def fix_ask_confirm(self, number: int, run: str, comment_id: int) -> None:
        """Record the posted comment of ``run``'s pending ask; from it on, a maintainer's reply
        after that comment resolves the ask. Raises when the claim is not this run's any more."""
        def change(data):
            ask = data["open"].get(str(number))
            if not isinstance(ask, dict) or ask.get("run") != run:
                raise ValueError(f"issue #{number}'s open ask is not run {run}'s")
            ask["comment_id"] = comment_id
            ask.pop("pending", None)
        self._fix_ask_edit(change)

    def fix_ask_resolve(self, number: int, reply: str, by: str, approve: bool,
                        expect: dict | None = None) -> bool:
        """Save the handoff (the ask and its reply) for the resumed turn; the ask stays open until
        ``fix_ask_drop`` (a resume that could not be queued is retried). ``expect``: the ask the
        reply was read for (the sweep's snapshot); when the open ask is no longer exactly it (it
        was closed, or another one opened meanwhile, #2382 review F1), nothing is recorded, so an
        old reply never answers a new ask. Whether it was recorded."""
        done: list[bool] = []

        def change(data):
            ask = data["open"].get(str(number))
            ask = ask if isinstance(ask, dict) else {}     # a junk entry reads as none (#80)
            if expect is not None and ask != expect:
                return
            data["handoff"][str(number)] = {"kind": ask.get("kind", ""), "text": ask.get("text", ""),
                                            "reply": reply, "by": by}
            if approve and number not in data["approved"]:
                data["approved"].append(number)
            done.append(True)
        self._fix_ask_edit(change)
        return bool(done)

    def fix_ask_drop(self, number: int, run: str = "", uncount: bool = False,
                     expect: dict | None = None, discard: bool = False) -> bool:
        """Close the issue's open ask; whether one was closed. ``run``: only when the open ask
        is that run's (#2381: ``continue`` releasing its own plan ask, never another one).
        ``uncount``: also give back the count a ``pending`` reservation took, for a claim whose
        comment is known not to be on the issue, so it never spends one of the issue's asks.
        ``expect``: only when the open ask is exactly that one (the sweep's snapshot, #2382
        review F1), so a sweep never closes an ask that opened after it read the old one.
        ``discard``: the ask is abandoned, not resumed (#2051 review F2): its saved handoff and
        the issue's plan approval go with it, in the same edit, so a later fix turn starts
        fresh (plan first again, no old reply). Nothing changes when the ask is not closed."""
        dropped: list[bool] = []

        def change(data):
            ask = data["open"].get(str(number))
            if ask is None or (run and (not isinstance(ask, dict) or ask.get("run") != run)):
                return
            if expect is not None and ask != expect:
                return
            data["open"].pop(str(number))
            if uncount and isinstance(ask, dict) and ask.get("pending"):
                data["count"][str(number)] = max(0, data["count"].get(str(number), 0) - 1)
            if discard:
                data["handoff"].pop(str(number), None)
                data["approved"] = [n for n in data["approved"] if n != number]
            dropped.append(True)
        self._fix_ask_edit(change)
        return bool(dropped)
