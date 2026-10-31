"""#479: the ready-to-merge queue behind ``hermes dk ready``.

Read-only: it lists the open PRs an operator can merge now, in the order to merge them, and names
the reason for every PR it leaves out. The loop never merges.

A PR is ready when it is approved at its head (the same effective-review predicate the gates use),
that approval is not stale (#473, ``stale_approval.is_stale``), the required checks at the head are
green (``ci.gating``), GitHub calls it mergeable, and nothing holds it: no draft, open gate failure,
human-paths hold, escalation, shadow-review hold (#1004) or same-head retarget quarantine. A retarget quarantine blocks only
while its fresh review is owed: a host-receipted approval at the head ends it (#791). Anything
unreadable is an exclusion, never a guess. An issue-fix hold (``st.fix_holds()``) is not a hold on a PR: it means
the issue waits for its origin PR to merge, so it never excludes that PR (#759).

Order: a stacked PR (its base is another PR's branch) comes after that parent, and is excluded
while the parent is not itself ready. PRs that change the same files are merged one after another
(lowest number first), each annotated with the one before it, since the later ones need
re-checking once the earlier one has landed.

The watchdog sweep (``sweep`` below, never ``hermes dk ready`` itself) sends one ``ready_set``
observer notice when the set changes (#654).

Needs a ``required_checks`` list in the loop to mean "required": with none, every check that
reports gates, as everywhere else. GitHub branch protection is not read; ``mergeable_state``
``blocked`` (a protection rule not met) excludes the PR.
"""
from __future__ import annotations

from . import ci, config, explain, gate, gate_failures, gh, stale_approval, transition

# GitHub mergeable_state values that leave a PR mergeable (``unstable`` is only a non-required
# check failing; the required ones are judged by ``ci.gating`` above).
MERGEABLE_STATES = ("clean", "has_hooks", "unstable")


def _changed_since_listing(pr: dict, detail: dict) -> list[str]:
    """Fields of the fresh single-PR read that differ from the listing the approval was judged on."""
    changed = []
    if detail.get("number") != pr.get("number"):
        changed.append("number differs")
    if detail.get("state") != "open":
        changed.append(f"state is {detail.get('state') or 'unknown'}")
    if detail.get("draft") is not False:
        changed.append("now a draft" if detail.get("draft") else "draft flag unreadable")
    if (detail.get("head") or {}).get("sha") != (pr.get("head") or {}).get("sha"):
        changed.append("head moved")
    if (detail.get("base") or {}).get("ref") != (pr.get("base") or {}).get("ref"):
        changed.append("base retargeted")
    return changed


# The kinds of reason ``assess_kinds`` gives (#1163 M2b), for the ``diaktoros/review`` merge check:
# PENDING can still clear on its own (a review, the shadow, CI or a ruling still out, or a read
# that failed and is retried); FAILURE needs a change or a person (changes requested, red CI, a
# conflict, a hold, a ruling the operator decides). GITHUB is GitHub's ``blocked``: with the merge
# check required GitHub reads ``blocked`` *because* the check is not green, so the check never
# counts it (it would wait on itself). OUTSIDE: not a loop PR.
PENDING, FAILURE, GITHUB, OUTSIDE = "pending", "failure", "github", "outside"


# A required check that has not reported yet is pending this long after its head is first seen:
# a workflow takes a moment to create its check runs after a push (shadow review of #1225).
MISSING_GRACE_S = 15 * 60


def _clock(value, now: float) -> float | None:
    """A persisted clock, or ``None`` when it is corrupt or in the future (#1235): the
    watchdog's ``valid_clock`` rule, so a bad value never ends a grace early."""
    import math
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and 0 < value <= now else None


def _past_start_grace(st, number, head: str) -> bool:
    """Whether the head has been seen long enough that a missing required check is not just one
    still starting. A head the watchdog has not recorded yet (or an unreadable record) is not.
    The grace runs from when the watchdog first saw the head (#1236: a draft opened before arming
    and marked ready later is new to the loop then), else, for a record older than that field,
    from ``armed_since`` (#1232)."""
    import time
    now = time.time()
    watch = st.watch()
    entry = (watch.get("heads") or {}).get(str(number)) if isinstance(watch, dict) else None
    if not isinstance(entry, dict) or entry.get("sha") != head:
        return False
    for value in (entry.get("observed_at"), entry.get("first_seen_at"), watch.get("armed_since")):
        seen = _clock(value, now)
        if seen is not None:
            return now - seen >= MISSING_GRACE_S
    return False


