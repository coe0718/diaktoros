"""#2302: every file the plugin appends to has a bound, or is registered as deliberately unbounded.

#549 was a file the plugin appended to forever. This guard scans every Python source outside the
suite (``_sources.python_sources``), by AST, for an append open: ``open(..., "a"...)`` (also
``io.open``, ``codecs.open`` and the like, and a mode the scan cannot read, such as a variable),
``<path>.open("a"...)``, ``os.fdopen(..., "a"...)``, an ``os.open`` whose flags hold ``O_APPEND``
or are a name or expression the scan cannot resolve to append-free flags (#2377), and a call of
the shared append-and-prune ``util.append_keeping``. Every call is counted in the function that
makes it. Opens are counted, not only the
functions that hold them: each
site, keyed ``path::function``, must be in exactly one registry, with ``OPENS`` giving its count
where it holds more than one, so a second open added to a registered function fails too:

* ``APPENDS``: the file's bound (size limit, rotation or pruning rule) and where it is enforced,
  with ``PROOF`` naming the behaviour test that writes past the bound and sees it hold;
* ``UNBOUNDED``: an append-only record kept whole on purpose, with the reason beside it;
* ``NOT_LOGS``: an append-mode open that writes no record (a lock file, a spike's fixture edit,
  CI's step summary).

The issue (#2302) listed seven sites; the scan also catches the ``"a+b"`` lock opens and the
non-plugin files, all in ``NOT_LOGS``.

A new append site in none of them fails ``test_every_append_site_is_registered``; a registered
site that no longer exists fails it too, so the registry cannot go stale.

What the scan covers, and what it does not (perfect static detection is not possible, #2379):

* ``os.open`` is counted per call: one for each ``os.open`` whose flags argument (second
  positional or ``flags=``) holds ``O_APPEND`` (``os.O_APPEND``, a bare ``O_APPEND``,
  ``getattr(os, "O_APPEND")``) or names a variable assigned (``=``, ``|=``, annotated) in the same
  function from an expression that holds it, directly or through another such variable. Two
  ``os.open`` calls sharing one flags variable count two. Branches are followed: an assignment
  counts if it may reach the open on any path (``if``/``else``, ``try``/``except``, ``match``,
  a loop's next pass or ``break``), so a non-append rebinding on one branch does not hide the
  open; the read errs toward counting more, never fewer (``_Bindings``).
* Flags count as append unless the scan proves them append-free (#2377, ``_flags_may_append``).
  Append-free are: ``os.O_X`` or ``getattr(os, "O_X"[, default])`` for an ``O_`` flag other than
  ``O_APPEND`` that the host running the guard defines without the append bit; an int constant
  without that bit; ``a | b`` and ``a ^ b`` of append-free values, ``a & b`` with one append-free
  side, ``a if c else b`` and ``a or b`` of append-free values; and a name every value of which
  is append-free. A name is followed to its innermost binding scope: a lambda, the function,
  then the enclosing functions and the module (a module constant, a closure variable).
  Everything else counts as append: any other ``os`` attribute (``os.ST_NOATIME`` is 1024, the
  Linux ``O_APPEND``), ``~``, ``<<`` (``1 << 10``), ``+`` and every other operator, a parameter
  (a lambda's included), an import, a ``for``, ``with``, ``except ... as`` or ``match`` capture
  target, a ``global`` or ``nonlocal`` name, a name nothing binds, any other call, an
  ``os.open(*args)`` or ``(**kw)``. A false alarm only asks for a registry entry; a miss is an
  unbounded file. Which bit is ``O_APPEND`` is read on the host running the guard (CI's Linux).
* A mention of ``O_APPEND`` that reaches no ``os.open`` is not an open and is not counted
  (#2377). Flags handed to a helper are counted at the helper's own ``os.open``, whose flags
  parameter the scan cannot resolve, so it counts as append there.
* Not covered: an opener reached through an alias (``o = open; o(p, "a")``, ``from os import
  open``), or a mode or flags value produced at run time from strings. Those need a reviewer's
  eye.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import collections
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _sources import ROOT, python_sources  # noqa: E402
from diaktoros import corpus, gate_decisions, state  # noqa: E402

APPENDS = {
    "diaktoros/state.py::LoopState.note":
        "watchdog.log: past LOG_PRUNE_BYTES, cut to its last LOG_KEEP lines that fit in that many "
        "bytes, under _log_lock (LoopState._prune_log)",
    "diaktoros/gate_decisions.py::record":
        "gate-decisions.jsonl: pruned to its last KEEP (200) lines under its flock "
        "(util.append_keeping)",
    "diaktoros/corpus.py::record":
        "corpus_scores.jsonl: pruned to its last SCORES_KEEP runs under its flock "
        "(util.append_keeping)",
    "diaktoros/util.py::append_keeping":
        "the shared append-then-prune: the file is cut to its caller's last `keep` lines under "
        "the caller's lock",
    "diaktoros/run_supervisor/workers.py::WorkerLifecycle._worker_log":
        "<ledger>.workers.log: the host rotates it once to .1 past WORKER_LOG_MAX at a spawn; "
        "a worker, which holds it as its stderr, writes only up to WORKER_LOG_MAX "
        "(waits.cap_stderr, called by run_supervisor.main) and opens it only while "
        "waits.within_cap holds",
    "diaktoros/run_supervisor/waits.py::note":
        "<ledger>.workers.log: a wait line is appended only while waits.within_cap holds "
        "(at most WORKER_LOG_MAX); the host's next spawn rotates it",
}

# The behaviour test that writes past each APPENDS entry's bound and asserts it held.
PROOF = {
    "diaktoros/state.py::LoopState.note": "test_append_bounds.Bounds.test_watchdog_log_is_pruned",
    "diaktoros/gate_decisions.py::record": "test_append_bounds.Bounds.test_gate_decisions_are_pruned",
    "diaktoros/corpus.py::record": "test_append_bounds.Bounds.test_corpus_scores_are_pruned",
    "diaktoros/util.py::append_keeping": "test_append_bounds.Bounds.test_append_keeping_prunes",
    "diaktoros/run_supervisor/workers.py::WorkerLifecycle._worker_log":
        "test_run_supervisor.Lifecycle.test_a_worker_never_grows_its_log_past_the_cap",
    "diaktoros/run_supervisor/waits.py::note":
        "test_waits.ClaimWaits.test_a_wait_is_not_appended_past_the_log_cap",
}

_AUDIT = ("broker-audit.jsonl, kept whole on purpose (#2302): an audit record that four readers "
          "depend on (push_settle._attempted, authorship.pushed_heads, "
          "history_sources.audit_writes, desktop_api._record_broker), and a dropped record gives "
          "a false answer (a push counted as never attempted, a pushed head flagged as "
          "misattributed). About 200 bytes per broker write (~14 MB a year on the busiest loop); "
          "rotation, if it is ever needed, is its own change")
UNBOUNDED = {
    "diaktoros/broker/__init__.py::_audit": _AUDIT,
    "diaktoros/safe_push.py::_audit": _AUDIT,
}

NOT_LOGS = {
    "diaktoros/state.py::LoopState._transition_write": "stack-transitions.lock: a flock file, empty",
    "diaktoros/gate_failures.py::Ledger._locked": "the gate-failure ledger's flock file, empty",
    "diaktoros/seat_model.py::profile_lock": "a per-profile flock file in diaktoros-seat-locks/",
    "diaktoros/app_tokens.py::held": "an App token file's flock file, empty",
    "diaktoros/config/__init__.py::push_policy_lock": ".fixer-push-policy.lock: a flock file, empty",
    "diaktoros/onboarding.py::_locked": ".onboarding.lock: a flock file, empty",
    "diaktoros/pacing.py::_locked": "the pacing file's flock file, empty",
    "tools/github_app_spike.py::run":
        "a one-off spike that edits app.py in its own scratch clone, not plugin state",
    ".github/scripts/plugin_guard.py::main":
        "CI's $GITHUB_STEP_SUMMARY: a file the Actions runner owns, one job long",
}

# The number of append opens a registered function holds, where it is more than one (an
# ``os.open`` with ``O_APPEND`` flags and the ``os.fdopen(.., "a")`` that wraps the same descriptor
# count one each; each ``os.open`` counts, even when several share one flags variable, #2379).
# Another open added to the function changes its count, so the guard fails on it.
OPENS = {
    "diaktoros/run_supervisor/waits.py::note": 2,              # os.open(O_APPEND), os.fdopen("a")
    "diaktoros/run_supervisor/workers.py::WorkerLifecycle._worker_log": 2,   # os.open(flags), os.fdopen("ab")
}


def _mode_is_append(node) -> bool:
    """An append mode, or one the scan cannot read (a variable): that may be append too."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and "a" in node.value
    return True


