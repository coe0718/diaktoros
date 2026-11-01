"""The broker's issue writes (#1349): the issue fixer's one write and the reviewer's filing.

Methods of :class:`~diaktoros.broker_ipc.RunBroker`, moved unchanged: the issue fixer's branch,
PR and review request (or its one comment on the issue), the reproduction proof it carries, and
the reviewer's and shadow's ``file_issue`` / ``comment_issue`` requests. Shared names are read
from ``broker_ipc`` at call time, so a patch there reaches them.
"""
from __future__ import annotations

import re

from . import broker, config, issue_labels, repro_proof, safe_push


class IssueWrites:
    """The issue-side methods of :class:`~diaktoros.broker_ipc.RunBroker`."""

    def _issue_fix(self, raw: bytes, request: object) -> object:
        """The issue fixer's one write, then the observer's notice of how it ended (#231).

        Only a write the ledger recorded is announced: a request refused before that (a bad
        manifest, a title too long) changed nothing and the seat may resubmit it.
        """
        try:
            return self._issue_fix_write(raw, request)
        finally:
            outcome = _issue_fix_outcome(self.scope)
            if outcome:
                try:
                    loop = config.by_repo(self.scope.repo)
                except Exception:
                    loop = None
                broker_ipc._issue_notice(loop, self.scope, "fixed", outcome)

    def _issue_fix_write(self, raw: bytes, request: object) -> object:
        """The issue fixer's one write (#214): a new branch, its PR and the review request — or,
        when it could not fix the issue, one comment on the issue.

        Validated before the capability is spent; recorded in the ledger before any write;
        the branch push holds the push policy lock, like the fixer's, and its lease requires the
        branch to be absent. A write whose outcome is unknown is recorded as uncertain and never
        replayed.
        """
        from . import fix_hold, run_supervisor, state as state_mod
        from .run_supervisor import Supervisor, issue_inputs
        if self.scope.role != "issue_fixer" or not isinstance(request, dict):
            raise broker_ipc.ProtocolError("operation out of scope")
        operation = request.get("operation")
        if operation == "open_pr":
            if not {"operation", "manifest", "title", "body"} <= set(request) <= {
                    "operation", "manifest", "title", "body", "repro"}:
                raise broker_ipc.ProtocolError(
                    "open_pr takes manifest, title, body and optional repro")
            try:
                repro = repro_proof.valid_ids(request.get("repro"))
            except ValueError as exc:
                raise broker_ipc.ProtocolError(f"{exc}; nothing was written, resubmit") from None
            title, body = request["title"], request["body"]
            if (not isinstance(title, str) or not title.strip()
                    or len(title) > broker.ISSUE_PR_TITLE_MAX
                    or any(ord(ch) < 32 for ch in title)):
                raise broker_ipc.ProtocolError(
                    f"the PR title must be one line of 1-{broker.ISSUE_PR_TITLE_MAX} characters; "
                    "nothing was written, resubmit")
            safe_push._manifest(request["manifest"])
        elif operation == "issue_comment":
            if (set(request) not in ({"operation", "body"}, {"operation", "body", "kind"})
                    or len(raw) > broker_ipc.MAX_REQUEST
                    or request.get("kind", "question") not in fix_hold.ASK_KINDS):
                raise broker_ipc.ProtocolError("issue_comment takes a body, and optionally kind "
                                               "question or plan")
            body = request["body"]
        else:
            raise broker_ipc.ProtocolError("operation out of scope")
        if not isinstance(body, str) or not body.strip() or len(body.encode()) > broker.ANSWERS_MAX:
            raise broker_ipc.ProtocolError(
                f"the description must be non-empty text of at most {broker.ANSWERS_MAX} bytes; "
                "nothing was written, resubmit")
        if self._used:
            raise broker_ipc.ProtocolError("run capability already used")
        if not self.scope.run_id or not self.scope.ledger_db:
            raise broker_ipc.ProtocolError("host run ledger unavailable")
        self._used = True
        supervisor = Supervisor(self.scope.ledger_db, create=False)
        run_id, repo, number = self.scope.run_id, self.scope.repo, self.scope.number
        proof = self._prove(request["manifest"], repro) if operation == "open_pr" else None
        with config.push_policy_lock():
            current = config.by_repo(repo)
            if (current is None or current.get("id") != self._loop.get("id")
                    or current.get("state_dir") != self._loop.get("state_dir")
                    or not config.issue_fixes_enabled(current)):
                raise broker_ipc.ProtocolError("issue fixes are not enabled by the host operator")
            kind = request.get("kind") if operation == "issue_comment" else None
            st = state_mod.state_for(current)
            try:
                if kind:
                    fix_hold.check_ask(st, number)         # one open ask; the asks are capped
                elif operation == "open_pr" and fix_hold.plan_required(
                        st, number, issue_inputs.issue_fix_issue(current, number)):
                    raise ValueError("this issue needs a plan first: post it with issue_comment "
                                     "--ask plan and wait for a maintainer's /loop approve")
            except (ValueError, run_supervisor.RetryableError) as exc:
                self._used = False                         # refused before any write
                raise broker_ipc.ProtocolError(
                    f"{exc}; nothing was written, your one write is unspent")
            supervisor.record_issue_fix(run_id, repo, number, self.scope.head,
                                        "pr" if operation == "open_pr" else "comment",
                                        self.scope.branch)
            if proof is not None:
                try:
                    supervisor.record_repro_proof(run_id, repo, number, self.scope.head,
                                                  repro_proof.dumps(proof))
                except Exception as exc:    # the proof informs; it never blocks the write
                    from .util import log
                    log(f"repro proof for run {run_id} not recorded: {exc}")
            self.completed = True
            if operation == "issue_comment":
                return self._issue_comment(current, supervisor, body.strip(), kind)
            try:
                pushed = safe_push.open_branch(current, repo=repo, number=number,
                                               base=self.scope.head, branch=self.scope.branch,
                                               manifest=request["manifest"])
            except safe_push.PushFailure as exc:
                supervisor.issue_fix_status(run_id, "uncertain", error=f"push {exc.outcome}")
                if exc.outcome == "published":
                    # #824: the host read the new branch back at the commit — published, not
                    # unknown. Still held: no PR was opened from this turn.
                    self._hold_issue_fix(supervisor, "published")
                    raise broker_ipc.ProtocolError(broker_ipc.ISSUE_PUSH_PUBLISHED)
                self._hold_issue_fix(supervisor, "unknown")
                raise broker_ipc.ProtocolError(
                    "the push's outcome is unknown: do not retry; say so")
            except broker.BrokerDenied as exc:
                supervisor.issue_fix_status(run_id, "denied", error=str(exc)[:200])
                raise
        supervisor.issue_fix_status(run_id, "pushed", new_head=pushed["new_head"])
        try:
            pr = broker.open_issue_pr(current, repo=repo, number=number, branch=self.scope.branch,
                                      title=title.strip(), body=body, login=pushed["login"])
        except Exception as exc:
            supervisor.issue_fix_status(run_id, "uncertain",
                                        error=f"PR create outcome unknown: {broker_ipc._why(exc)}")
            self._hold_issue_fix(supervisor, "pr_create_unknown")
            raise broker_ipc.ProtocolError("the branch was pushed but the PR could not be "
                                           "confirmed: do not retry; say so")
        supervisor.issue_fix_status(run_id, "opened", pr_number=pr)
        try:
            broker.request_issue_pr_review(current, repo=repo, pr=pr, login=pushed["login"])
        except Exception as exc:
            # The PR is open as the fixer, so the reviewer gate's `opened` still starts the loop.
            supervisor.issue_fix_status(
                run_id, "opened", error=f"review request not confirmed: {broker_ipc._why(exc)}")
            return {"accepted": True}
        supervisor.issue_fix_status(run_id, "requested")
        return {"accepted": True}

    def _prove(self, manifest: dict, ids: list[str]) -> dict:
        """Run the named reproduction tests (#484) before the write; any failure to run them is
        an ``unavailable`` record, never a refusal."""
        if not ids:
            # Nothing named, but the hunk check (#1043) still runs against the test files the
            # fix changed, when there is a sandbox to run it in; it never blocks either way.
            if self._prover is None:
                return repro_proof.none_named()
            try:
                return self._prover(manifest, [])
            except Exception:
                return repro_proof.none_named()
        if self._prover is None:
            return repro_proof.unavailable("this host run has no sandbox to prove it in")
        try:
            return self._prover(manifest, ids)
        except Exception as exc:
            return repro_proof.unavailable(f"{type(exc).__name__}: {exc}")

    def _hold_issue_fix(self, supervisor, outcome: str) -> None:
        """An issue-fix write whose outcome is unknown holds its run ``uncertain`` (#522), as the
        fixer's push does: ``status`` lists it, ``reconcile`` releases it, nothing replays it."""
        try:
            supervisor.quarantine_push(self.scope.run_id, self.scope.repo, self.scope.number,
                                       self.scope.head, outcome, seat="issue_fixer")
        except Exception as exc:
            raise broker_ipc.ProtocolError("post-write quarantine persistence failed") from exc

    def _record_shadow_issue(self, raw: bytes, request: object) -> object:
        """Record one issue a shadow review asks to file (#914), checked as the reviewer's would
        be. The seat gets the same "accepted" a filing gets; the host files it after the real
        review (``shadow_issues.file_pending``), or never, if the reviewer filed the same."""
        from .run_supervisor import Supervisor
        title, body, labels, _current = self._issue_request(raw, request)
        if not self.scope.run_id or not self.scope.ledger_db:
            raise broker_ipc.ProtocolError("host run ledger unavailable")
        try:
            Supervisor(self.scope.ledger_db, create=False).record_shadow_issue(
                self.scope.run_id, self.scope.repo, self.scope.number, self.scope.head,
                title, body, labels, broker.FILED_ISSUES_MAX)
        except ValueError as exc:
            raise broker_ipc.ProtocolError(f"{exc}; nothing was filed")
        return {"accepted": True}

    def _issue_request(self, raw: bytes, request: object) -> tuple:
        """A ``file_issue`` request checked as the reviewer's is (#247): ``(title, body, labels,
        loop)``, or ``ProtocolError``. Shared by the reviewer's filing and the shadow's record
        (#914), so a second model's request is held to exactly the same rules."""
        if self.scope.role != "reviewer" or not isinstance(request, dict):
            raise broker_ipc.ProtocolError("operation out of scope")
        if (set(request) != {"operation", "title", "body", "labels"}
                or len(raw) > broker_ipc.MAX_REQUEST):
            raise broker_ipc.ProtocolError("file_issue takes a title, a body and labels")
        title, body, labels = request["title"], request["body"], request["labels"]
        if (not isinstance(title, str) or not title.strip()
                or len(title) > broker.ISSUE_PR_TITLE_MAX or any(ord(ch) < 32 for ch in title)):
            raise broker_ipc.ProtocolError(
                f"the issue title must be one line of 1-{broker.ISSUE_PR_TITLE_MAX} characters; "
                "nothing was filed")
        if (not isinstance(body, str) or not body.strip()
                or len(body.encode()) > broker.FILED_ISSUE_BODY_MAX):
            raise broker_ipc.ProtocolError(
                f"the issue body must be non-empty text of at most {broker.FILED_ISSUE_BODY_MAX} "
                "bytes; nothing was filed")
        current = config.by_repo(self.scope.repo)
        if (current is None or current.get("id") != self._loop.get("id")
                or current.get("state_dir") != self._loop.get("state_dir")):
            raise broker_ipc.ProtocolError("run configuration changed")
        triage = current.get("triage") or {}
        allowed = {name.casefold(): name for name in triage.get("labels") or []}
        # The fix label is a maintainer's hand-off to the fixer: a reviewer applying it would
        # close the chain with no person in it. Refused here, on its own, rather than resting on
        # normalize_triage keeping it out of triage.labels (#298).
        fix = str(triage.get("fix_label") or "").casefold()
        if fix:
            allowed.pop(fix, None)
            if isinstance(labels, list) and any(isinstance(x, str) and x.casefold() == fix
                                                for x in labels):
                raise broker_ipc.ProtocolError("the fix label is a maintainer's hand-off to the "
                                               "fixer; a reviewer never applies it — nothing "
                                               "was filed")
        if (not isinstance(labels, list) or not all(isinstance(x, str) for x in labels)
                or len(labels) > int(triage.get("max_labels") or 3)
                or any(x.casefold() not in allowed for x in labels)):
            raise broker_ipc.ProtocolError("labels must come from the loop's triage list "
                                           f"({', '.join(allowed.values()) or 'none configured'}); "
                                           "nothing was filed")
        labels = list(dict.fromkeys(allowed[x.casefold()] for x in labels))
        if why := issue_labels.missing_type(allowed.values(), labels):    # #967: a type label
            raise broker_ipc.ProtocolError(why)
        return title.strip(), body, labels, current

    def _file_issue(self, raw: bytes, request: object) -> object:
        """File one issue from an issue-tier finding (#247), as the reviewer's own login.

        Narrow on purpose: a title, a body and labels from the loop's triage list, at most
        ``broker.FILED_ISSUES_MAX`` per run, one title per PR (a retry or a later round never
        files it twice). Recorded in the ledger before the POST; an unknown outcome is uncertain
        and never replayed. The issue carries its lineage (the PR, and how deep a chain of
        automatic fixes it came from) so a later automatic hand-off can be bounded.
        """
        from .run_supervisor import Supervisor
        title, body, labels, current = self._issue_request(raw, request)
        if not self.scope.run_id or not self.scope.ledger_db:
            raise broker_ipc.ProtocolError("host run ledger unavailable")
        supervisor = Supervisor(self.scope.ledger_db, create=False)
        # Lineage: a finding on a PR that fixed a filed issue is one generation deeper.
        match = re.fullmatch(config.ISSUE_FIX_BRANCH_RE, self.scope.branch or "")
        parent = supervisor.filed_depth(self.scope.repo, int(match.group(1))) if match else None
        depth = (parent or 0) + 1
        login = broker.authorize(current, repo=self.scope.repo, number=self.scope.number,
                                 head=self.scope.head, role="reviewer", branch=self.scope.branch,
                                 operation="file_issue")
        try:
            seq = supervisor.record_filed_issue(self.scope.run_id, self.scope.repo,
                                                self.scope.number, self.scope.head,
                                                title.strip(), depth, broker.FILED_ISSUES_MAX)
        except ValueError as exc:
            raise broker_ipc.ProtocolError(f"{exc}; nothing was filed")
        try:
            issue = broker.file_issue(current, repo=self.scope.repo, number=self.scope.number,
                                      head=self.scope.head, login=login, title=title.strip(),
                                      body=body, labels=labels, depth=depth)
        except broker.BrokerDenied as exc:
            supervisor.filed_issue_status(self.scope.run_id, seq, "uncertain",
                                          error=str(exc)[:200])
            raise broker_ipc.ProtocolError(
                "the issue's outcome is unknown: do not retry it; say so")
        except Exception as exc:
            supervisor.filed_issue_status(self.scope.run_id, seq, "uncertain",
                                          error=f"POST outcome unknown: {broker_ipc._why(exc)}")
            raise broker_ipc.ProtocolError(
                "the issue's outcome is unknown: do not retry it; say so")
        supervisor.filed_issue_status(self.scope.run_id, seq, "posted", issue_number=issue)
        return {"accepted": True, "issue": issue}

    def _issue_comment(self, loop: dict, supervisor, body: str, kind: str | None = None) -> object:
        from . import state as state_mod
        run_id = self.scope.run_id
        try:
            login = broker.authorize_issue_fix(loop, repo=self.scope.repo, number=self.scope.number)
        except broker.BrokerDenied as exc:
            supervisor.issue_fix_status(run_id, "denied", error=str(exc)[:200])
            raise
        try:
            comment = broker.post_issue_comment(loop, repo=self.scope.repo,
                                                number=self.scope.number, login=login, body=body)
        except Exception as exc:
            supervisor.issue_fix_status(run_id, "uncertain",
                                        error=f"POST outcome unknown: {broker_ipc._why(exc)}")
            raise broker_ipc.ProtocolError("the comment's outcome is unknown: do not retry; say so")
        supervisor.issue_fix_status(run_id, "posted", comment_id=comment)
        if kind:       # #483: the question or plan is open; the seat is released with this run
            state_mod.state_for(loop).fix_ask_set(self.scope.number, kind, comment, body)
        return {"accepted": True}