def assess(loop: dict, st, pr: dict, reviews, ci_state, detail: dict | None) -> list[str]:
    """Why one open PR is not ready to merge; ``[]`` when nothing excludes it.

    ``reviews`` and ``ci_state`` are ``None`` when unreadable; ``detail`` is the single-PR read
    (it carries ``mergeable_state``), ``None`` when unreadable. Stacking is judged by the caller.
    """
    return [text for _kind, text in assess_kinds(loop, st, pr, reviews, ci_state, detail)]


def assess_kinds(loop: dict, st, pr: dict, reviews, ci_state,
                 detail: dict | None) -> list[tuple[str, str]]:
    """``assess``'s reasons, each with its kind (``PENDING``/``FAILURE``/``GITHUB``/``OUTSIDE``)."""
    why: list[tuple[str, str]] = []

    def add(kind: str, text: str) -> None:
        why.append((kind, text))

    approved = False
    number = pr.get("number")
    head = (pr.get("head") or {}).get("sha") or ""
    author = str((pr.get("user") or {}).get("login") or "").lower()
    if not head:
        return [(PENDING, "head missing or malformed")]
    if pr.get("draft"):
        add(PENDING, "draft PR")
    if author not in config.reviewed_authors(loop):
        add(OUTSIDE, f"author {author or '?'} is not one of this loop's fixers or review-only authors")

    if not isinstance(reviews, list):
        add(PENDING, "review list unreadable: approval unknown")
    else:
        reviews = transition.effective_reviews(loop, st, number, head, reviews)
        latest = (gate.latest_effective_review_at_head(reviews, loop, head)
                  if isinstance(reviews, list) else None)
        if not isinstance(reviews, list):
            add(PENDING, "review receipts unreadable: approval unknown")
        elif latest is not None and gh.review_state(latest) == "CHANGES_REQUESTED":
            # #1245: a red merge check names the verdict, with the budget explain counts.
            add(FAILURE, f"changes requested at head {head[:7]} "
                         f"({len(gate.verdicts(reviews, loop))} of {loop.get('cap')} verdicts)")
        elif latest is None or gh.review_state(latest) != "APPROVED":
            # No verdict at the head yet is a review still to come.
            add(PENDING, f"not approved at head {head[:7]}")
        else:
            approved = True
            if stale_approval.is_stale(st.watch(), number, head):
                add(FAILURE, stale_approval.line(stale_approval.record(st.watch(), number)))

    gating = ci.gating(ci_state, config.required_checks(loop))
    if gating is None:
        add(PENDING, "CI unreadable at this head")
    elif not gating.green:
        bits = [f"{label} {ci._names(names)}" for label, names in
                (("failed:", gating.failed), ("cancelled:", gating.cancelled),
                 ("still running:", gating.pending), ("never reported:", gating.missing))
                if names]
        # A required check that never reported is for the operator (a misnamed or dropped
        # workflow, ci.approval_refusal): nothing ends that wait on its own.
        add(FAILURE if gating.failed or gating.cancelled
            or (gating.missing and _past_start_grace(st, number, head)) else PENDING,
            "required checks not green — " + "; ".join(bits))

    if not isinstance(detail, dict):
        add(PENDING, "mergeability unreadable")
    elif changed := _changed_since_listing(pr, detail):
        add(PENDING, "not ready (changed since listing): " + ", ".join(changed))
    else:
        mstate = detail.get("mergeable_state")
        if detail.get("mergeable") is False or mstate == "dirty":
            add(FAILURE, "not mergeable: conflicts with its base")
        elif mstate not in MERGEABLE_STATES:
            # Only ``blocked`` is GitHub reflecting the rules (the merge check among them) back:
            # ``behind`` needs an update, and an unknown or uncomputed state is not decided yet.
            said = f"not mergeable: GitHub reports {mstate or 'no merge state yet'}"
            if mstate == "blocked" and config.reviewer_is_app(loop):
                # #1163 M2c: GitHub never counts an App's approval, so a base that still requires
                # approvals blocks every PR until a person approves; doctor reads the base.
                said += (" (the reviewer is a GitHub App, whose approvals GitHub never counts: "
                        "a base that requires approvals needs a person's; `hermes dk doctor` "
                        "reads it)")
            add(GITHUB if mstate == "blocked" else FAILURE if mstate == "behind" else PENDING, said)

    try:
        for entry in gate_failures.open_for(loop, number):
            add(FAILURE, "open gate failure: " + explain.gate_failure_line(entry)[:200])
    except Exception as exc:
        add(PENDING, f"gate-failure ledger unreadable: {type(exc).__name__}")
    held = st.human_hold(number, head)
    if isinstance(held, dict):
        add(FAILURE, f"waiting for a person: {held.get('why') or 'touches human_paths'}")
    marker = st.breach_get(number)
    overruled = gate.shadow_overruled(loop, marker, head)        # #1004: ACCEPT, approval stands
    if overruled:
        pass
    elif gate.escalated(marker, head) or gate.breach_delivery_status(
            marker if isinstance(marker, dict) else {}, head) in {"awaiting-adjudication",
                                                                  "adjudicating"}:
        # Waiting for the ruling; once it is in, the decision is the operator's.
        # With no adjudicator route no ruling ever comes: a person decides (shadow, #1225).
        # The reason says which (#1245): a stale "awaiting" after the ruling misled.
        ruled = explain._ruled(loop, number, head)
        if ruled:
            add(FAILURE, f"escalated: the adjudicator ruled {ruled} at head {head[:7]}; a person "
                         "decides (merge, close, or push a new head)")
        elif not (loop.get("adjudicator") or {}).get("route"):
            add(FAILURE, "escalated, and this loop has no adjudicator route: a person decides")
        else:
            add(PENDING, "escalated: parked awaiting adjudication")
    shadow_kind, shadow_held, _note = gate.shadow_state(loop, number, head, marker=marker)
    if shadow_held:
        add(shadow_kind or PENDING, "shadow review: " + shadow_held)
    # The hold blocks only while the fresh review is still owed. ``reviews`` above are already the
    # host-receipted post-boundary ones, so an approval found there ends the hold's veto, as in
    # the gates (#791). An unreadable review list never counts as approved.
    if not approved and transition.hold(st, number, head):
        add(PENDING, "same-head base retarget hold")
    return why


