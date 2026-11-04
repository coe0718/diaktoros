"""What each model request of a turn kept of the one before it, and what the cache served (#1790).

A turn is many model requests, each resending the conversation so far, so a provider's prompt
cache can serve only what a request shares with an earlier one. The reviewer's cache share was
16-54% where the shadow's is 96-98%, and the turn totals cannot say why. Two causes look alike in
them: the client rewrote earlier history (a changed item ends the shared prefix there), or the
history was kept and the provider still did not serve it from its cache.

The inference proxy records one entry per request it sends upstream, in order, as sent: past
the turn's quota (a refused request has none) and with the host's submit-now nudge, which the
next request does not carry (#1815):

- ``items``: the conversation's items (the instructions and tools count as the first);
- ``kept``: how many of the previous request's items this one starts with, unchanged (None on the
  turn's first request). ``kept == of`` is a request that only appended;
- ``of``: the previous request's item count;
- ``changed``: when it did not keep them all, the kind of the first item that differs
  (``instructions/tools``, a message role or a Responses item type), or ``dropped`` when the
  request is a shorter copy of the previous one;
- ``in`` and ``cached``: its input tokens and those the provider says its cache served (None:
  not reported).

An item's identity is its JSON as sent, key order included: a provider caches the text it is sent.
The trace is the sandbox's own requests, an operator diagnostic, never a fact the loop acts on.
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import Counter

MAX_ENTRIES = 200    # past any seat's step cap; a longer turn keeps its first requests
FIRST = 'instructions/tools'


def _items(payload: dict, mode: str) -> list:
    if mode == 'codex_responses':
        head, rest = [payload.get('instructions'), payload.get('tools')], payload.get('input')
    elif mode == 'anthropic_messages':
        head, rest = [payload.get('system'), payload.get('tools')], payload.get('messages')
    else:
        head, rest = [payload.get('tools')], payload.get('messages')
    return [head, *(rest if isinstance(rest, list) else [rest])]


def _kind(item, index: int) -> str:
    if index == 0:
        return FIRST
    if isinstance(item, dict):
        return str(item.get('type') if item.get('type') not in (None, 'message')
                   else item.get('role') or item.get('type') or 'item')[:40]
    return type(item).__name__


class Trace:
    """One turn's requests. ``see`` before a request is sent, ``usage`` once its reply is read.
    Never raises: a request it cannot read gets no entry."""

    def __init__(self, mode: str):
        self.mode = mode
        self.entries: list[dict] = []
        self._last: list[bytes] | None = None
        self._lock = threading.Lock()

    def see(self, body: bytes) -> dict | None:
        try:
            items = _items(json.loads(body), self.mode)
            digests = [hashlib.sha256(json.dumps(item).encode()).digest()[:12] for item in items]
        except Exception:
            return None
        with self._lock:
            entry = {'items': len(digests), 'kept': None, 'of': None}
            if self._last is not None:
                kept = 0
                for old, new in zip(self._last, digests):
                    if old != new:
                        break
                    kept += 1
                entry.update(kept=kept, of=len(self._last))
                if kept < len(self._last):
                    entry['changed'] = (_kind(items[kept], kept) if kept < len(digests)
                                        else 'dropped')
            self._last = digests
            if len(self.entries) < MAX_ENTRIES:
                self.entries.append(entry)
        return entry

    def usage(self, entry: dict | None, tokens_in: int, cached: int | None) -> None:
        if entry is not None:
            with self._lock:
                entry.update({'in': tokens_in, 'cached': cached})


def summary(turns: list[list]) -> dict:
    """The requests of ``turns`` (each a turn's entries), split by whether they kept the previous
    request's history. For a request that kept it all, the cache could have served about the
    previous request's input: ``served`` of ``servable`` is at most how much of that it did. A
    provider reports how many input tokens its cache served, not which, and some may be of what
    the request appended (#1814), so its cached tokens count up to that input: an upper bound,
    never an exact share. Counted only where both are reported. ``changed`` counts the first changed item's kind of the others."""
    out = {'requests': 0, 'first': 0, 'kept': 0, 'served': 0, 'servable': 0,
           'changed': Counter()}
    for entries in turns:
        previous = None
        for entry in entries if isinstance(entries, list) else ():
            if not isinstance(entry, dict):
                continue
            out['requests'] += 1
            kept, of = entry.get('kept'), entry.get('of')
            if kept is None:
                out['first'] += 1
            elif kept == of:
                out['kept'] += 1
                cached, before = entry.get('cached'), (previous or {}).get('in')
                if isinstance(cached, int) and isinstance(before, int):
                    # A cache may also hold part of what this request appended (#1814), which is
                    # not the resent part: counted up to what was resent, never past 100%.
                    out['served'] += min(cached, before)
                    out['servable'] += before
            else:
                out['changed'][str(entry.get('changed') or 'unknown')] += 1
            previous = entry
    return out


def line(seat: str, s: dict) -> str:
    """One seat's summary, as ``stats`` prints it."""
    if not s['requests']:
        return f"{seat}: no traced requests"
    rewrote = s['requests'] - s['first'] - s['kept']
    text = (f"{seat}: {s['requests']} requests in {s['first']} turns; {s['kept']} kept the "
            "previous request's history")
    if s['servable']:
        # An upper bound (review of #1813, F3): a provider says how many input tokens its cache
        # served, not which, and some may be of what the request appended.
        text += (f" (of the {s['servable']:,} tokens they resent, the cache served at most "
                 f"{s['served']:,}, {s['served'] / s['servable']:.0%}: a provider says how many "
                 "tokens it served, not which)")
    if rewrote:
        kinds = ", ".join(f"{kind} ×{n}" for kind, n in s['changed'].most_common(4))
        text += f"; {rewrote} did not" + (f" (first change: {kinds})" if kinds else "")
    return text