def _issue_fix_outcome(scope: broker_ipc.RunScope) -> str:
    """The recorded issue-fix write, as a notice says it, or "" when nothing was recorded."""
    if not scope.run_id or not scope.ledger_db:
        return ""
    from .run_supervisor import Supervisor
    try:
        with Supervisor(scope.ledger_db, create=False)._connect() as con:
            row = con.execute("SELECT kind,state,pr_number,comment_id,error FROM issue_fixes "
                              "WHERE run_id=?", (scope.run_id,)).fetchone()
    except Exception:
        return "write recorded; result unreadable — inspect the ledger"
    if row is None:
        return ""
    state, error = row["state"], row["error"]
    if row["kind"] == "pr" and row["pr_number"]:
        pr = f"PR #{row['pr_number']} opened"
        if state == "requested":
            return f"{pr}, review requested"
        return pr + (f" ({error})" if error else "")
    if row["kind"] == "comment" and state == "posted":
        return "could not fix it: the fixer commented on the issue instead"
    return f"{state}" + (f": {error}" if error else "") + (
        " — never replayed; inspect the branch and issue" if state == "uncertain" else "")


# Last, so either module may be imported first: broker_ipc's class statement needs IssueWrites
# defined, and these methods read broker_ipc only when they run.
from . import broker_ipc  # noqa: E402
