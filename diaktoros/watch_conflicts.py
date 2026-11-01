"""The watchdog's conflict step: which loop PRs conflict with their base, and the turn for each.

Moved from ``scripts/watchdog.py`` unchanged (#1349). The sweep and the merge-triggered conflict
scan (``conflict_scan``) both call it, so both detect and act the same way.
"""

from __future__ import annotations

import re
import time

from diaktoros import config, conflict_person, gate, gh, observer, pr_hold
from diaktoros import review_only_conflict, watch_mode
from diaktoros import state as state_mod
from diaktoros.util import log

# GitHub computes a PR's mergeability lazily: the first read after its base moves usually
# answers null/"unknown" and starts the computation. One short wait, then one re-read.
MERGEABILITY_WAIT_S = 5.0


def mergeability(loop: dict, live, head: str, author: str) -> str:
    """``conflict``, ``unknown`` (GitHub has not computed it yet) or ``""`` for a loop PR read
    live at ``head``. A review-only PR that merely fell behind counts as a conflict too, when
    the host may update it."""
    if not isinstance(live, dict) or (live.get("head") or {}).get("sha") != head:
        return ""
    stale = ("dirty", "behind") if (author in config.review_only(loop)
                                    and config.review_only_update(loop)) else ("dirty",)
    if live.get("mergeable_state") in stale:
        return "conflict"
    if live.get("mergeable") is None or live.get("mergeable_state") in (None, "unknown"):
        return "unknown"
    return ""


def recheck_unknown(loop: dict, unknown: list[tuple[int, str, str]],
                    conflict_live: dict[int, dict]) -> list[tuple[int, str, str, str]]:
    """Re-read, once and after one shared wait, the PRs whose mergeability was not computed yet:
    otherwise a conflict a merge just caused waits for the next sweep."""
    if not unknown:
        return []
    if not watch_mode.TEST:
        time.sleep(MERGEABILITY_WAIT_S)
    found = []
    for number, head, author in unknown:
        live = gh.pr(loop, number)
        if mergeability(loop, live, head, author) == "conflict":
            found.append((number, head, str((live.get("base") or {}).get("sha") or ""), author))
            conflict_live[number] = live
    return found


def act_on_conflicts(loop: dict, st: state_mod.LoopState,
                     conflicts: list[tuple[int, str, str, str]],
                     conflict_live: dict[int, dict]) -> None:
    """Queue a conflict turn and say each conflicted head once (#303). The sweep and the
    merge-triggered conflict scan share it, so both act the same way; the turn key and the
    observer's per-head dedup keep a later pass from doing it twice."""
    resolving = config.unattended_fixer_push_enabled(loop)
    tip = base_tip(loop) if resolving and conflicts else None
    for number, head, _pr_base_sha, author in conflicts:
        # A review-only author's PR (#191) is never the fixer's, a conflict included.
        review_only = author in config.review_only(loop)
        # The turn is pinned to the base branch's tip, not the PR's ``base.sha``: GitHub leaves
        # that at the base commit of the PR's last update, so a conflict a later merge caused
        # would be merged against a base that never conflicted ("nothing to resolve").
        base_sha = tip or ""
        if resolving and not review_only and not base_sha:
            log(f"#{number} @ {head[:7]} conflict turn not queued: {loop['base']}'s tip "
                "unreadable (GitHub read failed); the next sweep retries")
        person = conflict_person.get(st, number, head) if resolving and not review_only else ""
        if person:
            # #1154: this head's conflict was already handed to a person; a new base tip does
            # not change that (a new push does), so no turn and no repeated failure notice.
            log(f"#{number} @ {head[:7]} conflict needs a person ({person}): not requeued")
        elif resolving and not review_only and re.fullmatch(r"[0-9a-f]{40}", base_sha):
            # #303 stage 3: one resolving fixer turn per (head, base); the ledger's turn key
            # dedups every later sweep. The worker merges and may still hand it to a person
            # (a whole-file conflict, or workflow changes the fixer's token cannot push).
            # enqueue_isolated, not block_pr_agent: that one is a gate's and ends the process.
            from diaktoros.run_supervisor import CONFLICT_KEY
            try:
                outcome = gate.enqueue_isolated(loop, "fixer", number, head,
                                                turn_key=f"{CONFLICT_KEY}{base_sha}")
                log(f"#{number} @ {head[:7]} conflict with {loop['base']} at {base_sha[:7]}: "
                    f"fixer {outcome}")
            except Exception as exc:
                log(f"#{number} @ {head[:7]} conflict turn at {base_sha[:7]} not queued: "
                    f"{type(exc).__name__}: {exc}")
        next_review_only = ""
        if review_only:
            # The cause, the files and the commands (a dry merge on the host, nothing pushed);
            # with review_only_update on, a clean merge is pushed first, once, under a lease.
            tried = ""
            try:
                # The dry merge, and an attempt that stopped, are kept per (head, base tip): a
                # sweep that sees the same pair again reads them instead of fetching (#504).
                key = (number, head, tip or base_tip(loop) or "", config.review_only_update(loop))
                cached = st.review_only_conflict(*key) if key[2] else None
                if cached:
                    facts, tried, attempt = cached
                else:
                    facts, attempt = review_only_conflict.assess(
                        loop, number, head, conflict_live.get(number) or {}), True
                if (conflict_live.get(number) or {}).get("mergeable_state") == "behind" \
                        and (facts["conflicted"] or facts["error"]):
                    continue                  # behind, not conflicting: nothing to say
                pushed = ""
                # A stop that is not a fact about the pair (pushes off, a broker refusal, a
                # failed push) keeps the dry merge but is attempted again (shadow on #1151).
                if attempt:
                    pushed, tried = review_only_conflict.update(loop, number, head, facts)
                    if not pushed and key[2] and "clean" in facts:   # the dry merge ran
                        st.review_only_conflict_set(
                            *key, facts, tried,
                            retry=isinstance(tried, review_only_conflict.Retry))
                if pushed:
                    log(f"#{number} @ {head[:7]} merged {loop['base']} in: {pushed[:7]}")
                    observer.notify(loop, st, "updated", number, pushed, identity="updated",
                                    outcome=f"merged {loop['base']} into #{number} at "
                                            f"{pushed[:7]}; review resumes",
                                    next_turn="reviewer: the new head is reviewed as usual")
                    continue
                next_review_only = review_only_conflict.message(loop, author, facts, tried)
            except Exception as exc:
                log(f"#{number} review-only conflict facts unavailable: "
                    f"{type(exc).__name__}: {exc}")
                next_review_only = (f"{author}: merge {loop['base']} into the branch "
                                    "(review-only — no fixer)")
            if (conflict_live.get(number) or {}).get("mergeable_state") == "behind":
                continue                      # behind and the host did not update: no conflict
        # Keyed by head (the observer dedups on it): a new push that still conflicts is a new
        # notice, a sweep that sees the same conflicted head again is not.
        observer.notify(loop, st, "conflict", number, head, identity="conflict",
                        outcome=f"conflicts with {loop['base']} — GitHub cannot merge it as it is",
                        next_turn=(next_review_only if review_only else
                                   f"the fixer will try to merge {loop['base']} into the branch "
                                   "and resolve it (a whole-file conflict or workflow changes "
                                   "come back to you)" if resolving else
                                   f"you: merge {loop['base']} into the branch (unattended fixer "
                                   "pushes are off, so the loop does not resolve it)"))