# Modules whose ``open(path, mode)`` takes the mode second, as the builtin does.
_OPEN_MODULES = {"io", "codecs", "builtins", "gzip", "bz2", "lzma", "_io"}


def _is_append_call(call: ast.Call) -> bool:
    """Does ``call`` open a file in an append mode, or append through ``util.append_keeping``?
    Each caller of that shared helper is its own site: its own file, with its own ``keep``.
    ``os.open`` is not judged here: its flags are (``_flags_may_append``)."""
    func = call.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
    if name == "append_keeping":
        return True
    owner = func.value.id if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) else None
    if name == "fdopen" or (name == "open" and (isinstance(func, ast.Name) or owner in _OPEN_MODULES)):
        position = 1                      # open(path, mode), io.open(...), os.fdopen(fd, mode)
    elif name == "open" and owner not in _NOT_PATH_OPENS and _may_be_path_open(call):
        position = 0                      # <path>.open(mode), a variable mode included
    else:
        return False
    modes = [kw.value for kw in call.keywords if kw.arg == "mode"] + call.args[position:position + 1]
    return any(_mode_is_append(mode) for mode in modes)


# Owners whose ``.open`` is not ``Path.open``: ``os.open`` (its ``O_APPEND`` is judged apart) and
# modules whose ``open`` takes a name first, its mode (if any) after it.
_NOT_PATH_OPENS = {"os", "tarfile", "zipfile", "shelve", "dbm", "webbrowser", "wave", "sunau"}
# The parameters of ``Path.open(mode='r', buffering=-1, encoding=None, errors=None, newline=None)``.
_PATH_OPEN_KEYWORDS = {"mode", "buffering", "encoding", "errors", "newline"}


def _may_be_path_open(call: ast.Call) -> bool:
    """Could ``<x>.open(...)`` be ``Path.open`` with a mode? The scan cannot tell what ``x`` is, so
    only arguments ``Path.open`` cannot take rule it out: a keyword it has no parameter for (a URL
    opener's ``timeout``, tarfile's ``fileobj``), a string second argument (``buffering`` is an
    int, so ``x.open(name, "r:gz")`` is not it), more than five positionals, or no mode at all
    (the default ``"r"``). Anything else counts, a variable first argument included: it may be
    the mode, and a false alarm only asks for a registry entry, where a miss is an unbounded file
    (#2302 review)."""
    if any(kw.arg not in _PATH_OPEN_KEYWORDS for kw in call.keywords) or len(call.args) > 5:
        return False
    second = call.args[1] if len(call.args) > 1 else None
    if isinstance(second, ast.Constant) and isinstance(second.value, str):
        return False
    return bool(call.args) or any(kw.arg == "mode" for kw in call.keywords)


def _is_o_append(node) -> bool:
    """A mention of ``O_APPEND``: ``os.O_APPEND``, a bare ``O_APPEND`` or ``getattr(os, "O_APPEND")``."""
    return (isinstance(node, ast.Name) and node.id == "O_APPEND"
            or isinstance(node, ast.Attribute) and node.attr == "O_APPEND"
            or isinstance(node, ast.Constant) and node.value == "O_APPEND")


_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_BINDERS = (*_FUNCTIONS, ast.ClassDef)


def _body_nodes(scope):
    """Every node of ``scope``'s own body, not those of the functions and classes nested in it
    (their names, which ``scope`` binds, are yielded; their bodies are not)."""
    pending = list(scope.body) if isinstance(scope.body, list) else [scope.body]
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, _BINDERS):
            pending.extend(ast.iter_child_nodes(node))


def _params(function) -> set:
    """The names of a function's or a lambda's parameters."""
    params = function.args
    return {a.arg for a in (*params.posonlyargs, *params.args, *params.kwonlyargs,
                            *filter(None, (params.vararg, params.kwarg)))}


