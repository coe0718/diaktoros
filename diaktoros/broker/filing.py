"""The issues the broker files (#1349 split): the reviewer's own (#247), and those a shadow
review (#914) and a ruling (#1864) ask for, which the reader files, with their labels.

Moved unchanged from ``broker``. What they share with the broker's other writes (``_write``,
``_audit``, the identity check, ``BrokerDenied``) is read from ``broker`` at call time, so a
patch there reaches them too.
"""
from __future__ import annotations

import sys

from .. import broker, gh


# What a reviewer may file from its issue-tier findings (#247): a few short issues, each a title,
# a body and labels from the loop's triage list — nothing else (no assignee, no mention target, no
# milestone, no edit to an existing issue).
FILED_ISSUES_MAX = 3
FILED_ISSUE_BODY_MAX = 6 * 1024


def lineage_note(*, pr: int, head: str, depth: int, by: str = "the Diaktoros reviewer") -> str:
    """The host's line under a filed issue: where it came from, and how deep the chain is."""
    note = (f"\n\n---\nFiled by {by} from PR #{pr} (head `{head[:7]}`). "
            f"Lineage depth {depth}.")
    if depth > 1:
        note += (" This finding came from reviewing an automatic fix of a filed issue: a person "
                 "decides whether it goes back to the fixer.")
    return note


def file_issue(loop: dict, *, repo: str, number: int, head: str, login: str, title: str,
               body: str, labels: list[str], depth: int) -> int:
    """POST one issue as the reviewer (#247): its number, or raise. Title, body and labels only."""
    payload = {"title": title, "labels": labels,
               "body": broker._signed(loop, body.strip() + lineage_note(pr=number, head=head,
                                                                 depth=depth),
                               "reviewer", head)}
    result, why = broker._write(loop, f"/repos/{repo}/issues", method="POST", body=payload, login=login)
    if (not isinstance(result, dict) or type(result.get("number")) is not int
            or result["number"] <= 0 or "pull_request" in result):
        raise broker._denied("GitHub issue write did not return a successful response", why)
    broker._audit(loop, repo, number, head, "", "reviewer", "file_issue", login)
    return result["number"]


SHADOW_FOUND_LABEL = "shadow-found"     # every issue a shadow review found carries it (#914)


def file_shadow_issue(loop: dict, *, repo: str, number: int, head: str, title: str, body: str,
                      labels: list[str], model: str, depth: int) -> tuple[int, str]:
    """POST one issue a shadow review found (#914), as the **reader**: the reviewer did not find
    it. ``(number, gap)``, or raise: gap says which labels it could not carry ('' when all do,
    #1589). The labels were checked when the shadow asked (the loop's triage list, never the fix
    label); ``shadow-found`` is added here. Host-built header and footer."""
    header = (f"Found by a shadow review (`{model}`) of #{number} at `{head[:7]}`. The reviewer "
              "did not file it. A shadow review is a second model that writes nothing; the host "
              "filed this for it.\n\n")
    return _file_as_reader(loop, repo=repo, number=number, head=head, title=title,
                           labels=[*labels, SHADOW_FOUND_LABEL], seat="shadow",
                           operation="file_shadow_issue",
                           text=header + body.strip() + lineage_note(
                               pr=number, head=head, depth=depth,
                               by="the Diaktoros host for a shadow review"))


ADJUDICATOR_FOUND_LABEL = "adjudicator-found"   # every issue a ruling's follow-up became (#1864)


def file_ruling_issue(loop: dict, *, repo: str, number: int, head: str, title: str, body: str,
                      labels: list[str], verdict: str, depth: int) -> tuple[int, str]:
    """POST one follow-up an adjudicator ruled valid but not blocking (#1864), as the
    **reader**, like a shadow's: ``(number, gap)``, or raise. ``adjudicator-found`` is added; the
    header names the PR it follows up and the ruling, so a person (and ``fix_hold``, from its
    ``filed_issues`` row) knows it waits on that PR."""
    header = (f"Follow-up to #{number}: a finding the adjudicator ruled valid but not blocking, "
              f"in its {verdict} ruling at `{head[:7]}`. The host filed it for the adjudicator, "
              "which writes nothing but its ruling.\n\n")
    return _file_as_reader(loop, repo=repo, number=number, head=head, title=title,
                           labels=[*labels, ADJUDICATOR_FOUND_LABEL], seat="adjudicator",
                           operation="file_ruling_issue",
                           text=header + body.strip() + lineage_note(
                               pr=number, head=head, depth=depth,
                               by="the Diaktoros host for an adjudicator ruling"))