def scan_conflicts(loop: dict, st: state_mod.LoopState) -> int:
    """The sweep's conflict step alone, for the scan a merge requests (``conflict_scan``).

    The same PRs as the sweep (reviewed authors, not drafts, on the loop's base, not held), the
    same detection and the same action. Nothing else: stalls and alerts stay the sweep's,
    whose output is the one delivered. Returns how many conflicts it acted on.
    """
    armed = True if watch_mode.TEST else gate.hooks_read(loop)[0]
    if armed is not True:
        log("conflict scan skipped: hooks paused or unreadable")
        return 0
    prs = gh.open_prs(loop)
    if not isinstance(prs, list):
        log("conflict scan skipped: open PRs could not be listed")
        return 0
    conflicts: list[tuple[int, str, str, str]] = []
    conflict_live: dict[int, dict] = {}
    unknown: list[tuple[int, str, str]] = []
    for pr in prs:
        if (listed := config.loop_pr(loop, pr)) is None:
            continue
        number, head, author = listed
        hold, hold_error = pr_hold.read(loop, number)
        if hold or hold_error:
            continue                          # a held or unreadable hold: the sweep's to judge
        live = gh.pr(loop, number)
        verdict = mergeability(loop, live, head, author)
        if verdict == "conflict":
            conflicts.append((number, head, str((live.get("base") or {}).get("sha") or ""), author))
            conflict_live[number] = live
        elif verdict == "unknown":
            unknown.append((number, head, author))
    conflicts += recheck_unknown(loop, unknown, conflict_live)
    act_on_conflicts(loop, st, conflicts, conflict_live)
    observer.flush(loop, st, wait_s=0 if watch_mode.TEST else observer.digest_wait(loop))
    log(f"conflict scan: {len(conflicts)} conflicted PR(s) acted on")
    return len(conflicts)


def base_tip(loop: dict) -> str | None:
    """The base branch's current commit, or None when it cannot be read.

    A conflict turn merges this, not a PR's ``base.sha`` (the base as of the PR's last update).
    """
    from urllib.parse import quote
    ref = gh.api(loop, f"/repos/{loop['repo']}/git/ref/heads/{quote(loop['base'], safe='/')}",
                 login=loop["read_token"])
    obj = ref.get("object") if isinstance(ref, dict) else None
    sha = obj.get("sha") if isinstance(obj, dict) else None
    if (not isinstance(ref, dict) or ref.get("ref") != f"refs/heads/{loop['base']}"
            or not isinstance(obj, dict) or obj.get("type") != "commit"
            or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha)):
        return None
    return sha.lower()