def _binds_unreadably(node, name: str) -> bool:
    """Does ``node`` bind ``name`` other than by an assignment ``_bindings`` reads: an import, a
    ``def`` or ``class``, an ``except ... as``, a ``match`` capture (``case x``, ``*x``,
    ``**x``; stored as strings, not ``ast.Name``), a ``global`` or ``nonlocal``?"""
    return (isinstance(node, (ast.Import, ast.ImportFrom))
            and any((a.asname or a.name.split(".")[0]) == name for a in node.names)
            or isinstance(node, _BINDERS) and getattr(node, "name", None) == name
            or isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar))
            and node.name == name
            or isinstance(node, ast.MatchMapping) and node.rest == name
            or isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names)


def _bindings(scope, name: str):
    """The values ``scope`` assigns to ``name`` (``=``, ``|=``, an annotated or ``:=``
    assignment), an empty list when it binds no ``name``, or None when it binds it in a way the
    scan cannot read (a parameter, a lambda's included, a ``for``, ``with`` or ``match`` target,
    tuple unpacking, or any form ``_binds_unreadably`` names)."""
    if isinstance(scope, _FUNCTIONS) and name in _params(scope):
        return None
    values, read = [], set()
    for node in _body_nodes(scope):
        if isinstance(node, ast.Assign):
            simple = [t for t in node.targets if isinstance(t, ast.Name) and t.id == name]
            values += [node.value] * bool(simple)
            read.update(map(id, simple))
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.NamedExpr)) \
                and isinstance(node.target, ast.Name) and node.target.id == name:
            values += [node.value] if node.value is not None else []
            read.add(id(node.target))
    for node in _body_nodes(scope):
        if (isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Store)
                and id(node) not in read or _binds_unreadably(node, name)):
            return None
    return values


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _scope_nodes(node):
    """Every node of ``node``'s own scope: nested functions and classes, each a site of its own
    (their decorators and defaults included, as before), are not entered."""
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, _SCOPES):
            yield child
            yield from _scope_nodes(child)


