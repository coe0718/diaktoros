"""The watchdog's stacked-PR step: the stacked-wait queue, and fresh reviews after a retarget.

Moved from ``scripts/watchdog.py`` unchanged (#1349).
"""

from __future__ import annotations

import hashlib
import json
import time

from diaktoros import gh, observer, situation, transition
from diaktoros import state as state_mod

def reconcile_stacked(loop: dict, st: state_mod.LoopState, watch: dict,
                      prs: list[dict], lines: list[str]) -> None:
    """Separate visibility queue; never a seat authorization or drain input."""
    previous = watch.get("stacked_wait")
    pending = dict(previous) if isinstance(previous, dict) else {}
    listed = set()
    for pr in prs:
        if not isinstance(pr, dict) or pr.get("state") != "open":
            continue
        number = pr.get("number")
        if type(number) is not int or number <= 0:
            continue
        key = str(number)
        base = (pr.get("base") or {}).get("ref")
        prior_head = (watch.get("heads") or {}).get(key)
        # A draft-only sweep can have removed the visibility entry while retaining
        # its stacked head observation. Restore that boundary before reconciling.
        if (base == loop["base"] and key not in pending and isinstance(prior_head, dict)
                and prior_head.get("base") not in (None, loop["base"])):
            pending[key] = {"head": prior_head.get("sha"), "base": prior_head["base"]}
        # Drafts are not scheduling candidates, but an observed stacked child
        # can be retargeted while draft. Quarantine before skipping draft work.
        if pr.get("draft") and not (key in pending and base == loop["base"]):
            continue
        listed.add(key)
        if (not base or base == loop["base"] or
                ((pr.get("user") or {}).get("login") or "").lower() not in loop["fixers"]):
            if key in pending:
                if base == loop["base"]:
                    live = gh.pr(loop, number)
                    if (not isinstance(live, dict) or live.get("number") != number
                            or live.get("state") != "open" or
                            (live.get("head") or {}).get("sha") != (pr.get("head") or {}).get("sha") or
                            (live.get("base") or {}).get("ref") != base or
                            (live.get("base") or {}).get("sha") != (pr.get("base") or {}).get("sha")):
                        continue
                    entry = transition.record(loop, st, number, (pr.get("head") or {}).get("sha"),
                                              base, watch=watch)
                    if entry:
                        # Owner policy (#23): the parent merged and the child is on trunk
                        # now, so start a fresh review situation — one isolated reviewer
                        # turn. Nothing from before the boundary carries over.
                        status, detail = transition.start_fresh_review(loop, st, number, live=live)
                        next_turn = {"enqueued": "reviewer queued (fresh review)",
                                     "retry": "fresh reviewer enqueue retries next sweep"}.get(
                                         status, "you")
                        lines.append(f"#{number} retargeted to {base} at the same head — old "
                                     "reviews, rounds and queued work quarantined; fresh review: "
                                     f"{status} ({detail})")
                        observer.notify(loop, st, "stall", number, entry["head"],
                                        identity=f"retarget:{entry['head']}",
                                        outcome="old same-head reviews quarantined; fresh review "
                                        f"{status}", next_turn=next_turn)
                # A retarget is not permission to reinterpret a previous seat request
                # for the same child SHA as a trunk request.
                for seat in ("reviewer", "fixer"):
                    st.queue_pop_head(seat, f"{loop['repo']}#{number}", pending[key].get("head"))
                pending.pop(key)
            continue
        resolution = situation.resolve(loop, number, listing=prs)
        head = (pr.get("head") or {}).get("sha")
        base_sha = (pr.get("base") or {}).get("sha")
        if (resolution.status == "eligible" or
                resolution.identity and (resolution.identity.head_sha != head or
                                         resolution.identity.base_ref != base or
                                         resolution.identity.base_sha != base_sha)):
            # Individual read disagrees with the listing; retry next sweep.
            continue
        branch_heads = []
        ref, visited = base, set()
        for _ in range(situation.MAX_PARENT_DEPTH):
            if ref in visited or ref == loop["base"]:
                break
            visited.add(ref)
            matches = [p for p in prs if isinstance(p, dict) and
                       isinstance(p.get("head"), dict) and p["head"].get("ref") == ref]
            branch_heads.append(sorted(
                [(p.get("number"), p["head"].get("sha"),
                  (p.get("base") or {}).get("ref"), (p.get("base") or {}).get("sha"),
                  p.get("state")) for p in matches], key=lambda item: str(item)))
            if len(matches) != 1:
                break
            ref = (matches[0].get("base") or {}).get("ref")
        generation = hashlib.sha256(json.dumps(
            [head, base, base_sha, branch_heads, resolution.status, resolution.reason,
             resolution.identity.key if resolution.identity else ""],
            separators=(",", ":")).encode()).hexdigest()
        if (pending.get(key) or {}).get("generation") == generation:
            continue
        pending[key] = {"head": head, "base": base, "base_sha": base_sha,
                        "generation": generation, "status": resolution.status,
                        "reason": resolution.reason, "parents": list(resolution.parents),
                        "identity": resolution.identity.key if resolution.identity else "",
                        "at": time.time()}
        lines.append(f"#{number} stacked {resolution.status}: {resolution.reason} "
                     "— visibility queue only; no reviewer run authorized")
        observer.notify(loop, st, "stall", number, head or "", identity=f"stacked:{generation}",
                        outcome=f"stacked {resolution.status}: {resolution.reason}", next_turn="you")
    watch["stacked_wait"] = {k: v for k, v in pending.items() if k in listed}


def retry_fresh_reviews(loop: dict, st: state_mod.LoopState, prs: list, lines: list[str],
                        since: float = 0.0) -> None:
    """Re-drive a transition's fresh reviewer turn that is not durably enqueued yet.

    Covers a failed enqueue (missing runtime, ledger or spawn failure) and a child that was
    still a draft when retargeted. Each attempt re-reads the live PR; the turn key keeps a
    success idempotent, and a failure stays visible here until one succeeds.
    """
    for pr in prs:
        if not isinstance(pr, dict) or pr.get("state") != "open" or pr.get("draft"):
            continue
        if (pr.get("base") or {}).get("ref") != loop["base"]:
            continue
        number = pr.get("number")
        head = (pr.get("head") or {}).get("sha")
        if type(number) is not int or not head:
            continue
        entry = transition.hold(st, number, head)
        if entry is None or transition.baseline_missing(entry):
            continue
        fresh = entry.get("fresh_review")
        if isinstance(fresh, dict) and (fresh.get("state") == "enqueued" or (
                isinstance(fresh.get("at"), (int, float)) and fresh["at"] >= since)):
            continue  # done, or already attempted (and reported) earlier in this sweep
        status, detail = transition.start_fresh_review(loop, st, number)
        if status == "retry":
            lines.append(f"⚠️ #{number} fresh review after retarget not enqueued: {detail} "
                         "— retrying next sweep")
        elif status == "enqueued":
            lines.append(f"#{number} fresh review after retarget enqueued ({detail})")
