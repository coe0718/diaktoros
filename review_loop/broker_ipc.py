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
import secrets
import socket
import stat
import threading
from dataclasses import dataclass

from . import broker, config, safe_push

MAX_REQUEST = 16 * 1024
MAX_BODY = 12 * 1024
MAX_PUSH_REQUEST = 196 * 1024


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
    "answers instead with `python -m review_loop.broker_client request_review --answers-file "
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
        self._pushed_head: str | None = None
        # How the fixer's answers comment ended ('posted', 'uncertain', 'denied', 'unrecorded'),
        # a host-chosen word the sandbox may see; None when no answers were sent.
        self.answers_outcome: str | None = None
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
                    self._dispatch(_read_line(conn, MAX_PUSH_REQUEST if self.scope.role in
                                              ("fixer", "issue_fixer") else MAX_REQUEST))
                    # Never relay arbitrary GitHub response fields into the namespace.
                    response = {"ok": True, "result": {"accepted": True}}
                    if self.answers_outcome:
                        response["result"]["answers"] = self.answers_outcome
                except (ProtocolError, broker.BrokerDenied, ValueError, UnicodeError, TimeoutError) as exc:
                    response = {"ok": False, "error": str(exc) if isinstance(exc, ProtocolError) else "write denied"}
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
                                                manifest=request["manifest"])
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
        answers_only = (operation == "request_review" and not self._pushed_head
                        and bool(self._partial_view()))
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
            reason = self._partial_view()
            if reason:
                # Before the capability is consumed and before any GitHub read or write: the seat
                # was not shown the whole change, so it cannot approve it (#93, #110).
                raise ProtocolError(PARTIAL_VIEW_REFUSAL.format(reason=reason[:300]))
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
            result = submit(self._loop, self.scope, ledger, verdict, body)
        else:
            if operation == 'review' and self.require_receipt:
                raise ProtocolError('host review claim required')
            if answers_only:
                # Nothing was pushed, so nothing new to review: the answers comment at the head
                # the fixer was given is the whole write. It must actually land.
                self.answers_outcome = self._publish_answers(head, body)
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
            result = broker.perform(self._loop, repo=self.scope.repo, number=self.scope.number,
                                    head=head, role=self.scope.role, branch=self.scope.branch,
                                    operation=operation, verdict=verdict, body=body,
                                    require_verdict=not after_push)
        self.completed = True
        return result

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

    def _publish_answers(self, head: str, text: str) -> str:
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
            try:
                supervisor.begin_answers(**record, state="denied", error=reason)
            except Exception:
                pass
            return "denied"
        try:
            supervisor.begin_answers(**record)
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
                                          error=f"POST outcome unknown: {type(exc).__name__}")
            except Exception:
                pass
            return "uncertain"
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
                                        error=f"PR create outcome unknown: {type(exc).__name__}")
            raise ProtocolError("the branch was pushed but the PR could not be confirmed: do "
                                "not retry; say so")
        supervisor.issue_fix_status(run_id, "opened", pr_number=pr)
        try:
            broker.request_issue_pr_review(current, repo=repo, pr=pr, login=pushed["login"])
        except Exception as exc:
            # The PR is open as the fixer, so the reviewer gate's `opened` still starts the loop.
            supervisor.issue_fix_status(run_id, "opened",
                                        error=f"review request not confirmed: {type(exc).__name__}")
            return {"accepted": True}
        supervisor.issue_fix_status(run_id, "requested")
        return {"accepted": True}

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
                                        error=f"POST outcome unknown: {type(exc).__name__}")
            raise ProtocolError("the comment's outcome is unknown: do not retry; say so")
        supervisor.issue_fix_status(run_id, "posted", comment_id=comment)
        return {"accepted": True}


def _deliver_triage(launch_loop: dict, scope: RunScope, supervisor, labels: list[str],
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
                                 error=f"POST outcome unknown: {type(exc).__name__}")
        return
    supervisor.triage_status(scope.run_id, "posted", comment_id=comment_id)


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
                                 comment_error=f"POST outcome unknown: {type(exc).__name__}")
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