def _depth(by_number: dict, number: int) -> int:
    depth, seen = 0, {number}
    pr = by_number[number]
    while True:
        parent = pr["_parent"]
        if parent is None or parent in seen:
            return depth
        seen.add(parent)
        depth += 1
        pr = by_number[parent]


def build(loop: dict, st, prs: list[dict], reads: dict) -> tuple[list[dict], list[dict]]:
    """``(ready in merge order, excluded)`` from the open PRs and per-number reads.

    ``reads[number]`` is ``{"reviews", "ci", "detail", "files"}`` (``files``: names, or None when
    unreadable). Each ready entry is ``{"number","title","after":[...],"note"}``; each excluded
    one is ``{"number","title","why":[...]}``.
    """
    out, kinds = build_kinds(loop, st, prs, reads)
    titles = {p["number"]: str(p.get("title") or "")[:80] for p in prs
              if isinstance(p, dict) and isinstance(p.get("number"), int)}
    excluded = [{"number": n, "title": titles.get(n, ""), "why": [text for _k, text in why]}
                for n, why in sorted(kinds.items()) if why]
    return out, excluded


def build_kinds(loop: dict, st, prs: list[dict],
                reads: dict) -> tuple[list[dict], dict[int, list[tuple[str, str]]]]:
    """``(ready in merge order, {number: reasons with their kind})`` for every open PR: ``build``'s
    whole judgement, the stacking and merge-order reasons included, for the merge check (#1163)."""
    open_prs = [p for p in prs if isinstance(p, dict) and isinstance(p.get("number"), int)]
    by_branch = {(p.get("head") or {}).get("ref"): p["number"] for p in open_prs
                 if (p.get("head") or {}).get("ref")}
    info: dict[int, dict] = {}
    for pr in open_prs:
        number, read = pr["number"], reads.get(pr["number"]) or {}
        why = assess_kinds(loop, st, pr, read.get("reviews"), read.get("ci"), read.get("detail"))
        base = (pr.get("base") or {}).get("ref")
        parent = by_branch.get(base) if base and base != loop["base"] else None
        if base and base != loop["base"] and parent is None:
            why.append((PENDING, f"stacked on {base}, which is not an open PR branch: base "
                                 "unverified"))
        info[number] = {"number": number, "title": str(pr.get("title") or "")[:80], "why": why,
                        "_parent": parent, "files": read.get("files")}
    # A stacked PR is ready only once its parent is; parents are judged first (shallowest first).
    for number in sorted(info, key=lambda n: (_depth(info, n), n)):
        entry = info[number]
        parent = entry["_parent"]
        if parent is not None and info[parent]["why"]:
            entry["why"].append((PENDING, f"stacked on #{parent}, which is not ready"))
        if entry["files"] is None and not entry["why"]:
            entry["why"].append((PENDING, "changed files unreadable: merge order unknown"))
    ready = sorted((e for e in info.values() if not e["why"]),
                   key=lambda e: (_depth(info, e["number"]), e["number"]))
    out: list[dict] = []
    for entry in ready:
        after = [f"#{entry['_parent']}"] if entry["_parent"] is not None else []
        shared = []
        for earlier in out:
            common = set(entry["files"]) & set(info[earlier["number"]]["files"])
            if common:
                shared.append((earlier["number"], sorted(common)))
        note = "; ".join(
            f"same files as #{n} ({', '.join(f[:3])}{'…' if len(f) > 3 else ''}): merge after it, "
            "then re-check this one" for n, f in shared)
        out.append({"number": entry["number"], "title": entry["title"],
                    "after": after, "note": note})
    return out, {n: e["why"] for n, e in info.items()}


