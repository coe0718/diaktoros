"""``hermes dk history``'s one-line cycle summary: the totals a reader asks first (how many
rounds, how many turns and tokens, how long from the first review to the merge, how the findings
ended), counted from ``history.collect``'s own structure and the ledger's runs. Nothing is
estimated: a total with a turn whose tokens or cost were not recorded says how many it misses.
"""
from __future__ import annotations

from collections import Counter

_REVIEWS = ("review", "github-review")


def summary(runs: list, data: dict) -> dict:
    """``{rounds, turns, seats, tokens, tokens_unrecorded, cost, cost_unrecorded,
    first_review_at, review_to_merge_s, findings}``. ``tokens``/``cost`` are ``None`` when no turn
    recorded one; ``review_to_merge_s`` is ``None`` unless both ends are on record."""
    tokens = [(r.get("tokens_in") or 0) + (r.get("tokens_out") or 0) for r in runs
              if r.get("tokens_in") is not None or r.get("tokens_out") is not None]
    costs = [r["cost"] for r in runs if r.get("cost") is not None]
    reviews = [e["at"] for rnd in data["rounds"] for e in rnd["events"]
               if e["kind"] in _REVIEWS and e["at"]]
    first = min(reviews) if reviews else None
    merged_at = (data.get("merged") or {}).get("at")
    return {"rounds": len([r for r in data["rounds"] if r["events"]]),
            "turns": len(runs),
            "seats": dict(sorted(Counter(r["seat"] for r in runs).items())),
            "tokens": sum(tokens) if tokens else None,
            "tokens_unrecorded": len(runs) - len(tokens),
            "cost": round(sum(costs), 4) if costs else None,
            "cost_unrecorded": len(runs) - len(costs),
            "first_review_at": first,
            "review_to_merge_s": (merged_at - first) if first and merged_at and
                                 merged_at >= first else None,
            "findings": dict(sorted(Counter(f["outcome"] for f in data["findings"]).items()))}


def _tokens(n: int) -> str:
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else (f"{n / 1000:.0f}k" if n >= 1000
                                                             else str(n))


def _span(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours < 48 else f"{hours // 24}d {hours % 24}h"


def line(data: dict) -> str:
    """The summary as one line: ``2 rounds · 5 turns · 410k tokens · first review to merge 9m ·
    findings: 2 raised, 1 fixed, 1 overruled``. Empty when the structure carries no summary."""
    s = data.get("summary")
    if not s:
        return ""
    parts = [f"{s['rounds']} round{'s' * (s['rounds'] != 1)}",
             f"{s['turns']} turn{'s' * (s['turns'] != 1)}"]
    if s["tokens"] is not None:
        miss = f" ({s['tokens_unrecorded']} turn(s) unrecorded)" if s["tokens_unrecorded"] else ""
        parts.append(f"{_tokens(s['tokens'])} tokens{miss}")
    elif s["turns"]:
        parts.append("tokens not recorded")
    if s["cost"] is not None:
        parts.append(f"${s['cost']:.2f}" + (" (partial)" if s["cost_unrecorded"] else ""))
    if s["review_to_merge_s"] is not None:
        parts.append(f"first review to merge {_span(s['review_to_merge_s'])}")
    if s["findings"]:
        ended = ", ".join(f"{n} {outcome}" for outcome, n in s["findings"].items())
        parts.append(f"findings: {sum(s['findings'].values())} raised, {ended}")
    return " · ".join(parts)
