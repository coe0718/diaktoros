"""#2302: every file the plugin appends to has a bound, or is registered as deliberately unbounded.

#549 was a file the plugin appended to forever. This guard scans every Python source outside the
suite (``_sources.python_sources``), by AST, for an append open: ``open(..., "a"...)`` (also
``io.open``, ``codecs.open`` and the like, and a mode the scan cannot read, such as a variable),
``<path>.open("a"...)``, ``os.fdopen(..., "a"...)``, an ``os.open`` whose flags hold ``O_APPEND``
and a call of the shared append-and-prune ``util.append_keeping``. Opens are counted, not only the
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
  open; the read errs toward counting more, never fewer (``_Bindings``). An ``O_APPEND`` that
  reaches no counted ``os.open`` (flags passed to some other helper, a default argument, a value
  rebound on every path before the open) counts one itself.
* Not covered: an opener reached through an alias (``o = open; o(p, "a")``, ``from os import
  open``), flags built in another function or module and passed in, or a mode or flags value
  produced at run time from strings. Those need a reviewer's eye.
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
    ``os.open`` is not judged here: its ``O_APPEND``, wherever it is spelled, is (``_is_o_append``)."""
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

    def consumed(self, exprs: list) -> list:
        """The values of ``exprs`` and of every assignment that may reach them, in a chain."""
        out, todo, seen = [], list(exprs), set()
        while todo:
            expr = todo.pop()
            if id(expr) not in seen:
                seen.add(id(expr))
                out.append(expr.value if isinstance(expr, _DEFS) else expr)
                todo.extend(self.deps(expr))
        return out


def _o_append_opens(scope, nodes: list) -> int:
    """How many ``O_APPEND`` opens one scope's ``nodes`` hold (#2379): one per ``os.open`` whose
    flags hold ``O_APPEND``, inline or through any assignment that may reach a variable they read
    (``_Bindings``), so two opens sharing one flags variable count two, on every branch; plus one
    per ``O_APPEND`` mention that reaches no such ``os.open``, so a value passed elsewhere, or
    rebound on every path before the open, still counts."""
    bindings = _Bindings(scope)
    flags = [_os_open_flags(n) for n in nodes if isinstance(n, ast.Call)]
    counted = [f for f in flags if f is not None and bindings.holds(f)]
    consumed = {id(n) for value in bindings.consumed(counted) for n in _own(value) if _is_o_append(n)}
    return len(counted) + sum(1 for n in nodes if _is_o_append(n) and id(n) not in consumed)


def append_sites(source: str, relative: str) -> collections.Counter:
    """How many append opens each ``relative::Qualified.function`` of ``source`` holds: an
    append-mode open, an ``os.open`` whose flags hold ``O_APPEND``, an ``O_APPEND`` no such open
    consumes and a call of ``util.append_keeping`` count one each, so a second append in a
    registered function changes its count (#2302 review, #2379). The module docstring says which
    forms are covered and which are not."""
    found: collections.Counter = collections.Counter()

    def visit(node, scope):
        nodes = list(_scope_nodes(node))
        count = sum(1 for n in nodes if isinstance(n, ast.Call) and _is_append_call(n))
        count += _o_append_opens(node, nodes)
        if count:
            found[f"{relative}::{'.'.join(scope) or '<module>'}"] += count
        for child in _nested_scopes(node):
            visit(child, scope + [child.name])

    visit(ast.parse(source), [])
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
                  "def not_appends(p, opener, tarfile):\n    open(p); open(p, 'w'); p.open('rb'); "
                  "os.open(p, os.O_RDONLY); io.open(p, 'rb'); opener.open(p, timeout=30); "
                  "opener.open(p, data, 30, cafile=None); tarfile.open(p, 'r'); "
                  "tarfile.open(fileobj=p, mode='w:gz'); x.open(p, 'r:gz'); p.open(); "
                  "p.open(encoding='utf-8'); p.open('w')\n")
        self.assertEqual(dict(append_sites(source, "x.py")),
                         {"x.py::a": 1, "x.py::b": 1, "x.py::C.c": 1, "x.py::d": 1, "x.py::e": 1,
                          "x.py::f": 1, "x.py::kw": 1, "x.py::var": 1, "x.py::by_name": 1,
                          "x.py::io_open": 2, "x.py::unread": 1, "x.py::path_unread": 1,
                          "x.py::path_kw_unread": 1, "x.py::two": 2})

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
            {"x.py::f": 2, "x.py::g": 2, "x.py::h": 2})
        key = "diaktoros/run_supervisor/workers.py::WorkerLifecycle._worker_log"
        source = (ROOT / "diaktoros/run_supervisor/workers.py").read_text()
        grown = source.replace("            return os.fdopen(os.open(path, flags, 0o600), \"ab\")\n",
                               "            os.close(os.open(path, flags, 0o600))\n"
                               "            return os.fdopen(os.open(path, flags, 0o600), \"ab\")\n")
        self.assertNotEqual(grown, source, "the scratch edit no longer applies to _worker_log")
        found = append_sites(grown, "diaktoros/run_supervisor/workers.py")
        self.assertEqual(sorted((found - registered()).items()), [(key, 1)])

    def test_a_rebound_flags_variable_consumes_only_the_value_that_reaches_the_open(self):
        """#2379 review: an ``O_APPEND`` value passed to a helper, then rebound, is not consumed
        by a later ``os.open`` of the new value, so adding that open to a function registered for
        one site fails the guard; and flags rebound to ``O_RDONLY`` make a plain open. In
        ``_worker_log`` the first flags, handed to a helper and rebound before the open, become
        an append site of their own beside the open and its ``os.fdopen``."""
        before = "def f(p):\n    flags = os.O_APPEND\n    helper(flags)\n"
        after = before + "    flags = os.O_APPEND\n    os.open(p, flags)\n"
        self.assertEqual(dict(append_sites(before, "x.py")), {"x.py::f": 1})
        self.assertEqual(dict(append_sites(after, "x.py")), {"x.py::f": 2})
        self.assertEqual(dict(append_sites(
            "def r(p):\n    flags = os.O_APPEND\n    os.open(p, flags)\n"
            "    flags = os.O_RDONLY\n    os.open(p, flags)\n"
            "def s(p):\n    flags = os.O_WRONLY\n    flags = flags | os.O_APPEND\n    os.open(p, flags)\n"
            "def t(p):\n    flags = os.O_APPEND\n    flags |= os.O_CREAT\n    os.open(p, flags)\n", "x.py")),
            {"x.py::r": 1, "x.py::s": 1, "x.py::t": 1})
        key = "diaktoros/run_supervisor/workers.py::WorkerLifecycle._worker_log"
        source = (ROOT / "diaktoros/run_supervisor/workers.py").read_text()
        grown = source.replace("            return os.fdopen(os.open(path, flags, 0o600), \"ab\")\n",
                               "            helper(flags)\n"
                               "            flags = os.O_WRONLY | os.O_APPEND\n"
                               "            return os.fdopen(os.open(path, flags, 0o600), \"ab\")\n")
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
