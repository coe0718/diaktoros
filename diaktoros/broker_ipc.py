"""One-run, credential-owning Unix socket broker for review-loop REST writes.

The trusted launcher constructs RunScope from gate-verified data, starts this server
outside the agent's mount namespace, then bind-mounts ONLY its private socket
directory at /run/review-loop/broker (contained.BROKER_MOUNT), so the socket is
/run/review-loop/broker/broker.sock inside that namespace. Do not mount
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
import secrets
import socket
import stat
import threading
from dataclasses import dataclass

from . import broker, config, contained, safe_push
from . import broker_issues   # RunBroker's issue writes: broker_issues.IssueWrites, inherited

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
    # An adjudicator's turn (#1112): the run id of the shadow review its prompt showed ('' when
    # it showed none), recorded with its ruling so a later shadow review is never read as
    # overruled by it. None: not an adjudicator scope. Host-built, like every field here.
    shadow_run: str | None = None
    # A fix of a shadow review the adjudicator upheld (#1004 part 3): that REJECT ruling's time. Its
    # push needs no changes-requested review (the head is approved) but that ruling, still current
    # at the head; its review request needs none. None: not such a turn. Host-built.
    shadow_fix: float | None = None


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
    "answers instead with `python -P -m diaktoros.broker_client request_review --answers-file "
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
# An issue fix's branch push the host confirmed, though the transport errored (#824).
ISSUE_PUSH_PUBLISHED = (
    "push published: the git transport reported an error, but the host read the new branch back "
    "at your commit, so the push is published. No PR was opened from this turn. Do not retry. In "
    "your summary, say the branch was published and the host confirmed it, and that no PR was "
    "opened; do not say its outcome is unknown.")
PUSH_UNCONFIRMED = {
    "published": (
        "push published: the git transport reported an error, but the host read the branch back "
        "at your commit, so the push is published. No review request or answers will be sent "
        "from this turn. Do not retry. In your summary, say the push was published and the host "
        "confirmed the branch; do not say its outcome is unknown or that it was not published."),
    "published_pr_unverified": (
        "push outcome uncertain: the branch moved to your commit, but the host could not confirm "
        "the PR at it, so no review request or answers will be sent from this turn. Do not retry. "
        "In your summary, say the push may be published and its outcome is being checked by the "
        "host; do not say it was not published."),
}
PUSH_UNKNOWN = ("push outcome unknown: the host could not confirm whether the branch moved, so "
                "nothing more will be written from this turn. Do not retry. In your summary, say the "
                "push's outcome is unknown and being checked by the host.")


# A review whose POST was sent but did not finish confirmed: never "write denied" either, for
# the same reason as a push. The reasons are fixed host wording (review_receipt), safe to show.
REVIEW_POSTED_INELIGIBLE = (
    "review posted, but the PR is now {reason}: the review is on GitHub. Do not retry. In your "
    "summary, say the review was posted and the PR then became ineligible; do not say it was "
    "not published.")
REVIEW_UNCONFIRMED = (
    "review outcome uncertain: the review was sent to GitHub but the host could not confirm it "
    "({reason}), so it may be published. Do not retry. In your summary, say the review may be "
    "posted and its outcome is being checked by the host; do not say it was not published.")
REVIEW_POSTED_UNRECORDED = (
    "review posted, but the host could not record its outcome: the review is on GitHub. Do not "
    "retry. In your summary, say the review was posted; do not say it was not published.")


def _denial_message(exc: Exception) -> str:
    if isinstance(exc, ProtocolError):
        return str(exc)
    from . import review_receipt, safe_push
    if isinstance(exc, review_receipt.ReviewedPrIneligible):
        return REVIEW_POSTED_INELIGIBLE.format(reason=exc.reason)
    if isinstance(exc, review_receipt.ReviewPostUnconfirmed):
        return REVIEW_UNCONFIRMED.format(reason=exc.reason)
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


class RunBroker(broker_issues.IssueWrites):
    """Single-threaded server: reviewer writes once; fixer may push then request review."""

    def __init__(self, loop: dict, scope: RunScope, directory: str | Path,
                 *, require_push: bool = False, require_receipt: bool = False,
                 no_write: bool = False, prover=None):
        # ``prover`` is host-only like ``no_write`` (#484): ``prover(manifest, ids)`` runs an issue
        # fix's named reproduction tests on the old head and on the fix and returns the proof
        # (``repro_proof``). None (a broker built without one) records ``unavailable``.
        self._prover = prover
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
                    if isinstance(outcome, dict) and outcome.get("held") in ("human_paths",
                                                                             "conflict"):
                        # Host-written (#478, #1243): the seat must not read a withheld approval
                        # as done.
                        response["result"] = {"accepted": False, "held": outcome["held"],
                                              "reason": str(outcome.get("reason") or "")[:500]}
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
        if (self._no_write and isinstance(request, dict)
                and request.get("operation") == "file_issue"):
            # #914: a shadow's issue is recorded, never posted by the seat; the host files it
            # later, if the real review did not, as the reader. Nothing here reaches GitHub.
            return self._record_shadow_issue(raw, request)
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
                                                ci_fix=self.scope.ci_fix,
                                                shadow_fix=self.scope.shadow_fix)
                        # safe_push returns only after exact ref + PR readback and
                        # durable audit. Failure to commit completion leaves intent.
                        supervisor.confirm_push(self.scope.run_id, self.scope.repo,
                                                self.scope.number, self.scope.head)
                    except safe_push.scope_lock.ScopeHold:
                        # A hold (#482), raised before any write: the intent is withdrawn so the
                        # run is not mistaken for one that may have written, and `retry` can
                        # re-arm it once a maintainer releases the hold.
                        supervisor.abort_push(self.scope.run_id, self.scope.repo,
                                              self.scope.number, self.scope.head)
                        raise
                    except Exception as exc:
                        # An explicit attempt boundary, not exception text, determines
                        # whether a failed push must occupy the host-side hold.
                        if isinstance(exc, safe_push.PushFailure) or not isinstance(
                                exc, (broker.BrokerDenied, ProtocolError)):
                            if not self.scope.run_id or not self.scope.ledger_db:
                                raise ProtocolError('post-write hold has no host ledger') from exc
                            from .run_supervisor import Supervisor
                            outcome = safe_push.held_outcome(exc)
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
        if operation == "review" and verdict == "APPROVE":
            # Refusals no review body can cure come before every format check on the body
            # (Not verified, findings, Requirements): the seat is never sent to fix the format
            # of an approval that could not go through (#520).
            reason = self._partial_view()
            if reason:
                # Before the capability is consumed and before any GitHub read or write: the seat
                # was not shown the whole change, so it cannot approve it (#93, #110).
                raise ProtocolError(PARTIAL_VIEW_REFUSAL.format(reason=reason[:300]))
            # A live read of the head's CI: an approval of a head whose checks failed is a false
            # green, so the seat must request changes.
            from . import ci
            # Only the operator's required checks gate it, when the loop names them (#368).
            refusal = ci.approval_refusal(ci.gating(ci.read(self._loop, self.scope.head),
                                                    config.required_checks(self._loop)))
            if refusal:
                raise ProtocolError(refusal)
        if operation == "review" and not broker.has_not_verified(body):
            raise ProtocolError(broker.NOT_VERIFIED_REFUSAL)
        if operation == "review":
            # Numbered findings (#475): every open one accounted for, new ones on changed code.
            # Before the capability is consumed, so the reviewer can resubmit in the same turn.
            from . import findings, state as state_mod
            st = state_mod.state_for(self._loop)
            entry = st.findings_get(self.scope.number)
            if self._no_write:
                # A shadow (#218) is judged on the ledger its prompt was built from: the real
                # review of the same head may have recorded findings meanwhile, which it never
                # saw (on #1005 they turned its approval into a forced block).
                snapshot = st.shadow_findings_get(self.scope.number, self.scope.head)
                entry = entry if snapshot is None else snapshot
            reason = findings.check(self._loop, entry, self.scope.number, self.scope.head, body,
                                    verdict)
            if reason:
                raise ProtocolError(reason)
            # A block must point at what this PR changed (#476).
            # A seat that was not shown the whole change must still be able to block and say so.
            # Red CI (#348 refuses the APPROVE and asks for REQUEST_CHANGES naming the checks) is
            # citable by the check's name; unreadable CI, like a partial view, is not held to it.
            reason = ""
            if verdict == "REQUEST_CHANGES" and not self._partial_view():
                from . import ci
                gating = ci.gating(ci.read(self._loop, self.scope.head),
                                   config.required_checks(self._loop))
                if gating is not None:
                    reason = findings.check_citations(
                        self._loop, self.scope.number, verdict, body,
                        failing=[*gating.failed, *gating.missing])
            if reason:
                raise ProtocolError(reason)
            # #687: a block whose only ground is a test this sandbox could not run, when a
            # required check ran it at this head (or skipped it too).
            if verdict == "REQUEST_CHANGES":
                from . import ci, ci_tests
                head_ci = ci.read(self._loop, self.scope.head)
                reason = ci_tests.refusal(
                    ci_tests.read(self._loop, head_ci, config.required_checks(self._loop),
                                  self.scope.head), verdict, body)
                if reason:
                    raise ProtocolError(reason)
            # Requirements of the issue(s) the PR closes (#511); a PR closing none is unaffected.
            from . import issue_facts
            reason = issue_facts.check(self._loop, self.scope.number, verdict, body)
            if reason:
                raise ProtocolError(reason)
        if operation == "review" and verdict == "APPROVE":
            # Paths only a human may approve (#478): also before the capability is consumed, so
            # the seat's REQUEST_CHANGES in the same turn still goes through.
            refusal, held = self._human_paths_check()
            if refusal:
                raise ProtocolError(refusal)
            if held:
                # A clean head on a reserved path is a person's to approve: the turn ends here,
                # done, with no GitHub write. Refusing instead would leave a reviewer with nothing
                # to request changes for and no way to finish, so the turn would fail and retry.
                return self._hold_for_person(held, body)
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
            except broker.BrokerConflict as exc:
                return self._end_on_conflict(str(exc), body)     # #1243: nothing claimed or sent
            except ReviewedPrIneligible as exc:
                # The review exists on GitHub: name it on the run before answering the sandbox.
                try:
                    from .run_supervisor import Supervisor
                    Supervisor(self.scope.ledger_db, create=False).record_review_ineligible(
                        self.scope.run_id, exc.reason)
                except Exception as persistence_error:
                    raise ProtocolError(REVIEW_POSTED_UNRECORDED) from persistence_error
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
            # An App reviewer (#1163) is never requested on GitHub: the request is always
            # delivered to the reviewer gate by the host, as for #532.
            app_reviewer = operation == "request_review" and config.reviewer_is_app(self._loop)
            if after_push or app_reviewer:
                try:
                    from . import gh, review_kick
                    live = gh.api(self._loop, gh.pr_path(self._loop, self.scope.number),
                                  login=self._loop["read_token"])
                    pending = (live if isinstance(live, dict) and (
                        app_reviewer or review_kick.already_requested(self._loop, live)) else None)
                except Exception:
                    pending = None
            try:
                result = broker.perform(self._loop, repo=self.scope.repo,
                                        number=self.scope.number, head=head,
                                        role=self.scope.role, branch=self.scope.branch,
                                        operation=operation, verdict=verdict, body=body,
                                        require_verdict=not after_push)
            except broker.BrokerConflict as exc:
                return self._end_on_conflict(str(exc), body)     # #1243: nothing was sent
            if pending is not None:
                self._kick_review(pending, head)
        if operation == 'review':
            try:
                from . import findings
                findings.record(self._loop, self.scope.number, self.scope.head, body)
            except Exception:
                pass  # the review is already written; state is best effort
            from . import merge_check
            merge_check.refresh_quietly(self._loop, self.scope.number)   # #1163: a new verdict
        self.completed = True
        return result

    def _human_paths_check(self) -> tuple[str, str]:
        """``(refusal, held)`` for an APPROVE under ``human_paths`` (#478).

        ``refusal``: the file list could not be read, so nothing can be judged; the seat may
        retry. ``held``: the approval is a person's, naming why: the diff touches a reserved
        path, or it is too large for GitHub to list in full. Both empty: the APPROVE proceeds.
        The diff is read live from GitHub, never from the seat or the PR.
        """
        if not config.human_paths(self._loop):
            return "", ""
        from . import gh
        files, error = gh.pr_files_read(self._loop, self.scope.number)
        if files is None:
            return (f"the PR's file list could not be read ({error[:200]}), so the loop cannot "
                    "check it against human_paths; nothing was written, retry"), ""
        if len(files) >= gh.PR_FILES_LISTING_LIMIT:
            return "", (f"GitHub lists at most {gh.PR_FILES_LISTING_LIMIT} changed files and "
                        "this PR reaches that limit, so it cannot be checked in full")
        paths = []
        for item in files:
            for key in ("filename", "previous_filename"):
                if isinstance(item.get(key), str):
                    paths.append(item[key])
        hits = config.human_path_hits(self._loop, paths)
        if not hits:
            return "", ""
        names = ", ".join(hits[:10]) + (f" and {len(hits) - 10} more" if len(hits) > 10 else "")
        return "", f"it touches {names}"

    def _end_on_conflict(self, why: str, body: str = "") -> dict:
        """End a reviewer turn whose APPROVE met a conflict on the live read right before the write
        (#1243): capability spent, nothing written, no hold (the conflict merge brings a new head,
        reviewed as usual). A shadow never reaches the write path, so never this."""
        reason = (f"{why}: an approval would not stand, so none is posted. Your turn is done; "
                  "do not retry. The conflict is resolved on a new head, which gets a fresh "
                  "review.")
        self._used = True
        self.completed = True
        return {"accepted": False, "held": "conflict", "reason": reason}

    def _hold_for_person(self, why: str, body: str = "") -> dict:
        """End this reviewer turn without approving: the head waits for a person (#478).

        The hold is recorded for this head (the gate starts no further review there, and the
        watchdog does not call it a stall); the operator gets one notice. The capability is spent
        and the turn is complete, so it is neither retried nor reported as a failure.

        A no-write run (the selftest's live turn, a shadow review) records what would have
        happened and touches neither: a hold set by a run that writes nothing would stop the
        real reviewer at this head.
        """
        if self._no_write:
            self.recorded.append({"verdict": "APPROVE", "body": body, "authorized": False,
                                  "denial": "", "held": why})
            self._used = True
            self.completed = True
            return {"accepted": False, "held": "human_paths",
                    "reason": f"approval left to a person: {why}. Your turn is done; do not "
                              "retry."}
        from . import observer, state as state_mod
        st = state_mod.state_for(self._loop)
        st.human_hold_set(self.scope.number, self.scope.head, why)
        try:
            observer.notify(self._loop, st, "human_paths", self.scope.number, self.scope.head,
                            outcome=f"{why}: the loop would approve, a person must review and "
                                    "approve")
        except Exception:
            pass                 # a notice never changes the hold
        self._used = True
        self.completed = True
        return {"accepted": False, "held": "human_paths",
                "reason": f"approval left to a person: {why}. Your turn is done; do not retry."}

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
                operation="review", approve=verdict == "APPROVE")
        except broker.BrokerConflict as exc:
            # #1250: the answer a real write gives (#1243), so the no-write run behaves the same.
            entry["denial"] = entry["held"] = str(exc)[:200]
            return self._end_on_conflict(str(exc), body)
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
                                            self.scope.number, self.scope.head, verdict, body,
                                            shadow_run=self.scope.shadow_run)
        self.completed = True
        try:
            _deliver_ruling(self._loop, self.scope, supervisor, recorded["turn_key"], verdict, body)
        except Exception:
            # Already durable, and the operator outbox reports it regardless.
            pass
        from . import merge_check
        merge_check.refresh_quietly(self._loop, self.scope.number)       # #1163: a ruling
        if verdict == "REJECT" and self.scope.shadow_run:
            # #1004 part 3: an upheld shadow review's findings go to the fixer now; the watchdog
            # sweep queues the same turn (same key) if this does not.
            try:
                from . import gate
                gate.queue_shadow_fix(self._loop, self.scope.number, self.scope.head,
                                      db=self.scope.ledger_db)
            except Exception:
                pass
        return {"accepted": True}


    def _triage(self, raw: bytes, request: object) -> object:
        """Record the one triage (labels from the loop's list, an optional comment), then write it.

        The issue text this turn read is untrusted, so this is the boundary: only labels the
        operator listed, at most ``max_labels``, and a comment only where the loop allows one.
        The response is ok once the triage is durable; the GitHub writes follow from the host's
        current configuration and live issue, and their outcome is recorded, never retried.
        """
        from .run_supervisor import Supervisor, issue_inputs
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
        if not isinstance(body, str) or len(body.strip()) > issue_inputs.TRIAGE_COMMENT_MAX:
            raise ProtocolError(f"the comment must be text of at most {issue_inputs.TRIAGE_COMMENT_MAX}"
                                " characters; nothing was written, resubmit")
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
        why = ("the reviewer is a GitHub App, which GitHub cannot request"
               if config.reviewer_is_app(self._loop) else
               "reviewer was already requested, so GitHub sends no event")
        log(f"#{self.scope.number} @ {head[:7]}: {why} — "
            f"{'delivered the review request to the gate' if sent else 'the watchdog will start it'}")


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
            socket_path: str = contained.BROKER_MOUNT + "/broker.sock") -> dict:
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