def _reads(loop: dict, prs: list) -> dict:
    """GitHub, once per open PR: its reviews, CI, single-PR read and changed files."""
    reads: dict = {}
    for pr in prs:
        number = pr.get("number") if isinstance(pr, dict) else None
        head = (pr.get("head") or {}).get("sha") or "" if isinstance(pr, dict) else ""
        if not isinstance(number, int) or not head:
            continue
        reviews, _ = gh.reviews_read(loop, number)
        detail, _ = gh.fetch(loop, gh.pr_path(loop, number))
        files, _ = gh.pr_files_read(loop, number)
        reads[number] = {"reviews": reviews, "ci": ci.read(loop, head), "detail": detail,
                         "files": ([str(f.get("filename")) for f in files if isinstance(f, dict)]
                                   if isinstance(files, list) else None)}
    return reads


def collect(loop: dict, st) -> tuple[list[dict], list[dict], str]:
    """Read GitHub once per open PR and build the queue; the last item is a listing error."""
    prs, error = gh.open_prs_read(loop)
    if prs is None:
        return [], [], error or "open PR listing unreadable"
    ready, excluded = build(loop, st, prs, _reads(loop, prs))
    return ready, excluded, ""


def collect_kinds(loop: dict, st) -> tuple[list[dict], dict[int, list[tuple[str, str]]], str]:
    """``(open PRs, {number: reasons with kinds}, listing error)``: ``collect``'s reads and
    judgement, kept per PR for the merge check (#1163 M2b)."""
    prs, error = gh.open_prs_read(loop)
    if prs is None:
        return [], {}, error or "open PR listing unreadable"
    _ready, kinds = build_kinds(loop, st, prs, _reads(loop, prs))
    return [p for p in prs if isinstance(p, dict) and isinstance(p.get("number"), int)], kinds, ""


FILE = "ready-set.json"


def _outcome(numbers: list[int]) -> str:
    if not numbers:
        return "nothing is ready to merge now"
    return "ready to merge, in order: " + ", ".join(f"#{n}" for n in numbers)


def sweep(loop: dict, st, log=lambda _msg: None) -> bool:
    """One watchdog pass over the ready set (#654). True when a notice was raised now.

    The last set is kept beside ``main-ci.json``. No baseline means no news: the first set is
    recorded silently. A set that becomes empty is a change. Nothing is read from GitHub while the
    feed is unconfigured or does not subscribe to ``ready_set``; an unreadable listing is no news.
    """
    import time

    from . import observer
    if observer.configured(loop) or not observer.subscribes(loop, "ready_set"):
        return False
    ready, _excluded, error = collect(loop, st)
    if error:
        log(f"ready set unreadable: {error}")
        return False
    numbers = [e["number"] for e in ready]
    path = st.dir / FILE
    with st.locked():
        memory = st._load(path, {})
        previous = memory.get("ready") if isinstance(memory, dict) else None
        st._save(path, {"ready": numbers})
    if not isinstance(previous, list) or previous == numbers:
        return False                                  # first run, or nothing changed
    observer.notify(loop, st, "ready_set", numbers[0] if numbers else 0, "",
                    identity=f"{','.join(map(str, numbers)) or 'none'}#{int(time.time())}",
                    outcome=_outcome(numbers), next_turn="you merge" if numbers else "")
    log(f"ready set changed: {_outcome(numbers)}")
    return True