def _file_as_reader(loop: dict, *, repo: str, number: int, head: str, title: str,
                    labels: list[str], text: str, seat: str, operation: str) -> tuple[int, str]:
    """POST one host-built issue as the reader (#914, #1864): ``(number, gap)``, or raise
    ``BrokerNotSent`` when nothing was written, ``BrokerDenied`` when the outcome is unknown."""
    if repo != loop.get("repo"):
        raise broker.BrokerNotSent("repository mismatch")
    login = str(loop.get("read_token") or "")
    if not login:
        raise broker.BrokerNotSent("no reader identity")
    payload = {"title": title, "labels": list(dict.fromkeys(labels)),
               "body": broker._signed(loop, text, seat, head)}
    # Cleared first, so "never sent" is judged on this write alone: the flag is the thread's
    # latest call's, and a stale one from an earlier call would turn an unknown outcome (a 5xx,
    # a timeout) into a retry, and a retry into a second copy of the issue.
    gh._LAST.unsent = False
    gh._LAST.status = None
    result, why = broker._write(loop, f"/repos/{repo}/issues", method="POST", body=payload, login=login)
    if (not isinstance(result, dict) or type(result.get("number")) is not int
            or result["number"] <= 0 or "pull_request" in result):
        if result is None and gh.last_unsent():
            # The reader's credential is missing or unreadable: refused before any request.
            raise broker.BrokerNotSent(broker._denied("no credential to send the issue write with", why).args[0])
        status = gh.last_status()       # the response's own status; None when no answer arrived
        if result is None and status is not None and 400 <= status < 500 and status != 408:
            # GitHub rejected the request (no Issues write, a bad label): no issue was created.
            raise broker.BrokerNotSent(broker._denied("GitHub refused the issue write", why).args[0])
        raise broker._denied("GitHub issue write did not return a successful response", why)
    broker._audit(loop, repo, number, head, "", "reader", operation, login)
    return result["number"], _shadow_labels(loop, repo, number, head, result, payload["labels"],
                                            login, operation.removeprefix("file_"))


def _shadow_labels(loop: dict, repo: str, number: int, head: str, issue: dict,
                   wanted: list[str], login: str, kind: str = "shadow_issue") -> str:
    """Add the labels GitHub dropped from a new shadow issue (#1589); '' when it carries them
    all, else why not. GitHub drops create-time labels, silently, for a token that can't set them
    on a new issue (push access), so they are added with the labels endpoint after the POST. The
    issue stands either way, and is never filed again: a gap is reported (and audited), never
    raised, and neither is a failure to write either audit line once GitHub has confirmed the
    issue (#1622 review)."""
    def names(found) -> set:
        return {str(x.get("name")).casefold() for x in found or [] if isinstance(x, dict)}
    missing = [x for x in wanted if x.casefold() not in names(issue.get("labels"))]
    if not missing:
        return ""
    gh._LAST.status = None      # judged on this write alone, never an earlier call's answer
    gh._LAST.unsent = False
    try:
        added, why = broker._write(loop, f"/repos/{repo}/issues/{issue['number']}/labels",
                            method="POST", body={"labels": missing}, login=login)
        status, unsent = gh.last_status(), gh.last_unsent()
    except (Exception, gh.GateBudgetExceeded) as exc:  # noqa: BLE001 - filed: report, never raise
        # The sweep's budget running out here (#1705) is a gap too: raised past this point it
        # would leave the filed issue's ledger rows `posting`/`recorded`, its number lost.
        added, why, status, unsent = None, f"{type(exc).__name__}: {exc}", None, False
    if isinstance(added, list):
        _audit_confirmed(loop, repo, number, head, f"{kind}_labels", login)
        missing = [x for x in missing if x.casefold() not in names(added)]
    if not missing:
        return ""
    _audit_confirmed(loop, repo, number, head, f"{kind}_labels_missing", login)
    return _label_gap(missing, why, status, added is None and not unsent, login, repo)


def _label_gap(missing: list[str], why: str, status: int | None, unanswered: bool, login: str,
               repo: str) -> str:
    """Why a new issue still lacks ``missing`` after the labels write (#1641). Only a 403 answer
    that is not a rate limit is a confirmed permission refusal; a write sent with no answer (a
    timeout, a dropped connection) leaves the outcome unknown; any other failure is reported as
    it is, without a permission claim."""
    labels = ", ".join(missing)
    if status == 403 and "rate limit" not in (why or "").casefold():
        return (f"GitHub did not apply {labels} ({why or 'HTTP 403'}): the reader {login} needs "
                f"permission to label issues in {repo}")
    if unanswered and status is None:
        return (f"the label write for {labels} got no answer ({why or 'no response'}): whether "
                "GitHub applied them is unknown; check the issue's labels")
    return f"GitHub did not apply {labels} ({why or 'the label write left them off'})"


def _audit_confirmed(loop: dict, repo: str, number: int, head: str, operation: str,
                     login: str) -> None:
    """The reader's audit line after GitHub confirmed the shadow issue: a failure to write it is
    said on stderr, never raised, so the filed issue's number is never lost to it."""
    try:
        broker._audit(loop, repo, number, head, "", "reader", operation, login)
    except Exception as exc:  # noqa: BLE001 - the issue exists: its outcome stands
        print(f"diaktoros broker: audit record {operation} not written for #{number}: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