def _nested_scopes(node):
    """The functions and classes nested directly in ``node``'s scope."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _SCOPES):
            yield child
        else:
            yield from _nested_scopes(child)


def _os_open_flags(call: ast.Call):
    """The flags argument of an ``os.open(path, flags, ...)`` call (by position or ``flags=``),
    or None when ``call`` is not ``os.open`` or passes none."""
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == "open"
            and isinstance(func.value, ast.Name) and func.value.id == "os"):
        return None
    flags = [kw.value for kw in call.keywords if kw.arg == "flags"] + call.args[1:2]
    return flags[0] if flags else None


_DEFS = (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.NamedExpr)
_LOOPS = (ast.For, ast.AsyncFor, ast.While)
_TRIES = tuple(getattr(ast, name) for name in ("Try", "TryStar") if hasattr(ast, name))


def _merge(*envs) -> dict:
    """The union of name -> may-reach-assignments maps: every assignment that any path brings."""
    out: dict = {}
    for env in envs:
        for name, defs in env.items():
            out[name] = out.get(name, frozenset()) | defs
    return out


def _own(node) -> list:
    """``node`` and every node of its own scope (``_scope_nodes``)."""
    return [node, *_scope_nodes(node)]


class _Bindings:
    """The assignments to plain names in one scope, and every one that may reach each read of a
    name (#2379 reviews). It follows the statements' structure: an assignment hides the earlier
    value on the path after it, so in ``flags = os.O_APPEND; helper(flags); flags = os.O_APPEND;
    os.open(p, flags)`` the open reads only the second value. Every path reaches the code after
    it: both arms of an ``if`` or ``try``, each ``match`` case, a loop's body back to its own top
    and, through ``break``, to the code after the loop. So after ``flags = os.O_APPEND`` and
    ``if c: flags = os.O_RDONLY``, ``os.open(p, flags)`` may read either value and is an append
    open. Where it cannot tell it keeps more assignments, never fewer: ``return``, ``raise`` and
    ``continue`` are read as falling through too, an ``except`` sees every value its ``try``
    assigns, a ``:=``, a tuple or ``for`` target or an import does not hide the earlier value. It
    may over-count, which fails the guard; it does not hide an append open."""

    def __init__(self, scope):
        self.defs: list = []          # every assignment to a plain name, in visiting order
        self.names: dict = {}         # id(assignment) -> the names it binds
        self.reads: dict = {}         # id(read) -> the assignments that may reach it
        self._loops: list = []        # per enclosing loop: [envs at ``break``, at ``continue``]
        self.scope = scope
        body = scope.body if isinstance(scope, (ast.Module, *_SCOPES)) else []
        for child in ast.iter_child_nodes(scope):
            if not isinstance(child, _SCOPES) and all(child is not s for s in body):
                self._expr(child, {})     # defaults, decorators, annotations: no local value yet
        self._block(body, {})
        self._held = self._holding()

    def _see(self, read, env: dict, name: str) -> None:
        """Record that the values of ``name`` in ``env`` may reach ``read``."""
        self.reads[id(read)] = self.reads.get(id(read), frozenset()) | env.get(name, frozenset())

    def _define(self, node, name: str, env: dict, hide: bool = True) -> None:
        if all(node is not d for d in self.defs):
            self.defs.append(node)
        self.names.setdefault(id(node), set()).add(name)
        env[name] = frozenset({node}) | (frozenset() if hide else env.get(name, frozenset()))

    def _expr(self, node, env: dict) -> None:
        """The reads (and ``:=`` assignments, which keep the earlier value too) in ``node``."""
        for n in _own(node):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                self._see(n, env, n.id)
            elif isinstance(n, ast.NamedExpr):
                self._define(n, n.target.id, env, hide=False)

    def _block(self, stmts: list, env: dict) -> dict:
        for stmt in stmts:
            env = self._stmt(stmt, env)
        return env

    def _loop(self, stmt, env: dict) -> dict:
        """A loop's body starts from the values before it or from its own last pass (or a
        ``continue``), to a fixed point; the code after it sees those and every ``break``."""
        top = env
        while True:
            self._loops.append([[], []])
            if isinstance(stmt, ast.While):
                self._expr(stmt.test, top)
            end = self._block(stmt.body, dict(top))
            breaks, continues = self._loops.pop()
            again = _merge(env, end, *continues)
            if again == top:
                break
            top = again
        return _merge(self._block(stmt.orelse, dict(top)), top, *breaks)

    def _try(self, stmt, env: dict) -> dict:
        """Each ``except`` may start anywhere in the ``try``: it sees every value the ``try``
        assigns and the values before it."""
        done = self._block(stmt.body, dict(env))
        assigned = [{name: frozenset({d})} for s in stmt.body for n in _own(s)
                    for d in self.defs if d is n for name in self.names[id(d)]]
        start = _merge(env, done, *assigned)
        handled = []
        for handler in stmt.handlers:
            if handler.type is not None:
                self._expr(handler.type, start)
            handled.append(self._block(handler.body, dict(start)))
        ends = _merge(self._block(stmt.orelse, dict(done)), *handled)
        return self._block(stmt.finalbody, _merge(start, ends)) if stmt.finalbody else ends

    def _stmt(self, stmt, env: dict) -> dict:
        """The values after ``stmt``, recording the reads in it."""
        if isinstance(stmt, _SCOPES):
            return env                # a site of its own, decorators and defaults included
        if isinstance(stmt, ast.If):
            self._expr(stmt.test, env)
            return _merge(self._block(stmt.body, dict(env)), self._block(stmt.orelse, dict(env)))
        if isinstance(stmt, _LOOPS):
            if not isinstance(stmt, ast.While):
                self._expr(stmt.iter, env)
            return self._loop(stmt, env)
        if isinstance(stmt, _TRIES):
            return self._try(stmt, env)
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                self._expr(item, env)
            return self._block(stmt.body, env)
        if isinstance(stmt, ast.Match):
            self._expr(stmt.subject, env)
            cases = []
            for case in stmt.cases:
                if case.guard is not None:
                    self._expr(case.guard, env)
                cases.append(self._block(case.body, dict(env)))
            return _merge(env, *cases)
        if isinstance(stmt, (ast.Break, ast.Continue)) and self._loops:
            self._loops[-1][isinstance(stmt, ast.Continue)].append(dict(env))
        self._expr(stmt, env)
        if isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name):
            self._see(stmt, env, stmt.target.id)
        if isinstance(stmt, _DEFS) and stmt.value is not None:
            for target in stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]:
                if isinstance(target, ast.Name):
                    self._define(stmt, target.id, env)
        return env

    def deps(self, expr) -> frozenset:
        """The assignments that may reach a read in ``expr`` (an expression or an assignment):
        the names its value reads and, for ``x |= ...``, the earlier value of ``x``."""
        value = expr.value if isinstance(expr, _DEFS) else expr
        reads = [n for n in _own(value) if isinstance(n, ast.Name)]
        if isinstance(expr, ast.AugAssign):
            reads.append(expr)
        return frozenset().union(*(self.reads.get(id(r), frozenset()) for r in reads))

    def _spells(self, expr) -> bool:
        value = expr.value if isinstance(expr, _DEFS) else expr
        return any(_is_o_append(n) for n in _own(value))

    def _holding(self) -> set:
        """The ids of the assignments whose value holds ``O_APPEND``, to a fixed point (in a
        loop an assignment may read itself)."""
        held: set = set()
        changed = True
        while changed:
            changed = False
            for d in self.defs:
                if id(d) not in held and (self._spells(d) or any(id(x) in held for x in self.deps(d))):
                    held.add(id(d))
                    changed = True
        return held

    def holds(self, expr) -> bool:
        """Does ``expr`` hold ``O_APPEND``, spelled in it or through an assignment that may reach
        a name it reads?"""
        return self._spells(expr) or any(id(d) in self._held for d in self.deps(expr))


_APPEND_BIT = getattr(os, "O_APPEND", 0)


def _os_flag_may_append(name: str) -> bool:
    """Could ``os.<name>`` hold the append bit? Only an ``O_`` open flag this host defines, other
    than ``O_APPEND``, without that bit is append-free (#2386 review: ``os.ST_NOATIME`` is 1024,
    the Linux ``O_APPEND``). A flag this host lacks counts as append: its value is unknown."""
    value = getattr(os, name, None) if name.startswith("O_") and name != "O_APPEND" else None
    return not isinstance(value, int) or bool(value & _APPEND_BIT)


def _getattr_os(call: ast.Call):
    """The attribute name and default of ``getattr(os, "NAME"[, default])``, or None."""
    args = call.args
    if (isinstance(call.func, ast.Name) and call.func.id == "getattr" and not call.keywords
            and len(args) in (2, 3) and isinstance(args[0], ast.Name) and args[0].id == "os"
            and isinstance(args[1], ast.Constant) and isinstance(args[1].value, str)):
        return args[1].value, (args[2] if args[2:] else None)
    return None


def _flags_may_append(expr, chain, bindings, seen=frozenset()) -> bool:
    """Could the flags ``expr`` include ``O_APPEND``? Yes unless the scan proves them append-free
    (#2377, #2386 review): an int without the append bit, an ``os`` open flag without it
    (``_os_flag_may_append``), ``getattr(os, "O_X", default)`` with both append-free, ``|`` and
    ``^`` of append-free values, ``&`` with one append-free side, ``a if c else b`` and ``a or
    b`` of append-free values. Every other operator (``1 << 10`` is ``O_APPEND`` on Linux, as is
    ``~0``), attribute and call counts as append. A name is followed to the values its innermost
    binding scope in ``chain`` (the module, then the functions enclosing the call, then the
    lambdas around it) gives it: in the call's own function only those that may reach the read
    (``bindings``, a ``_Bindings``), elsewhere every value. A name that scope binds in a way the
    scan cannot read (a parameter, a ``match`` capture), or that no scope binds, counts as append.
    A false alarm only asks for a registry entry; a miss is an unbounded file."""
    if _is_o_append(expr):
        return True
    if isinstance(expr, ast.Constant):
        return not isinstance(expr.value, int) or bool(expr.value & _APPEND_BIT)
    if isinstance(expr, ast.Attribute):              # os.O_WRONLY and the like
        return not (isinstance(expr.value, ast.Name) and expr.value.id == "os"
                    and not _os_flag_may_append(expr.attr))
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, (ast.BitOr, ast.BitXor, ast.BitAnd)):
        sides = [_flags_may_append(side, chain, bindings, seen) for side in (expr.left, expr.right)]
        return all(sides) if isinstance(expr.op, ast.BitAnd) else any(sides)
    if isinstance(expr, (ast.BoolOp, ast.IfExp)):    # the value is one of the operands
        parts = expr.values if isinstance(expr, ast.BoolOp) else [expr.body, expr.orelse]
        return any([_flags_may_append(p, chain, bindings, seen) for p in parts])
    if isinstance(expr, ast.Call) and _getattr_os(expr):     # getattr(os, "O_NOFOLLOW", 0)
        name, default = _getattr_os(expr)
        if hasattr(os, name):                        # the attribute, not the default, is used
            return _os_flag_may_append(name)
        return default is None or _flags_may_append(default, chain, bindings, seen)
    if isinstance(expr, ast.Name):
        return _name_may_append(expr, chain, bindings, seen)
    return True


def _name_may_append(name, chain, bindings, seen) -> bool:
    """``_flags_may_append`` for a name: the innermost scope of ``chain`` that binds it decides
    (a lambda around the call first, so its parameter shadows the function's binding). In the
    call's own function (``bindings.scope``) the assignments that may reach this read are judged;
    in any other scope, every value it assigns."""
    for depth in range(len(chain), 0, -1):
        values = _bindings(chain[depth - 1], name.id)
        if values is None:
            return True
        if not values:
            continue
        if bindings is not None and chain[depth - 1] is bindings.scope:
            return _defs_may_append(bindings.reads.get(id(name), frozenset()), chain[:depth],
                                    bindings, seen)
        if name.id in seen:                          # flags |= ...: its other values are judged
            return False
        return any([_flags_may_append(v, chain[:depth], None, seen | {name.id})
                    for v in values])
    return True


def _defs_may_append(defs, chain, bindings, seen) -> bool:
    """May any of the local assignments ``defs`` (``_Bindings``) give ``O_APPEND`` flags? An
    ``x |= ...`` also brings the earlier value of ``x``; an assignment reached again (a loop) is
    judged once. No assignment reaching a name the scope binds counts as append."""
    if not defs:
        return True
    results = []
    for d in defs:
        if id(d) in seen:
            continue
        inner = seen | {id(d)}
        results.append(_flags_may_append(d.value, chain, bindings, inner))
        if isinstance(d, ast.AugAssign):
            results.append(_defs_may_append(bindings.reads.get(id(d), frozenset()), chain,
                                            bindings, inner))
    return any(results)


def _is_os_open(call: ast.Call) -> bool:
    func = call.func
    return (isinstance(func, ast.Attribute) and func.attr == "open"
            and isinstance(func.value, ast.Name) and func.value.id == "os")


def _lambdas_around(scope) -> dict:
    """id(node) -> the lambdas of ``scope``'s own body that enclose it, outermost first: each is
    a binding scope of its own for the names the node reads (#2386 review)."""
    around: dict = {}

    def walk(node, lambdas):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPES):
                continue
            inner = lambdas + [child] if isinstance(child, ast.Lambda) else lambdas
            around[id(child)] = inner
            walk(child, inner)

    walk(scope, [])
    return around


def _o_append_opens(scope, nodes: list, chain: list) -> int:
    """How many append ``os.open`` calls one scope's ``nodes`` hold (#2379, #2377): one per
    ``os.open`` whose flags may hold ``O_APPEND`` (``_flags_may_append``: spelled, through any
    assignment that may reach a variable they read, ``_Bindings``, or not provably append-free),
    so two opens sharing one flags variable count two, on every branch. A name is followed
    through the lambdas around the call, then outward through ``chain``. An ``os.open(*args)`` or
    ``(**kw)`` counts too."""
    bindings = _Bindings(scope)
    around = _lambdas_around(scope)
    count = 0
    for call in nodes:
        if not (isinstance(call, ast.Call) and _is_os_open(call)):
            continue
        flags = _os_open_flags(call)
        unreadable = (any(isinstance(a, ast.Starred) for a in call.args)
                      or any(k.arg is None for k in call.keywords))
        if unreadable or flags is not None and (
                bindings.holds(flags)
                or _flags_may_append(flags, chain + around.get(id(call), []), bindings)):
            count += 1
    return count


def append_sites(source: str, relative: str) -> collections.Counter:
    """How many append opens each ``relative::Qualified.function`` of ``source`` holds: an
    append-mode open, an ``os.open`` whose flags may hold ``O_APPEND`` and a call of
    ``util.append_keeping`` count one each, in the function that makes the call, so a second
    append in a registered function changes its count (#2302 review, #2379, #2377). A mention of
    ``O_APPEND`` that reaches no ``os.open`` is not an open (#2377). The module docstring says
    which forms are covered and which are not."""
    found: collections.Counter = collections.Counter()

    def visit(node, scope, chain):
        key = f"{relative}::{'.'.join(scope) or '<module>'}"
        nodes = list(_scope_nodes(node))
        count = sum(1 for n in nodes if isinstance(n, ast.Call) and _is_append_call(n))
        count += _o_append_opens(node, nodes, chain)
        if count:
            found[key] += count
        # A class body is not an enclosing scope for the names its methods read.
        outer = chain[:-1] if isinstance(node, ast.ClassDef) else chain
        for child in _nested_scopes(node):
            visit(child, scope + [child.name], outer + [child])

    tree = ast.parse(source)
    visit(tree, [], [tree])
    return found


def scan() -> collections.Counter:
    sites: collections.Counter = collections.Counter()
    for path in python_sources():
        sites.update(append_sites(path.read_text(errors="replace"), path.relative_to(ROOT).as_posix()))
    return sites


def registered() -> collections.Counter:
    """Every registered site with the number of append opens it is registered for."""
    return collections.Counter({key: OPENS.get(key, 1)
                                for key in (*APPENDS, *UNBOUNDED, *NOT_LOGS)})


class Guard(unittest.TestCase):
    def test_every_append_site_is_registered(self):
        expected, found = registered(), scan()
        self.assertEqual(sorted((found - expected).items()), [],
                         "an append open with no bound: bound it, then register it in APPENDS "
                         "with a PROOF test (or, with the reason, in UNBOUNDED or NOT_LOGS); a "
                         "second open in a registered function raises its OPENS count")
        self.assertEqual(sorted((expected - found).items()), [],
                         "registered append opens that no longer exist")

    def test_registries_do_not_overlap_and_every_bound_has_a_proof(self):
        self.assertFalse(set(APPENDS) & set(UNBOUNDED) or set(APPENDS) & set(NOT_LOGS)
                         or set(UNBOUNDED) & set(NOT_LOGS))
        self.assertLessEqual(set(OPENS), set(registered()))
        self.assertEqual(set(PROOF), set(APPENDS))
        for key, test_id in PROOF.items():
            module, cls, method = test_id.split(".")
            with self.subTest(key=key):
                self.assertTrue(callable(getattr(getattr(importlib.import_module(module), cls), method)),
                                test_id)

    def test_a_new_append_open_is_found(self):
        source = ("import io, os, codecs\n"
                  "def a(p):\n    open(p, 'a').write('x')\n"
                  "def b(p):\n    open(p, mode='ab')\n"
                  "class C:\n    def c(self, p):\n        p.open('a+')\n"
                  "def d(p):\n    os.open(p, os.O_WRONLY | os.O_APPEND)\n"
                  "def e(fd):\n    os.fdopen(fd, 'a')\n"
                  "def f(p):\n    util.append_keeping(p, 'x', 3, p)\n"
                  "def kw(p):\n    os.open(p, flags=os.O_WRONLY | os.O_APPEND | os.O_CREAT)\n"
                  "def var(p):\n    flags = os.O_WRONLY | O_APPEND\n    os.open(p, flags)\n"
                  "def by_name(p):\n    os.open(p, getattr(os, 'O_APPEND'))\n"
                  "def io_open(p):\n    io.open(p, 'a'); codecs.open(p, mode='a')\n"
                  "def unread(p, m):\n    open(p, m)\n"
                  "def path_unread(p, mode):\n    p.open(mode).write('x')\n"
                  "def path_kw_unread(p, mode):\n    p.open(mode=mode, encoding='utf-8')\n"
                  "def two(p):\n    open(p, 'a').write('x')\n    open(p, 'a').write('y')\n"
                  "def shared(p):\n    flags=os.O_WRONLY|os.O_APPEND|os.O_CREAT\n"
                  "    a=os.open(p,flags)\n    b=os.open(p+'x',flags)\n"
                  "FL=os.O_WRONLY|os.O_APPEND\ndef g(p):\n    os.open(p,FL)\n"
                  "def not_appends(p, opener, tarfile):\n    open(p); open(p, 'w'); p.open('rb'); "
                  "os.open(p, os.O_RDONLY); io.open(p, 'rb'); opener.open(p, timeout=30); "
                  "opener.open(p, data, 30, cafile=None); tarfile.open(p, 'r'); "
                  "tarfile.open(fileobj=p, mode='w:gz'); x.open(p, 'r:gz'); p.open(); "
                  "p.open(encoding='utf-8'); p.open('w')\n")
        self.assertEqual(dict(append_sites(source, "x.py")),
                         {"x.py::a": 1, "x.py::b": 1, "x.py::C.c": 1, "x.py::d": 1, "x.py::e": 1,
                          "x.py::f": 1, "x.py::kw": 1, "x.py::var": 1, "x.py::by_name": 1,
                          "x.py::io_open": 2, "x.py::unread": 1, "x.py::path_unread": 1,
                          "x.py::path_kw_unread": 1, "x.py::two": 2, "x.py::shared": 2,
                          "x.py::g": 1})

    def test_each_os_open_with_append_flags_is_its_own_site(self):
        """#2377: each ``os.open`` counts where it is called, however its flags were built. A bare
        ``O_APPEND`` mention that reaches no open is not an open (#2377, #2386 review F3)."""
        shared = ("def f(p):\n    flags=os.O_WRONLY|os.O_APPEND|os.O_CREAT\n"
                  "    a=os.open(p,flags)\n    b=os.open(p+'x',flags)\n")
        self.assertEqual(dict(append_sites(shared, "x.py")), {"x.py::f": 2})
        constant = "FL=os.O_WRONLY|os.O_APPEND\ndef g(p):\n    os.open(p,FL)\n"
        self.assertEqual(dict(append_sites(constant, "x.py")), {"x.py::g": 1})
        source = ("import os\nfrom m import IMPORTED\n"
                  "def grown(p):\n    flags = os.O_WRONLY\n    if p:\n        flags |= os.O_APPEND\n"
                  "    os.open(p, flags)\n"
                  "def param(p, flags):\n    os.open(p, flags)\n"
                  "def unbound(p):\n    os.open(p, NOWHERE)\n"
                  "def imported(p):\n    os.open(p, IMPORTED)\n"
                  "def called(p):\n    os.open(p, make_flags())\n"
                  "def outer(p):\n    fl = os.O_APPEND\n    def inner():\n        os.open(p, fl)\n"
                  "def starred(a):\n    os.open(*a)\n"
                  "def mention_only():\n    return os.O_APPEND\n"
                  "def not_appends(p):\n    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)\n"
                  "    os.open(p, flags, 0o600); os.open(p, os.O_RDONLY); os.open(p, flags=os.O_RDONLY)\n"
                  "    rd = os.O_RDONLY\n    os.open(p, rd | os.O_DIRECTORY)\n")
        self.assertEqual(dict(append_sites(source, "x.py")),
                         {"x.py::grown": 1, "x.py::param": 1, "x.py::unbound": 1,
                          "x.py::imported": 1, "x.py::called": 1, "x.py::outer.inner": 1,
                          "x.py::starred": 1})
        self.assertEqual(dict(append_sites("def f():\n    return os.O_APPEND\n", "x.py")), {})

    def test_flags_that_only_compute_the_append_bit_are_found(self):
        """#2386 review: flags count as append unless proven append-free. On the real
        ``_worker_log`` (registered for 2), each extra ``os.open`` below holds the append bit on
        Linux without spelling ``O_APPEND``, and each must fail the guard: a shift (F1), a lambda
        parameter shadowing an append-free local (F2), a non-flag ``os`` attribute equal to the
        bit (F4) and a ``match`` capture shadowing an append-free local (F5). Each is a real
        append here: the bit is checked against this host's ``os.O_APPEND``."""
        bit = getattr(os, "O_APPEND", 0)
        if bit != 1 << 10 or getattr(os, "ST_NOATIME", None) != bit:
            self.skipTest("the snippets use Linux's O_APPEND value (1024)")
        relative = "diaktoros/run_supervisor/workers.py"
        key = f"{relative}::WorkerLifecycle._worker_log"
        source = (ROOT / relative).read_text()
        anchor = '            return os.fdopen(os.open(path, flags, 0o600), "ab")\n'
        self.assertIn(anchor, source)
        pad = "            "
        extras = {
            "shift": f"{pad}os.close(os.open(path, os.O_WRONLY | os.O_CREAT | (1 << 10), 0o600))\n",
            "lambda": f"{pad}shadow = os.O_RDONLY\n"
                      f"{pad}(lambda shadow: os.close(os.open(path, shadow, 0o600)))"
                      "(os.O_WRONLY | os.O_CREAT | 1024)\n",
            "attribute": f"{pad}os.close(os.open(path, os.O_WRONLY | os.ST_NOATIME, 0o600))\n",
            "getattr": f"{pad}os.close(os.open(path, getattr(os, 'ST_NOATIME', 0), 0o600))\n",
            "match": f"{pad}shadow = os.O_RDONLY\n{pad}match 1024:\n{pad}    case shadow:\n"
                     f"{pad}        os.close(os.open(path, shadow | os.O_WRONLY, 0o600))\n",
            "invert": f"{pad}os.close(os.open(path, ~os.O_RDONLY & ~os.O_CREAT, 0o600))\n",
            "add": f"{pad}os.close(os.open(path, os.O_WRONLY + 1024, 0o600))\n",
        }
        for name, extra in extras.items():
            with self.subTest(name):
                grown = append_sites(source.replace(anchor, extra + anchor), relative)
                self.assertEqual(sorted((grown - registered()).items()), [(key, 1)])
        self.assertEqual(dict(append_sites(
            "def f(p, c):\n    fl = os.O_RDONLY\n    g = lambda fl: os.open(p, fl)\n"
            "    h = lambda q: os.open(q, fl)\n"
            "    os.open(p, (os.O_RDONLY | os.O_CREAT) & ~0 ^ 0 if c else 0)\n"
            "    os.open(p, os.O_RDWR & (1 << 10)); os.open(p, getattr(os, 'O_NOFOLLOW', 0))\n"
            "    os.open(p, getattr(os, 'O_NO_SUCH_FLAG', 0))\n", "x.py")),
            {"x.py::f": 1})

    def test_a_second_os_open_in_worker_log_fails_the_guard(self):
        """The review's case on the real tree: a second ``os.open(path, flags, 0o600)`` added to
        ``WorkerLifecycle._worker_log`` is an unregistered append open."""
        relative = "diaktoros/run_supervisor/workers.py"
        source = (ROOT / relative).read_text()
        anchor = '            return os.fdopen(os.open(path, flags, 0o600), "ab")\n'
        self.assertIn(anchor, source)
        grown = append_sites(source.replace(
            anchor, "            os.close(os.open(path, flags, 0o600))\n" + anchor), relative)
        key = f"{relative}::WorkerLifecycle._worker_log"
        self.assertEqual(sorted((grown - registered()).items()), [(key, 1)])
        self.assertEqual(append_sites(source, relative) - registered(), collections.Counter())

    def test_a_second_open_in_a_registered_function_fails_the_guard(self):
        """The review's case: ``corpus.record`` is registered for its one ``append_keeping``; an
        unbounded ``open(p, 'a')`` added beside it is an unregistered open, not the same site."""
        record = "diaktoros/corpus.py::record"
        grown = append_sites("def record(p):\n    util.append_keeping(p, 'x', 3, p)\n"
                             "    open(p, 'a').write('unbounded')\n", "diaktoros/corpus.py")
        self.assertEqual(sorted((grown - registered()).items()), [(record, 1)])
        found = append_sites("def record(p):\n    util.append_keeping(p, 'x', 3, p)\n",
                             "diaktoros/corpus.py")
        self.assertEqual(found - registered(), collections.Counter())

    def test_os_opens_sharing_one_flags_variable_count_each(self):
        """#2379: two ``os.open`` calls reusing one ``O_APPEND`` flags variable are two opens, so a
        second open added to ``WorkerLifecycle._worker_log`` (registered for 2) fails the guard."""
        self.assertEqual(dict(append_sites(
            "def f(path):\n    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT\n"
            "    fd = os.open(path, flags, 0o600)\n    extra = os.open(path, flags, 0o600)\n"
            "def g(path):\n    base = os.O_APPEND\n    flags = base | os.O_WRONLY\n    flags |= os.O_CREAT\n"
            "    os.open(path, flags=flags); os.open(path, flags); os.open(path, os.O_RDONLY)\n"
            "def h(path):\n    flags = os.O_WRONLY | os.O_APPEND\n    other = os.O_APPEND\n"
            "    os.open(path, flags)\n    helper(other)\n", "x.py")),
            {"x.py::f": 2, "x.py::g": 2, "x.py::h": 1})
        key = "diaktoros/run_supervisor/workers.py::WorkerLifecycle._worker_log"
        source = (ROOT / "diaktoros/run_supervisor/workers.py").read_text()
        grown = source.replace("            return os.fdopen(os.open(path, flags, 0o600), \"ab\")\n",
                               "            os.close(os.open(path, flags, 0o600))\n"
                               "            return os.fdopen(os.open(path, flags, 0o600), \"ab\")\n")
        self.assertNotEqual(grown, source, "the scratch edit no longer applies to _worker_log")
        found = append_sites(grown, "diaktoros/run_supervisor/workers.py")
        self.assertEqual(sorted((found - registered()).items()), [(key, 1)])

    def test_a_rebound_flags_variable_consumes_only_the_value_that_reaches_the_open(self):
        """#2379 review, under #2377's rule: an ``O_APPEND`` value handed to a helper is not an open
        where it is spelled (#2377); the helper's own ``os.open`` of its flags parameter counts.
        A later ``os.open`` of a rebound ``O_APPEND`` value counts; flags rebound to ``O_RDONLY``
        make a plain open. In ``_worker_log``, a helper defined beside it that opens the flags it
        is handed is an unregistered append site of its own."""
        before = "def f(p):\n    flags = os.O_APPEND\n    helper(flags)\n"
        after = before + "    flags = os.O_APPEND\n    os.open(p, flags)\n"
        helper = "def helper(p, fl):\n    os.open(p, fl)\n"
        self.assertEqual(dict(append_sites(before, "x.py")), {})
        self.assertEqual(dict(append_sites(after, "x.py")), {"x.py::f": 1})
        self.assertEqual(dict(append_sites(before + helper, "x.py")), {"x.py::helper": 1})
        self.assertEqual(dict(append_sites(
            "def r(p):\n    flags = os.O_APPEND\n    os.open(p, flags)\n"
            "    flags = os.O_RDONLY\n    os.open(p, flags)\n"
            "def s(p):\n    flags = os.O_WRONLY\n    flags = flags | os.O_APPEND\n    os.open(p, flags)\n"
            "def t(p):\n    flags = os.O_APPEND\n    flags |= os.O_CREAT\n    os.open(p, flags)\n", "x.py")),
            {"x.py::r": 1, "x.py::s": 1, "x.py::t": 1})
        key = "diaktoros/run_supervisor/workers.py::WorkerLifecycle._helper"
        source = (ROOT / "diaktoros/run_supervisor/workers.py").read_text()
        anchor = "    def _worker_log(self):\n"
        grown = source.replace(anchor, "    def _helper(self, path, fl):\n"
                                       "        return os.open(path, fl, 0o600)\n\n" + anchor)
        self.assertNotEqual(grown, source, "the scratch edit no longer applies to _worker_log")
        found = append_sites(grown, "diaktoros/run_supervisor/workers.py")
        self.assertEqual(sorted((found - registered()).items()), [(key, 1)])

    def test_a_flags_value_on_any_path_reaches_the_open(self):
        """#2379 review: a non-append rebinding on one branch (``if``, ``try``, a loop that may
        ``break`` or run again) does not hide the ``O_APPEND`` value on the other path, so every
        ``os.open`` that may append counts; flags that are ``O_RDONLY`` on every path do not. In
        ``_worker_log``, a conditional ``O_RDONLY`` rebinding plus a second ``os.open`` fails
        the guard."""
        branch = "def f(p, c):\n    flags = os.O_APPEND\n    if c:\n        flags = os.O_RDONLY\n"
        self.assertEqual(dict(append_sites(branch + "    os.open(p, flags)\n", "x.py")), {"x.py::f": 1})
        self.assertEqual(dict(append_sites(branch + "    os.open(p, flags)\n    os.open(p, flags)\n",
                                           "x.py")), {"x.py::f": 2})
        self.assertEqual(dict(append_sites(
            "def t(p):\n    flags = os.O_APPEND\n    try:\n        flags = os.O_RDONLY\n        g()\n"
            "    except E:\n        pass\n    os.open(p, flags)\n    os.open(p, flags)\n"
            "def b(p, xs):\n    for x in xs:\n        flags = os.O_APPEND\n        if x:\n            break\n"
            "        flags = os.O_RDONLY\n    os.open(p, flags)\n    os.open(p, flags)\n"
            "def w(p, xs):\n    flags = os.O_RDONLY\n    for x in xs:\n        os.open(p, flags)\n"
            "        os.open(p, flags)\n        flags = os.O_APPEND\n"
            "def ro(p, c):\n    flags = os.O_RDONLY\n    if c:\n        flags = os.O_WRONLY\n"
            "    os.open(p, flags)\n    os.open(p, flags)\n", "x.py")),
            {"x.py::t": 2, "x.py::b": 2, "x.py::w": 2})
        key = "diaktoros/run_supervisor/workers.py::WorkerLifecycle._worker_log"
        source = (ROOT / "diaktoros/run_supervisor/workers.py").read_text()
        grown = source.replace("            return os.fdopen(os.open(path, flags, 0o600), \"ab\")\n",
                               "            if path.name.endswith(\".ro\"):\n"
                               "                flags = os.O_RDONLY\n"
                               "            os.close(os.open(path, flags, 0o600))\n"
                               "            return os.fdopen(os.open(path, flags, 0o600), \"ab\")\n")
        self.assertNotEqual(grown, source, "the scratch edit no longer applies to _worker_log")
        found = append_sites(grown, "diaktoros/run_supervisor/workers.py")
        self.assertEqual(sorted((found - registered()).items()), [(key, 1)])


class Bounds(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.loop = {"id": "bounds", "repo": "acme/widgets", "base": "main", "cap": 3,
                     "inflight_ttl_min": 10, "ttl_min": 45, "turn_budget_s": 900,
                     "state_dir": str(self.root / "state"), "fixers": ["f"], "reviewers": ["r"],
                     "seats": {"reviewer": {}, "fixer": {}}}

    def test_watchdog_log_is_pruned(self):
        st = state.LoopState(self.loop)
        for i in range(state.LOG_PRUNE_BYTES // state.LOG_LINE_MAX + 20):
            st.note(f"sweep {i} " + "x" * state.LOG_LINE_MAX)
        self.assertLessEqual(st.log.stat().st_size, state.LOG_PRUNE_BYTES + state.LOG_LINE_MAX + 100)

    def test_gate_decisions_are_pruned(self):
        for i in range(gate_decisions.KEEP + 5):
            gate_decisions.record(self.loop, "gate_reviewer", {"number": 7}, "declined", f"r{i}")
        entries = gate_decisions.read(self.loop)
        self.assertEqual(len(entries), gate_decisions.KEEP)
        self.assertEqual(entries[-1]["reason"], f"r{gate_decisions.KEEP + 4}")

    def test_corpus_scores_are_pruned(self):
        path = self.root / "state" / corpus.SCORES_FILE
        for i in range(corpus.SCORES_KEEP + 3):
            corpus.record(path, f"rev{i}", "m", [], now=i)
        runs = corpus.history(path)
        self.assertEqual(len(runs), corpus.SCORES_KEEP)
        self.assertEqual((runs[0]["prompt_rev"], runs[-1]["prompt_rev"]),
                         ("rev3", f"rev{corpus.SCORES_KEEP + 2}"))
        self.assertTrue((path.parent / corpus.SCORES_LOCK).is_file())

    def test_append_keeping_prunes(self):
        from diaktoros import util
        path, lock = self.root / "f.log", self.root / "f.lock"
        for i in range(7):
            util.append_keeping(path, f"{i}\n", 3, lock)
        self.assertEqual(path.read_text(), "4\n5\n6\n")


if __name__ == "__main__":
    unittest.main()
