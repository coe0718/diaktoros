"""One-run, credential-owning Unix socket broker for review-loop REST writes.

The trusted launcher constructs RunScope from gate-verified data, starts this server
outside the agent's mount namespace, then bind-mounts ONLY its socket (or its private
socket directory) at /run/review-loop/broker.sock inside that namespace. Do not mount
this module's host config, state, token files or socket parent into the agent.

The socket is a bearer capability, and that is all it is: reaching the path is the credential, and
it buys exactly the one verified write its RunScope names. The protection is exactly this, and no
more (issue #89):

* another UID cannot reach it — the run directory is 0700 and the socket 0600, inside a private
  scratch directory;
* the same UID can. There is no accept-time credential check and no SO_PEERCRED check: the seat
  runs under the same host UID as this broker, so a peer check could not tell the seat from any
  other same-UID process. The per-run path is enumerable (a scratch parent such as /var/tmp is
  world-traversable, and the run directory name is discoverable), and the socket is then
  connectable and usable. Treat every same-UID process on the host as able to spend this run's
  write, and put nothing in a RunScope that one such process may not use.

That is not an extra privilege: the same UID already holds the host PATs this broker writes with,
so the socket hands out nothing the caller could not do directly. Never share this socket between
runs; close it when the run ends.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import threading
from dataclasses import dataclass

from . import broker, config, safe_push

MAX_REQUEST = 16 * 1024
MAX_BODY = 12 * 1024
# A push line carries the whole manifest: a diff of up to safe_push.MAX_PATCH, base64-encoded
# (#64), or whole files of up to MAX_CONTENT; both fit with room for the JSON around them.
MAX_PUSH_REQUEST = 768 * 1024


@dataclass(frozen=True)
class RunScope:
    repo: str
    number: int
    head: str
    role: str
    branch: str
    run_id: str | None = None
    ledger_db: str | None = None
    generation: str | None = None
    # '' when the host showed the seat the whole change, else the host's reason it could not
    # (#93, #110). Host-built, like every field here: requests carry exactly operation/verdict/
    # body (or a manifest), so nothing inside the namespace can set, clear or observe it.
    partial_view: str = ""
    # A conflict-resolution turn (#303): {base_ref, base_sha, tree}, the merge the host staged as
    # the seat's /work. Host-built like every field here; the push becomes a merge commit of it.
    merge: dict | None = None
    # A CI-fix turn (#306): the host handed the fixer a failed check, not a verdict. Its push needs
    # no changes-requested review at the head, and its review request none either. Host-built.
    ci_fix: bool = False


# What a reviewer that could not see the whole change reads when it tries to approve (#93, #110).
# Only APPROVE and REQUEST_CHANGES are verdicts (broker.REVIEW_VERDICTS), so it names the one
# left; the capability is unspent, so that verdict goes through in the same turn.
PARTIAL_VIEW_REFUSAL = (
    "the host could not show you the whole change ({reason}); an approval is refused — submit "
    "REQUEST_CHANGES and explain in the body what was unavailable (COMMENT is not a verdict); "
    "nothing was written, resubmit")
# The fixer's side of the same rule: a fix built without seeing the whole change is not pushed.
# Its one write is its answers comment instead, stating what it could not see.
PARTIAL_VIEW_PUSH_REFUSAL = (
    "the host could not show you the whole change ({reason}); a push is refused — publish your "
    "answers instead with `python -m diaktoros.broker_client request_review --answers-file "
    "<file>` (no push), saying what was unavailable and what you could check in /work; nothing "
    "was written, the request is unspent")

# What a fixer whose every write the host refuses is told (#81): the run is held before any
# turn starts, but if a request somehow reaches the broker it names the denial instead of
# answering with an unrelated "must publish a confirmed push first". ``{reason}`` comes from
# ``policy_hold_reason``, so the seat, the operator's notice and the run ledger all name the
# same reason.
FIXER_WRITE_DENIED = (
    "every write from this turn is refused ({reason}); nothing is written and this turn is "
    "held for the operator — say plainly in your summary that the fix was not published, "
    "and never describe a fix as pushed")


def policy_hold_reason(loop: dict, *, run_id: str | None = None, repo: str = "",
                       number: int = 0, head: str = "", ledger_db: str | None = None) -> str:
    """The one reason the host refuses every write a fixer turn would make, or an empty string.

    The fixer push-policy check, in one place (#81): the launch-time snapshot is only the
    first reading, the host-owned repository configuration is reloaded as at the write
    boundary, and a run admitted under an earlier policy is named too. Every surface that
    holds or denies a fixer turn — ``gate.block_pr_agent``, ``run_supervisor`` and the broker —
    words the denial from these exact constants, so the operator, the run ledger and the seat
    all tell the same story. An unledgered turn (the gate's push-off hold) is named for the
    policy alone: it never becomes a run.

    A run whose admission snapshot is explicit (#81) — ``push_admitted`` recorded at enqueue —
    is held only when the *current* host policy genuinely refuses it (opted out after
    admission, ``FIXER_PUSH_REVOKED``). A launch-time opt-in the policy read cannot
    contradict is not a denial, so an admitted run reaches the normal broker push path and
    is judged on the push itself (partial view, capability, head), never on this hold. A run
    with no readable admission snapshot is named ``FIXER_NOT_ADMITTED`` (#81): a redelivered
    verdict never upgrades an unadmitted one.
    """
    from . import run_supervisor
    if not config.unattended_fixer_push_enabled(loop):
        # With no run yet (the gate's hold) the policy alone is the denial; a run that was
        # admitted under an earlier policy is named as such (#81): same facts, one wording.
        return run_supervisor.FIXER_PUSH_OFF if not ledger_db else run_supervisor.FIXER_NOT_ADMITTED
    current = config.by_repo(repo or loop.get("repo", ""))
    if current is not None and not config.unattended_fixer_push_enabled(current):
        # The policy moved under a run that was admitted under it: the writes it would make
        # are refused now (#81). A run this loop no longer owns is left to the broker's own
        # write-boundary reload — refusing it here would shadow that path's exact reason.
        return run_supervisor.FIXER_PUSH_REVOKED
    if not (run_id and ledger_db):
        # With no run to read an admission snapshot from (#81) — a hand-built scope, or the
        # gate's own policy read before the run exists — the policy alone is the denial: an
        # unprovable write is refused, and a redelivered verdict never upgrades an
        # unadmitted one. ``FIXER_NOT_ADMITTED`` is named whenever there is a run to read
        # and its snapshot cannot be verified.
        return run_supervisor.FIXER_NOT_ADMITTED if ledger_db else ""
    from .run_supervisor import Supervisor
    try:
        if Supervisor(ledger_db, create=False).push_admitted(run_id, repo, number, head):
            return ""
        # The run exists but push_admitted() returned False. Distinguish:
        # - If launch_intent is set, the run was explicitly created with pushes off
        #   (FIXER_NOT_ADMITTED, #22: never upgraded by a later opt-in).
        # - If launch_intent is NULL, it's a legacy row; trust the loop's current policy.
        with Supervisor(ledger_db, create=False)._connect() as con:
            row = con.execute('SELECT launch_intent FROM runs WHERE id=?', (run_id,)).fetchone()
        if row is not None and row['launch_intent'] is not None:
            return run_supervisor.FIXER_NOT_ADMITTED
        # Legacy row or hand-built scope: trust the loop's current opt-in.
        return "" if config.unattended_fixer_push_enabled(loop) else run_supervisor.FIXER_NOT_ADMITTED
    except Exception:
        pass  # an unreadable ledger fails closed: the push itself would refuse too
    return run_supervisor.FIXER_NOT_ADMITTED


class ProtocolError(Exception):
    """Malformed or out-of-scope request, without leaking host details."""


# Fixed, non-sensitive pre-write refusal reasons that are safe to show the agent so it can
# fix and retry. Anything else (tokens, identities, GitHub results) stays "write denied".
PUBLIC_DENIALS = frozenset({
    "file too large", "manifest too large", "patch too large", "unsafe file path",
    "repository control file", "invalid push manifest", "invalid push message or file count",
    "invalid base64 content", "patch content mismatch", "file content mismatch",
    "invalid file entry", "duplicate file path", "file and directory conflict",
    "unexpected diff record", "patch changes a nonregular file", "patch changes a file mode",
    "push has no changes", "patch changes too many files",
    "manifest base differs from scoped PR head",
    "manifest base differs from the scoped base commit",
    "base commit is not on the base branch", "fetched PR branch moved", "PR branch moved",
    "stale PR head", "unsafe branch ref", "manifest traverses tracked file or symlink",
    "manifest replaces tracked directory", "manifest replaces nonregular file",
    "patch does not apply to the scoped head", "invalid review verdict or empty body",
})


# A push whose attempt began but did not finish confirmed (#351): never "write denied", which
# reads as "nothing was published" — the branch may well hold the commit. Host-worded, by outcome.
PUSH_UNCONFIRMED = {
    "published_pr_unverified": (
        "push outcome uncertain: the branch moved to your commit, but the host could not confirm "
        "the PR at it, so no review request or answers will be sent from this turn. Do not retry. "
        "In your summary, say the push may be published and its outcome is being checked by the "
        "host; do not say it was not published."),
}
PUSH_UNKNOWN = ("push outcome unknown: the host could not confirm whether the branch moved, so "
                "nothing more will be written from this turn. Do not retry. In your summary, say the "
                "push's outcome is unknown and being checked by the host.")


def _denial_message(exc: Exception) -> str:
    if isinstance(exc, ProtocolError):
        return str(exc)
    from . import safe_push
    if isinstance(exc, safe_push.PushFailure):
        return PUSH_UNCONFIRMED.get(exc.outcome, PUSH_UNKNOWN)
    if isinstance(exc, broker.BrokerDenied) and str(exc) in PUBLIC_DENIALS:
        return "write denied: " + str(exc)
    return "write denied"


def _why(exc: Exception) -> str:
    """Why a GitHub write failed, for the ledger and the notice: ``HTTP <status>: <message>``
    when the broker saw one, else the exception's type name."""
    if isinstance(exc, broker.BrokerDenied):
        text = str(exc)
        at = text.find("HTTP ")
        return (text[at:] if at >= 0 else text)[:200]
    return type(exc).__name__


def _read_line(conn: socket.socket, limit: int) -> bytes:
    data = bytearray()
    while len(data) <= limit:
        part = conn.recv(min(4096, limit + 1 - len(data)))
        if not part:
            raise ProtocolError("incomplete request")
        data.extend(part)
        if b"\n" in part:
            line, extra = bytes(data).split(b"\n", 1)
            if extra or len(line) > limit:
                raise ProtocolError("invalid frame")
            return line
    raise ProtocolError("request too large")


class RunBroker:
    """Single-threaded server: reviewer writes once; fixer may push then request review."""

    def __init__(self, loop: dict, scope: RunScope, directory: str | Path,
                 *, require_push: bool = False, require_receipt: bool = False,
                 no_write: bool = False):
        # ``no_write`` is a host-only constructor argument (the selftest's live turn). It is
        # not a socket field: requests carry exactly operation/verdict/body (or a manifest),
        # so nothing inside the namespace can turn it on, off, or observe it.
        if type(no_write) is not bool or (no_write and scope.role != "reviewer"):
            raise ValueError("no-write mode is a host flag for reviewer runs only")
        self._no_write = no_write
        # What a no-write reviewer WOULD have submitted: verdict, body and the live authorization
        # outcome. Host memory only; never sent back to the sandbox.
        self.recorded: list[dict] = []
        self._loop = loop
        self.require_push = require_push
        self.require_receipt = require_receipt
        self.scope = scope
        self.directory = Path(directory)
        self.socket_path: Path | None = None
        self._listener: socket.socket | None = None
        self._used = False
        self.completed = False
        # How many requests reached the broker, refused ones included. Host memory only: a turn
        # that exits having sent none never tried to publish (#144), so it is worth one retry.
        self.requests = 0
        self._pushed_head: str | None = None
        # How the fixer's answers comment ended ('posted', 'uncertain', 'denied', 'unrecorded'),
        # a host-chosen word the sandbox may see; None when no answers were sent.
        self.answers_outcome: str | None = None
        self._answers_comment_id: int | None = None
        self._answers_error: str | None = None
        self._stop = threading.Event()

    def __enter__(self) -> "RunBroker":
        root = self.directory
        info = root.lstat()  # never follow a symlink in a caller-provided root
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ProtocolError("broker root must be a private owned directory")
        for _ in range(8):
            candidate = root / ("run-" + secrets.token_hex(16))
            try:
                candidate.mkdir(mode=0o700)
                break
            except FileExistsError:
                continue
        else:
            raise ProtocolError("cannot allocate run socket")
        self.socket_path = candidate / "broker.sock"
        try:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._listener = listener
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            listener.listen(4)
            listener.settimeout(0.2)
        except BaseException:
            self.close()
            raise
        return self

    def close(self) -> None:
        self._stop.set()
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        if self.socket_path is not None:
            self.socket_path.unlink(missing_ok=True)
            self.socket_path.parent.rmdir()
            self.socket_path = None

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def no_write(self) -> bool:
        """Read-only after construction: a started broker cannot be switched into or out of it."""
        return self._no_write

    def serve(self) -> None:
        if self._listener is None:
            raise RuntimeError("broker not started")
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                raise
            with conn:
                conn.settimeout(5)
                self.answers_outcome = None  # reported only on the request that sent them
                try:
                    outcome = self._dispatch(_read_line(
                        conn, MAX_PUSH_REQUEST if self.scope.role in ("fixer", "issue_fixer")
                        else MAX_REQUEST))
                    # Never relay arbitrary GitHub response fields into the namespace. A filed
                    # issue's number is the one exception: a host-checked int the reviewer cites.
                    response = {"ok": True, "result": {"accepted": True}}
                    if isinstance(outcome, dict) and type(outcome.get("issue")) is int:
                        response["result"]["issue"] = outcome["issue"]
                    if self.answers_outcome:
                        response["result"]["answers"] = self.answers_outcome
                except (ProtocolError, broker.BrokerDenied, ValueError, UnicodeError, TimeoutError) as exc:
                    response = {"ok": False, "error": _denial_message(exc)}
                except Exception:
                    # A GitHub, filesystem, or audit failure cannot expose paths or credentials.
                    response = {"ok": False, "error": "write failed"}
                if not response["ok"] and self.answers_outcome:
                    response["error"] += f" (answers comment: {self.answers_outcome})"
                try:
                    conn.sendall(json.dumps(response, separators=(",", ":")).encode() + b"\n")
                except OSError:
                    pass

    def _dispatch(self, raw: bytes) -> object:
        self.requests += 1
        request = json.loads(raw)
        if self._no_write and not (isinstance(request, dict)
                                   and request.get("operation") == "review"):
            # A no-write broker serves one reviewer verdict and nothing else, before any branch
            # that could reach a push, a ruling, or a GitHub write.
            raise ProtocolError("operation out of scope")
        # The adjudicator has exactly one operation, and only the adjudicator has it. Decide
        # that before any other branch so neither side can reach the other's write paths.
        if self.scope.role == "adjudicator" or (isinstance(request, dict)
                                                and request.get("operation") == "ruling"):
            return self._ruling(raw, request)
        # Triage (#213) likewise: one operation, only for the triage role, decided first.
        if self.scope.role == "triage" or (isinstance(request, dict)
                                           and request.get("operation") == "triage"):
            return self._triage(raw, request)
        # A reviewer's issue-tier findings as issues (#247): only for that role, never the
        # review's own capability — the one review write is still spent only by the review.
        if isinstance(request, dict) and request.get("operation") == "file_issue":
            return self._file_issue(raw, request)
        # An issue fix (#214): open_pr or issue_comment, only for that role, decided first.
        if self.scope.role == "issue_fixer" or (isinstance(request, dict) and request.get(
                "operation") in ("open_pr", "issue_comment")):
            return self._issue_fix(raw, request)
        if isinstance(request, dict) and request.get("operation") == "push":
            if set(request) != {"operation", "manifest"} or self.scope.role != "fixer":
                raise ProtocolError("operation out of scope")
            hold = self._policy_hold()
            if hold:
                # (#81) Every write this turn would make is refused: say so before anything
                # else, instead of an unrelated "must publish a confirmed push first" or
                # "run capability already used" (or a late post-capability policy read).
                raise ProtocolError(FIXER_WRITE_DENIED.format(reason=hold))
            if self._used:
                raise ProtocolError("run capability already used")
            reason = self._partial_view()
            if reason:
                # Before the capability, the policy reads and any Git call: a fixer that was not
                # shown the whole change cannot publish a change to it (#93, #110).
                raise ProtocolError(PARTIAL_VIEW_PUSH_REFUSAL.format(reason=reason[:300]))
            # The socket request and launch-time loop snapshot are not policy sources.
            # Reload the host-owned repository configuration at the write boundary.
            current_loop = config.by_repo(self.scope.repo)
            if current_loop is None or not config.unattended_fixer_push_enabled(current_loop):
                raise ProtocolError("unattended fixer push is not enabled by the host operator")
            if (self._loop.get("repo") != current_loop.get("repo")
                    or self._loop.get("id") != current_loop.get("id")
                    or self._loop.get("state_dir") != current_loop.get("state_dir")):
                raise ProtocolError("run configuration changed")
            # Validation precedes consuming the capability, but no write can be replayed.
            safe_push._manifest(request["manifest"])
            self._used = True
            try:
                # Lock covers the final host policy read and the complete ref operation.
                # Disable cannot return while an authorized push is still in progress.
                with config.push_policy_lock():
                    current_loop = config.by_repo(self.scope.repo)
                    if (current_loop is None or
                            not config.unattended_fixer_push_enabled(current_loop) or
                            current_loop.get('id') != self._loop.get('id') or
                            current_loop.get('state_dir') != self._loop.get('state_dir')):
                        raise ProtocolError('unattended fixer push policy changed before write')
                    # A newly enabled config must not authorize a worker that
                    # was admitted while the policy was off (or a legacy row).
                    if not self.scope.run_id or not self.scope.ledger_db:
                        raise ProtocolError('host run admission unavailable')
                    from .run_supervisor import Supervisor
                    supervisor = Supervisor(self.scope.ledger_db, create=False)
                    if not supervisor.push_admitted(
                            self.scope.run_id, self.scope.repo, self.scope.number,
                            self.scope.head):
                        raise ProtocolError('fixer push not authorized at run admission')
                    # Durable write-ahead intent precedes the external Git operation.
                    # If this commit fails, safe_push (and Git) are never called.
                    supervisor.begin_push(self.scope.run_id, self.scope.repo,
                                          self.scope.number, self.scope.head)
                    try:
                        result = safe_push.push(current_loop, repo=self.scope.repo,
                                                number=self.scope.number, head=self.scope.head,
                                                role=self.scope.role, branch=self.scope.branch,
                                                manifest=request["manifest"],
                                                **({"merge": dict(self.scope.merge)}
                                                   if self.scope.merge else {}),
                                                ci_fix=self.scope.ci_fix)
                        # safe_push returns only after exact ref + PR readback and
                        # durable audit. Failure to commit completion leaves intent.
                        supervisor.confirm_push(self.scope.run_id, self.scope.repo,
                                                self.scope.number, self.scope.head)
                    except Exception as exc:
                        # An explicit attempt boundary, not exception text, determines
                        # whether a failed push must occupy the host-side hold.
                        if isinstance(exc, safe_push.PushFailure) or not isinstance(
                                exc, (broker.BrokerDenied, ProtocolError)):
                            if not self.scope.run_id or not self.scope.ledger_db:
                                raise ProtocolError('post-write hold has no host ledger') from exc
                            from .run_supervisor import Supervisor
                            outcome = (exc.outcome if isinstance(exc, safe_push.PushFailure)
                                       and exc.outcome == 'published_pr_unverified' else 'unknown')
                            try:
                                Supervisor(self.scope.ledger_db, create=False).quarantine_push(
                                    self.scope.run_id, self.scope.repo, self.scope.number,
                                    self.scope.head, outcome)
                            except Exception as persistence_error:
                                raise ProtocolError('post-write quarantine persistence failed') from persistence_error
                        raise
            except (broker.BrokerDenied, ProtocolError):
                raise
            except Exception:
                # Failure to obtain the policy lock or reload configuration is
                # pre-write; the capability was consumed but Git was not called.
                raise
            self._pushed_head = result["new_head"]
            return result
        if not isinstance(request, dict) or set(request) != {"operation", "verdict", "body"}:
            raise ProtocolError("unsupported request fields")
        if len(raw) > MAX_REQUEST:
            raise ProtocolError("request too large")
        operation, verdict, body = (request[key] for key in ("operation", "verdict", "body"))
        expected = {"reviewer": "review", "fixer": "request_review"}.get(self.scope.role)
        if operation != expected or expected is None:
            raise ProtocolError("operation out of scope")
        # (#81) A fixer whose every write is refused has no request to make either: named
        # here, before anything is read or consumed, not after a later capability error.
        hold = self._policy_hold() if self.scope.role == "fixer" else ""
        if hold:
            raise ProtocolError(FIXER_WRITE_DENIED.format(reason=hold))
        # A partial-view fixer (#93, #110) may not push; its answers, alone, are its one write.
        # A dispute (#401): every finding answered as not a defect, nothing pushed. The answers
        # are published and the dispute recorded; no review is requested.
        dispute = (operation == "request_review" and verdict == "DISPUTE"
                   and not self._pushed_head)
        if dispute:
            verdict = ""
        answers_only = (operation == "request_review" and not self._pushed_head
                        and (bool(self._partial_view()) or dispute))
        if (operation == "request_review" and self.require_push and not self._pushed_head
                and not answers_only):
            raise ProtocolError("fixer must publish a confirmed push first")
        if not isinstance(verdict, str) or not isinstance(body, str) or len(body.encode()) > MAX_BODY:
            raise ProtocolError("invalid review fields")
        if self._used and not (operation == "request_review" and self._pushed_head):
            raise ProtocolError("run capability already used")
        answers = operation == "request_review" and body != ""
        if answers:
            # The fixer's answers ride on its one review request (#52): checked here, before the
            # capability is consumed, so a refusal leaves the request unspent.
            if not self._pushed_head and not answers_only:
                raise ProtocolError("answers are published with the review request after a "
                                    "confirmed push")
            if verdict or not broker.answers_valid(body):
                raise ProtocolError(f"answers must be non-empty text of at most "
                                    f"{broker.ANSWERS_MAX} bytes without the answers marker; "
                                    "nothing was written, resubmit")
            if not self.scope.run_id or not self.scope.ledger_db:
                raise ProtocolError("host run ledger unavailable")
        if answers_only and not answers:
            raise ProtocolError("the host could not show you the whole change, so there is no "
                                "push to review: publish your answers with --answers-file")
        if operation == "review" and (verdict not in broker.REVIEW_VERDICTS or not body.strip()):
            # Refused before the capability is consumed, so the reviewer can resubmit a real
            # verdict in the same turn. A COMMENT would neither wake the fixer nor cue a merge.
            raise ProtocolError("review verdict must be APPROVE or REQUEST_CHANGES with a non-empty "
                                "body (COMMENT is not a verdict); nothing was written, resubmit")
        if operation == "review" and not broker.has_not_verified(body):
            raise ProtocolError(broker.NOT_VERIFIED_REFUSAL)
        if operation == "review":
            # Numbered findings (#475): every open one accounted for, new ones on changed code.
            # Before the capability is consumed, so the reviewer can resubmit in the same turn.
            from . import findings, state as state_mod
            entry = state_mod.state_for(self._loop).findings_get(self.scope.number)
            reason = findings.check(self._loop, entry, self.scope.number, self.scope.head, body,
                                    verdict)
            if reason:
                raise ProtocolError(reason)
            # Requirements of the issue(s) the PR closes (#511); a PR closing none is unaffected.
            from . import issue_facts
            reason = issue_facts.check(self._loop, self.scope.number, verdict, body)
            if reason:
                raise ProtocolError(reason)
        if operation == "review" and verdict == "APPROVE":
            reason = self._partial_view()
            if reason:
                # Before the capability is consumed and before any GitHub read or write: the seat
                # was not shown the whole change, so it cannot approve it (#93, #110).
                raise ProtocolError(PARTIAL_VIEW_REFUSAL.format(reason=reason[:300]))
            # A live read of the head's CI, also before the capability is consumed: an approval
            # of a head whose checks failed is a false green, so the seat must request changes.
            from . import ci
            # Only the operator's required checks gate it, when the loop names them (#368).
            refusal = ci.approval_refusal(ci.gating(ci.read(self._loop, self.scope.head),
                                                    config.required_checks(self._loop)))
            if refusal:
                raise ProtocolError(refusal)
            # Paths only a human may approve (#478): also before the capability is consumed, so
            # the seat's REQUEST_CHANGES in the same turn still goes through.
            refusal = self._human_paths_refusal()
            if refusal:
                raise ProtocolError(refusal)
        # Consume BEFORE an external write: a lost response cannot lead to a replay.
        self._used = True
        after_push = operation == "request_review" and bool(self._pushed_head)
        head = self._pushed_head if after_push else self.scope.head
        self._pushed_head = None
        if self._no_write:
            return self._record_only(verdict, body)
        if operation == 'review' and self.scope.run_id is not None:
            from .review_receipt import ReceiptLedger, submit
            ledger = ReceiptLedger(self.scope.ledger_db, self.scope.run_id,
                                   self.scope.generation)
            from .review_receipt import ReviewedPrIneligible
            try:
                result = submit(self._loop, self.scope, ledger, verdict, body)
            except ReviewedPrIneligible as exc:
                # The review exists on GitHub: name it on the run before answering the sandbox.
                try:
                    from .run_supervisor import Supervisor
                    Supervisor(self.scope.ledger_db, create=False).record_review_ineligible(
                        self.scope.run_id, exc.reason)
                except Exception as persistence_error:
                    raise ProtocolError('review outcome persistence failed') from persistence_error
                raise
        else:
            if operation == 'review' and self.require_receipt:
                raise ProtocolError('host review claim required')
            if answers_only:
                # Nothing was pushed, so nothing new to review: the answers comment at the head
                # the fixer was given is the whole write. It must actually land.
                if dispute:
                    # Durable intent first (#401): the operator notice exists before the public
                    # comment POST, so a crash after the POST cannot lose the dispute.
                    from .run_supervisor import Supervisor
                    try:
                        Supervisor(self.scope.ledger_db, create=False).record_dispute(
                            self.scope.run_id, self.scope.repo, self.scope.number, head, body)
                    except Exception as persistence_error:
                        raise ProtocolError("dispute persistence failed") from persistence_error
                self.answers_outcome = self._publish_answers(head, body, dispute=dispute)
                if dispute and self.answers_outcome in ("posted", "uncertain", "denied"):
                    try:
                        Supervisor(self.scope.ledger_db, create=False).dispute_comment(
                            self.scope.run_id, self.answers_outcome,
                            comment_id=self._answers_comment_id, error=self._answers_error)
                    except Exception:
                        pass  # stays 'posting'; a finished run's notice then says uncertain
                if self.answers_outcome not in ("posted", "uncertain"):
                    raise ProtocolError(f"answers comment {self.answers_outcome}")
                self.completed = True
                return {"answers": self.answers_outcome}
            if answers:
                # Before the request: the request is what wakes the reviewer, whose record is
                # read from GitHub, so the answers must already be there. Whatever the comment's
                # outcome, the loop still continues with the request.
                self.answers_outcome = self._publish_answers(head, body)
                body = ''
            # #532: a request for a reviewer who is still requested (no verdict yet, e.g. after a
            # CI-fix push) makes GitHub send no event. Read that before the request, and if so
            # deliver the gate the event GitHub will not.
            pending = None
            if after_push:
                try:
                    from . import gh, review_kick
                    live = gh.api(self._loop, gh.pr_path(self._loop, self.scope.number),
                                  login=self._loop["read_token"])
                    pending = live if review_kick.already_requested(self._loop, live) else None
                except Exception:
                    pending = None
            result = broker.perform(self._loop, repo=self.scope.repo, number=self.scope.number,
                                    head=head, role=self.scope.role, branch=self.scope.branch,
                                    operation=operation, verdict=verdict, body=body,
                                    require_verdict=not after_push)
            if pending is not None:
                self._kick_review(pending, head)
        if operation == 'review':
            try:
                from . import findings
                findings.record(self._loop, self.scope.number, self.scope.head, body)
            except Exception:
                pass  # the review is already written; state is best effort
        self.completed = True
        return result

    def _human_paths_refusal(self) -> str:
        """Why the loop may not APPROVE this head because of ``human_paths``, or '' (#478).

        The diff is read live from GitHub (never from the seat or the PR). If it cannot be read
        completely the approval is refused: a path list that cannot be checked is not passed.
        One operator notice per PR and head (the observer's key makes repeats a no-op).
        """
        if not config.human_paths(self._loop):
            return ""
        from . import gh
        files, error = gh.pr_files_read(self._loop, self.scope.number)
        if files is None:
            return (f"the PR's file list could not be read ({error[:200]}), so the loop cannot "
                    "check it against human_paths; nothing was written, request changes or retry")
        paths = []
        for item in files:
            for key in ("filename", "previous_filename"):
                if isinstance(item.get(key), str):
                    paths.append(item[key])
        hits = config.human_path_hits(self._loop, paths)
        if not hits:
            return ""
        names = ", ".join(hits[:10]) + (f" and {len(hits) - 10} more" if len(hits) > 10 else "")
        from . import observer, state as state_mod
        try:
            observer.notify(self._loop, state_mod.state_for(self._loop), "human_paths",
                            self.scope.number, self.scope.head,
                            outcome=f"touches {names}: a person must review and approve")
        except Exception:
            pass                 # a notice never changes the refusal
        return (f"this change touches paths reserved for a human verdict ({names}); the loop "
                "does not approve them. Nothing was written: request changes, or leave the "
                "approval to a person")

    def _partial_view(self) -> str:
        """Why this run's seat could not see the whole change, or '' — from host records only.

        The scope the host built at launch, then the run ledger the worker wrote before the seat
        started. An unreadable ledger is a partial view with that reason (fail closed); the receipt
        claim re-reads the same row inside its transaction as well (review_receipt.ReceiptLedger).
        """
        if self.scope.partial_view:
            return self.scope.partial_view
        if not self.scope.run_id or not self.scope.ledger_db:
            return ""
        from .review_receipt import partial_view
        try:
            return partial_view(self.scope.ledger_db, self.scope.run_id)
        except Exception as exc:
            # Fail closed, and say so: a record nobody can read is not a whole view.
            return f"the host could not read this run's view record ({type(exc).__name__})"

    def _policy_hold(self) -> str:
        """Why the host policy refuses this fixer's push, or '' — read at request time.

        The write boundary is the same one the push path enforces: the host-owned repository
        configuration is reloaded here (never the launch-time snapshot or anything from
        inside the namespace), and a loop that has not opted in to unattended fixer pushes is
        named, so a denied write is never silent (#81). Returns '' — never a hold — when this
        broker holds no run ledger (``ledger_db`` is None): there is no admission snapshot to
        read, so the push path's own boundary checks stay the only authority (#81).
        """
        if not self.scope.ledger_db:
            return ""
        return policy_hold_reason(self._loop, run_id=self.scope.run_id,
                                  repo=self.scope.repo, number=self.scope.number,
                                  head=self.scope.head, ledger_db=self.scope.ledger_db)

    def _publish_answers(self, head: str, text: str, dispute: bool = False) -> str:
        """Post the fixer's answers as ONE PR comment by the fixer identity; return the outcome.

        Authorized like the fixer's other writes (live open PR at the exact pushed head, fixer
        author, distinct identities), recorded in the run ledger before the POST, and never
        retried: a POST whose outcome is unknown stays 'uncertain'.
        """
        from .run_supervisor import Supervisor
        supervisor = Supervisor(self.scope.ledger_db, create=False)
        record = dict(run_id=self.scope.run_id, repo=self.scope.repo, pr=self.scope.number,
                      base=self.scope.head, head=head, body=text)
        try:
            login = broker.authorize(self._loop, repo=self.scope.repo, number=self.scope.number,
                                     head=head, role="fixer", branch=self.scope.branch,
                                     operation="answers", require_verdict=False)
        except Exception as exc:
            reason = str(exc)[:200] if isinstance(exc, broker.BrokerDenied) else type(exc).__name__
            self._answers_error = reason
            try:
                supervisor.begin_answers(**record, state="denied", error=reason, dispute=dispute)
            except Exception:
                pass
            return "denied"
        try:
            supervisor.begin_answers(**record, dispute=dispute)
        except Exception:
            return "unrecorded"  # no durable intent, so no POST
        try:
            comment_id = broker.post_fixer_answers(
                self._loop, repo=self.scope.repo, number=self.scope.number, head=head,
                branch=self.scope.branch, login=login,
                text=broker.answers_comment_body(text, head=head, base=self.scope.head,
                                                 run_id=self.scope.run_id))
        except Exception as exc:
            try:
                supervisor.answers_status(self.scope.run_id, "uncertain",
                                          error=f"POST outcome unknown: {_why(exc)}")
            except Exception:
                pass
            return "uncertain"
        self._answers_comment_id = comment_id
        try:
            supervisor.answers_status(self.scope.run_id, "posted", comment_id=comment_id)
        except Exception:
            pass
        return "posted"

    def _record_only(self, verdict: str, body: str) -> object:
        """No-write reviewer: the live authorization reads, then record, and never POST.

        ``broker.authorize`` only GETs (``/user`` per identity and the live PR), so this proves
        the real write would have been allowed at this head without making it. No receipt claim,
        ledger row, audit line or GitHub write is produced. The sandbox gets the same answer a
        real write would give, so the agent's behaviour is the one a live run would show.
        """
        # Same verdicts a real reviewer write accepts: a COMMENT would stall the loop.
        if verdict not in ("APPROVE", "REQUEST_CHANGES") or not body.strip():
            raise broker.BrokerDenied("invalid review verdict or empty body")
        entry = {"verdict": verdict, "body": body, "authorized": False, "denial": ""}
        self.recorded.append(entry)
        try:
            entry["login"] = broker.authorize(
                self._loop, repo=self.scope.repo, number=self.scope.number,
                head=self.scope.head, role="reviewer", branch=self.scope.branch,
                operation="review")
        except broker.BrokerDenied as exc:
            entry["denial"] = str(exc)[:200]
            raise
        entry["authorized"] = True
        self.completed = True
        return {"recorded": True}

    def _ruling(self, raw: bytes, request: object) -> object:
        """Record the one ruling, then tell the operator, then (optionally) the PR.

        The response is ok as soon as the ruling is durable in the host run ledger; the notice
        and the comment are host deliveries of an already recorded fact, so their failures are
        recorded there too and never turned into a sandbox-visible error or retry.
        """
        if (self.scope.role != "adjudicator" or not isinstance(request, dict)
                or set(request) != {"operation", "verdict", "body"}
                or request["operation"] != "ruling"):
            raise ProtocolError("operation out of scope")
        if len(raw) > MAX_REQUEST:
            raise ProtocolError("request too large")
        verdict, body = request["verdict"], request["body"]
        from .run_supervisor import RULINGS, Supervisor
        if (verdict not in RULINGS or not isinstance(body, str) or not body.strip()
                or len(body.encode()) > MAX_BODY):
            raise ProtocolError("invalid ruling fields")
        if self._used:
            raise ProtocolError("run capability already used")
        if not self.scope.run_id or not self.scope.ledger_db:
            raise ProtocolError("host run ledger unavailable")
        # Consume BEFORE the ledger write: a lost response cannot lead to a second ruling.
        self._used = True
        supervisor = Supervisor(self.scope.ledger_db, create=False)
        recorded = supervisor.record_ruling(self.scope.run_id, self.scope.repo,
                                            self.scope.number, self.scope.head, verdict, body)
        self.completed = True
        try:
            _deliver_ruling(self._loop, self.scope, supervisor, recorded["turn_key"], verdict, body)
        except Exception:
            # Already durable, and the operator outbox reports it regardless.
            pass
        return {"accepted": True}


    def _triage(self, raw: bytes, request: object) -> object:
        """Record the one triage (labels from the loop's list, an optional comment), then write it.

        The issue text this turn read is untrusted, so this is the boundary: only labels the
        operator listed, at most ``max_labels``, and a comment only where the loop allows one.
        The response is ok once the triage is durable; the GitHub writes follow from the host's
        current configuration and live issue, and their outcome is recorded, never retried.
        """
        from .run_supervisor import TRIAGE_COMMENT_MAX, Supervisor
        if (self.scope.role != "triage" or not isinstance(request, dict)
                or set(request) != {"operation", "labels", "body"}
                or request["operation"] != "triage"):
            raise ProtocolError("operation out of scope")
        if len(raw) > MAX_REQUEST:
            raise ProtocolError("request too large")
        triage = self._loop.get("triage") or {}
        allowed = {name.casefold(): name for name in triage.get("labels") or []}
        most = triage.get("max_labels", 3)
        labels, body = request["labels"], request["body"]
        if (not isinstance(labels, list) or len(labels) > most
                or not all(isinstance(x, str) and x.casefold() in allowed for x in labels)
                or len({x.casefold() for x in labels}) != len(labels)):
            raise ProtocolError(f"labels must be at most {most} distinct names from the loop's "
                                "list; nothing was written, resubmit")
        if not isinstance(body, str) or len(body.strip()) > TRIAGE_COMMENT_MAX:
            raise ProtocolError(f"the comment must be text of at most {TRIAGE_COMMENT_MAX} "
                                "characters; nothing was written, resubmit")
        if body.strip() and not triage.get("comment"):
            raise ProtocolError("this loop applies labels only — drop the comment; nothing was "
                                "written, resubmit")
        if self._used:
            raise ProtocolError("run capability already used")
        if not self.scope.run_id or not self.scope.ledger_db:
            raise ProtocolError("host run ledger unavailable")
        self._used = True
        chosen = [allowed[x.casefold()] for x in labels]
        supervisor = Supervisor(self.scope.ledger_db, create=False)
        supervisor.record_triage(self.scope.run_id, self.scope.repo, self.scope.number, chosen,
                                 body.strip())
        self.completed = True
        try:
            _deliver_triage(self._loop, self.scope, supervisor, chosen, body.strip())
        except Exception as exc:
            supervisor.triage_status(self.scope.run_id, "uncertain",
                                     error=f"delivery failed: {type(exc).__name__}")
        return {"accepted": True}


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
                _issue_notice(loop, self.scope, "fixed", outcome)

    def _issue_fix_write(self, raw: bytes, request: object) -> object:
        """The issue fixer's one write (#214): a new branch, its PR and the review request — or,
        when it could not fix the issue, one comment on the issue.

        Validated before the capability is spent; recorded in the ledger before any write;
        the branch push holds the push policy lock, like the fixer's, and its lease requires the
        branch to be absent. A write whose outcome is unknown is recorded as uncertain and never
        replayed.
        """
        from .run_supervisor import Supervisor
        if self.scope.role != "issue_fixer" or not isinstance(request, dict):
            raise ProtocolError("operation out of scope")
        operation = request.get("operation")
        if operation == "open_pr":
            if set(request) != {"operation", "manifest", "title", "body"}:
                raise ProtocolError("open_pr takes manifest, title and body")
            title, body = request["title"], request["body"]
            if (not isinstance(title, str) or not title.strip()
                    or len(title) > broker.ISSUE_PR_TITLE_MAX
                    or any(ord(ch) < 32 for ch in title)):
                raise ProtocolError(f"the PR title must be one line of 1-"
                                    f"{broker.ISSUE_PR_TITLE_MAX} characters; nothing was "
                                    "written, resubmit")
            safe_push._manifest(request["manifest"])
        elif operation == "issue_comment":
            if set(request) != {"operation", "body"} or len(raw) > MAX_REQUEST:
                raise ProtocolError("issue_comment takes only a body")
            body = request["body"]
        else:
            raise ProtocolError("operation out of scope")
        if not isinstance(body, str) or not body.strip() or len(body.encode()) > broker.ANSWERS_MAX:
            raise ProtocolError(f"the description must be non-empty text of at most "
                                f"{broker.ANSWERS_MAX} bytes; nothing was written, resubmit")
        if self._used:
            raise ProtocolError("run capability already used")
        if not self.scope.run_id or not self.scope.ledger_db:
            raise ProtocolError("host run ledger unavailable")
        self._used = True
        supervisor = Supervisor(self.scope.ledger_db, create=False)
        run_id, repo, number = self.scope.run_id, self.scope.repo, self.scope.number
        with config.push_policy_lock():
            current = config.by_repo(repo)
            if (current is None or current.get("id") != self._loop.get("id")
                    or current.get("state_dir") != self._loop.get("state_dir")
                    or not config.issue_fixes_enabled(current)):
                raise ProtocolError("issue fixes are not enabled by the host operator")
            supervisor.record_issue_fix(run_id, repo, number, self.scope.head,
                                        "pr" if operation == "open_pr" else "comment",
                                        self.scope.branch)
            self.completed = True
            if operation == "issue_comment":
                return self._issue_comment(current, supervisor, body.strip())
            try:
                pushed = safe_push.open_branch(current, repo=repo, number=number,
                                               base=self.scope.head, branch=self.scope.branch,
                                               manifest=request["manifest"])
            except safe_push.PushFailure as exc:
                supervisor.issue_fix_status(run_id, "uncertain", error=f"push {exc.outcome}")
                self._hold_issue_fix(supervisor, "unknown")
                raise ProtocolError("the push's outcome is unknown: do not retry; say so")
            except broker.BrokerDenied as exc:
                supervisor.issue_fix_status(run_id, "denied", error=str(exc)[:200])
                raise
        supervisor.issue_fix_status(run_id, "pushed", new_head=pushed["new_head"])
        try:
            pr = broker.open_issue_pr(current, repo=repo, number=number, branch=self.scope.branch,
                                      title=title.strip(), body=body, login=pushed["login"])
        except Exception as exc:
            supervisor.issue_fix_status(run_id, "uncertain",
                                        error=f"PR create outcome unknown: {_why(exc)}")
            self._hold_issue_fix(supervisor, "pr_create_unknown")
            raise ProtocolError("the branch was pushed but the PR could not be confirmed: do "
                                "not retry; say so")
        supervisor.issue_fix_status(run_id, "opened", pr_number=pr)
        try:
            broker.request_issue_pr_review(current, repo=repo, pr=pr, login=pushed["login"])
        except Exception as exc:
            # The PR is open as the fixer, so the reviewer gate's `opened` still starts the loop.
            supervisor.issue_fix_status(run_id, "opened",
                                        error=f"review request not confirmed: {_why(exc)}")
            return {"accepted": True}
        supervisor.issue_fix_status(run_id, "requested")
        return {"accepted": True}

    def _kick_review(self, pr: dict, head: str) -> None:
        """Start the review GitHub will not announce (#532). Best effort: the request itself
        succeeded, and the watchdog's sweep is the backstop if this delivery does not land."""
        from . import review_kick
        from .util import log
        sender = str((self._loop.get("seats") or {}).get("fixer", {}).get("login")
                     or (self._loop.get("fixers") or [""])[0])
        try:
            sent = review_kick.kick(self._loop, pr, head, sender,
                                    f"kick-review-{self.scope.number}")
        except Exception as exc:
            sent = False
            log(f"#{self.scope.number} @ {head[:7]}: review start not delivered: "
                f"{type(exc).__name__}: {exc}")
        log(f"#{self.scope.number} @ {head[:7]}: reviewer was already requested, so GitHub sends "
            f"no event — {'delivered the review request to the gate' if sent else 'the watchdog will start it'}")

    def _hold_issue_fix(self, supervisor, outcome: str) -> None:
        """An issue-fix write whose outcome is unknown holds its run ``uncertain`` (#522), as the
        fixer's push does: ``status`` lists it, ``reconcile`` releases it, nothing replays it."""
        try:
            supervisor.quarantine_push(self.scope.run_id, self.scope.repo, self.scope.number,
                                       self.scope.head, outcome, seat="issue_fixer")
        except Exception as exc:
            raise ProtocolError("post-write quarantine persistence failed") from exc

    def _file_issue(self, raw: bytes, request: object) -> object:
        """File one issue from an issue-tier finding (#247), as the reviewer's own login.

        Narrow on purpose: a title, a body and labels from the loop's triage list, at most
        ``broker.FILED_ISSUES_MAX`` per run, one title per PR (a retry or a later round never
        files it twice). Recorded in the ledger before the POST; an unknown outcome is uncertain
        and never replayed. The issue carries its lineage (the PR, and how deep a chain of
        automatic fixes it came from) so a later automatic hand-off can be bounded.
        """
        from .run_supervisor import Supervisor
        if self.scope.role != "reviewer" or not isinstance(request, dict):
            raise ProtocolError("operation out of scope")
        if set(request) != {"operation", "title", "body", "labels"} or len(raw) > MAX_REQUEST:
            raise ProtocolError("file_issue takes a title, a body and labels")
        title, body, labels = request["title"], request["body"], request["labels"]
        if (not isinstance(title, str) or not title.strip()
                or len(title) > broker.ISSUE_PR_TITLE_MAX or any(ord(ch) < 32 for ch in title)):
            raise ProtocolError(f"the issue title must be one line of 1-"
                                f"{broker.ISSUE_PR_TITLE_MAX} characters; nothing was filed")
        if (not isinstance(body, str) or not body.strip()
                or len(body.encode()) > broker.FILED_ISSUE_BODY_MAX):
            raise ProtocolError(f"the issue body must be non-empty text of at most "
                                f"{broker.FILED_ISSUE_BODY_MAX} bytes; nothing was filed")
        current = config.by_repo(self.scope.repo)
        if (current is None or current.get("id") != self._loop.get("id")
                or current.get("state_dir") != self._loop.get("state_dir")):
            raise ProtocolError("run configuration changed")
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
                raise ProtocolError("the fix label is a maintainer's hand-off to the fixer; a "
                                    "reviewer never applies it — nothing was filed")
        if (not isinstance(labels, list) or not all(isinstance(x, str) for x in labels)
                or len(labels) > int(triage.get("max_labels") or 3)
                or any(x.casefold() not in allowed for x in labels)):
            raise ProtocolError("labels must come from the loop's triage list "
                                f"({', '.join(allowed.values()) or 'none configured'}); "
                                "nothing was filed")
        labels = list(dict.fromkeys(allowed[x.casefold()] for x in labels))
        if not self.scope.run_id or not self.scope.ledger_db:
            raise ProtocolError("host run ledger unavailable")
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
            raise ProtocolError(f"{exc}; nothing was filed")
        try:
            issue = broker.file_issue(current, repo=self.scope.repo, number=self.scope.number,
                                      head=self.scope.head, login=login, title=title.strip(),
                                      body=body, labels=labels, depth=depth)
        except broker.BrokerDenied as exc:
            supervisor.filed_issue_status(self.scope.run_id, seq, "uncertain",
                                          error=str(exc)[:200])
            raise ProtocolError("the issue's outcome is unknown: do not retry it; say so")
        except Exception as exc:
            supervisor.filed_issue_status(self.scope.run_id, seq, "uncertain",
                                          error=f"POST outcome unknown: {_why(exc)}")
            raise ProtocolError("the issue's outcome is unknown: do not retry it; say so")
        supervisor.filed_issue_status(self.scope.run_id, seq, "posted", issue_number=issue)
        return {"accepted": True, "issue": issue}

    def _issue_comment(self, loop: dict, supervisor, body: str) -> object:
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
                                        error=f"POST outcome unknown: {_why(exc)}")
            raise ProtocolError("the comment's outcome is unknown: do not retry; say so")
        supervisor.issue_fix_status(run_id, "posted", comment_id=comment)
        return {"accepted": True}


def _issue_notice(loop: dict | None, scope: RunScope, event: str, outcome: str) -> None:
    """The observer's issue-side notice (#231): best effort, once per run (keyed by its id)."""
    if loop is None:
        return
    from . import observer, state as state_mod
    try:
        observer.notify(loop, state_mod.state_for(loop), event, scope.number, scope.head,
                        identity=scope.run_id, outcome=outcome, issue=True)
    except Exception:
        pass                     # a notice never changes what was written


def _issue_fix_outcome(scope: RunScope) -> str:
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


def _triage_outcome(supervisor, run_id: str) -> str:
    """The recorded triage result, as a notice says it: the labels, or why there are none."""
    try:
        with supervisor._connect() as con:
            row = con.execute("SELECT state,error,labels,comment_id FROM triage_results "
                              "WHERE run_id=?", (run_id,)).fetchone()
    except Exception:
        return "result unreadable"
    if row is None:
        return "no result recorded"
    state = row["state"]
    if state == "posted":
        labels = row["labels"] or ""
        try:
            labels = ", ".join(json.loads(labels)) if labels.startswith("[") else labels
        except ValueError:
            pass
        return (f"labelled {labels}" if labels else "commented") + (
            " + comment" if row["comment_id"] and labels else "")
    if state == "nothing":
        return "no allowed label fit; nothing written"
    return f"{state}" + (f": {row['error']}" if row["error"] else "")


def _deliver_triage(launch_loop: dict, scope: RunScope, supervisor, labels: list[str],
                    body: str) -> None:
    """Write a recorded triage, then tell the observer how it ended (#231)."""
    try:
        _write_triage(launch_loop, scope, supervisor, labels, body)
    finally:
        try:
            loop = config.by_repo(scope.repo)
        except Exception:
            loop = None
        _issue_notice(loop, scope, "triaged", _triage_outcome(supervisor, scope.run_id))


def _write_triage(launch_loop: dict, scope: RunScope, supervisor, labels: list[str],
                  body: str) -> None:
    """Write a recorded triage: re-authorize against the live issue, then labels, then comment."""
    try:
        loop = config.by_repo(scope.repo)
    except Exception:
        loop = None
    if (loop is None or loop.get("id") != launch_loop.get("id")
            or loop.get("state_dir") != launch_loop.get("state_dir")
            or not config.triage_enabled(loop)):
        supervisor.triage_status(scope.run_id, "denied", error="loop configuration changed")
        return
    if not labels and not body:
        supervisor.triage_status(scope.run_id, "nothing")
        return
    try:
        login = broker.authorize_triage(loop, repo=scope.repo, number=scope.number)
    except broker.TriageSkipped as exc:
        supervisor.triage_status(scope.run_id, "skipped", error=str(exc)[:200])
        return
    except broker.BrokerDenied as exc:
        supervisor.triage_status(scope.run_id, "denied", error=str(exc)[:200])
        return
    except Exception as exc:
        supervisor.triage_status(scope.run_id, "denied",
                                 error=f"authorization failed: {type(exc).__name__}")
        return
    # Durable intent before the POSTs: a crash after it is reported as uncertain, never replayed.
    supervisor.triage_status(scope.run_id, "posting")
    try:
        comment_id = broker.post_triage(loop, repo=scope.repo, number=scope.number, login=login,
                                        labels=labels, body=body)
    except Exception as exc:
        supervisor.triage_status(scope.run_id, "uncertain",
                                 error=f"POST outcome unknown: {_why(exc)}")
        return
    supervisor.triage_status(scope.run_id, "posted", comment_id=comment_id)
    # #232: the hand-off is the host's, after the triage write is recorded; the seat never
    # applies fix_label. The live issue is re-read there, so only labels that really landed count.
    if labels and config.auto_fix_labels(loop):
        from . import fix_hold, util
        outcome = fix_hold.auto_offer(loop, scope.number)
        if outcome:
            util.log(f"issue #{scope.number}: {outcome}")


def _deliver_ruling(launch_loop: dict, scope: RunScope, supervisor, turn_key: str,
                    verdict: str, body: str) -> None:
    """Observer notice (best effort), then the PR comment only for a configured identity."""
    from . import observer, state as state_mod
    # Delivery follows the host's current configuration, not the launch snapshot: an identity
    # the operator removed since launch must not comment. A vanished or re-pointed loop gets
    # neither delivery; the outbox (run ledger) still carries the ruling.
    try:
        loop = config.by_repo(scope.repo)
    except Exception:
        loop = None
    if (loop is None or loop.get("id") != launch_loop.get("id")
            or loop.get("state_dir") != launch_loop.get("state_dir")):
        supervisor.ruling_status(scope.run_id, observer="skipped", comment="denied",
                                 comment_error="loop configuration changed")
        return
    rounds = turn_key.split(":", 1)[1] if turn_key.startswith("breach:") else "?"
    # The feed carries the fact (verdict, counts), never the model's reason text — that goes
    # to the operator outbox and, when configured, the PR. See observer.notify's contract.
    sent = observer.notify(loop, state_mod.state_for(loop), "ruling", scope.number, scope.head,
                           identity=turn_key, outcome=f"{verdict} · {rounds}/{loop['cap']} verdicts",
                           next_turn="you — the adjudicator never merges")
    supervisor.ruling_status(scope.run_id, observer="sent" if sent else "unsent")
    if not config.adjudicator_login(loop):
        supervisor.ruling_status(scope.run_id, comment="none")
        return
    try:
        login = broker.authorize_ruling_comment(loop, repo=scope.repo, number=scope.number,
                                                head=scope.head, branch=scope.branch)
    except broker.BrokerDenied as exc:
        supervisor.ruling_status(scope.run_id, comment="denied", comment_error=str(exc)[:200])
        return
    except Exception as exc:
        supervisor.ruling_status(scope.run_id, comment="denied",
                                 comment_error=f"authorization failed: {type(exc).__name__}")
        return
    # Durable intent before the POST: a crash after it is reported as uncertain, never replayed.
    supervisor.ruling_status(scope.run_id, comment="posting")
    try:
        comment_id = broker.post_ruling_comment(
            loop, repo=scope.repo, number=scope.number, head=scope.head, branch=scope.branch,
            login=login, text=broker.ruling_comment_body(verdict, body, head=scope.head,
                                                          turn_key=turn_key, run_id=scope.run_id,
                                                          cap=loop["cap"]))
    except Exception as exc:
        supervisor.ruling_status(scope.run_id, comment="uncertain",
                                 comment_error=f"POST outcome unknown: {_why(exc)}")
        return
    supervisor.ruling_status(scope.run_id, comment="posted", comment_id=comment_id)


def serve_in_thread(server: RunBroker) -> threading.Thread:
    """Start an entered server; caller owns its lifetime and must join on shutdown."""
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    return thread


# A push or review is several GitHub calls plus git fetch/push (each up to 90s), all
# host-side. Wait for the answer instead of timing out mid-write; the turn deadline is
# the real bound.
WRITE_TIMEOUT = 900


def request(operation: str, *, verdict: str = "", body: str = "",
            manifest: object = None,
            socket_path: str = "/run/review-loop/broker/broker.sock") -> dict:
    """Credentialless in-namespace caller; never accepts a target repo or token."""
    if operation == "push":
        payload = {"operation": operation, "manifest": manifest}
    elif operation == "triage":
        payload = {"operation": operation, "labels": list(manifest or []), "body": body}
    elif operation in ("review", "request_review", "ruling"):
        payload = {"operation": operation, "verdict": verdict, "body": body}
    else:
        raise ProtocolError("unsupported operation")
    raw = json.dumps(payload, separators=(",", ":")).encode()
    if len(raw) > MAX_PUSH_REQUEST:
        raise ProtocolError("request too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(WRITE_TIMEOUT)
        conn.connect(socket_path)
        conn.sendall(raw + b"\n")
        answer = _read_line(conn, MAX_REQUEST)
    result = json.loads(answer)
    if not isinstance(result, dict) or set(result) not in ({"ok", "result"}, {"ok", "error"}):
        raise ProtocolError("invalid broker response")
    return result


def main() -> None:
    """Small CLI for a sandboxed Hermes terminal tool call."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("review", "request_review", "push", "ruling"))
    parser.add_argument("--verdict", default="")
    parser.add_argument("--body-file")
    parser.add_argument("--manifest-file")
    args = parser.parse_args()
    if args.operation == "push":
        if args.body_file or not args.manifest_file:
            parser.error("push requires --manifest-file only")
        path = Path(args.manifest_file)
        if path.stat().st_size > MAX_PUSH_REQUEST:
            parser.error("manifest too large")
        result = request("push", manifest=json.loads(path.read_text()))
    else:
        if args.operation == "ruling" and (args.verdict not in ("ACCEPT", "REJECT", "RESPEC")
                                           or not args.body_file):
            parser.error("ruling requires --verdict ACCEPT|REJECT|RESPEC and --body-file")
        if args.manifest_file or (args.operation == "review" and not args.body_file):
            parser.error("invalid review arguments")
        if args.operation == "review" and args.verdict not in broker.REVIEW_VERDICTS:
            parser.error("review requires --verdict APPROVE|REQUEST_CHANGES (COMMENT is not a verdict)")
        body = Path(args.body_file).read_text() if args.body_file else ""
        if len(body.encode()) > MAX_BODY:
            parser.error("review body too large")
        result = request(args.operation, verdict=args.verdict, body=body)
    print(json.dumps(result))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
