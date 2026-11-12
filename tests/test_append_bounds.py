"""#2302: every file the plugin appends to has a bound, or is registered as deliberately unbounded.

#549 was a file the plugin appended to forever. This guard scans every Python source outside the
suite (``_sources.python_sources``), by AST, for an append open: ``open(..., "a"...)`` (also
``io.open``, ``codecs.open`` and the like, and a mode the scan cannot read, such as a variable),
``<path>.open("a"...)``, ``os.fdopen(..., "a"...)``, any mention of ``O_APPEND`` (so flags passed
by keyword or built in a variable are found) and a call of the shared append-and-prune
``util.append_keeping``. Opens are counted, not only the functions that hold them: each
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
# ``O_APPEND`` flag and the ``os.fdopen(.., "a")`` that wraps the same descriptor count one
# each). Another open added to the function changes its count, so the guard fails on it.
OPENS = {
    "diaktoros/run_supervisor/waits.py::note": 2,              # os.open(O_APPEND), os.fdopen("a")
    "diaktoros/run_supervisor/workers.py::WorkerLifecycle._worker_log": 2,   # the same pair
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
    """A mention of ``O_APPEND``: ``os.O_APPEND``, a bare ``O_APPEND`` or ``getattr(os, "O_APPEND")``.
    Each counts wherever it is, so flags passed by keyword or built in a variable are found."""
    return (isinstance(node, ast.Name) and node.id == "O_APPEND"
            or isinstance(node, ast.Attribute) and node.attr == "O_APPEND"
            or isinstance(node, ast.Constant) and node.value == "O_APPEND")


def append_sites(source: str, relative: str) -> collections.Counter:
    """How many append opens each ``relative::Qualified.function`` of ``source`` holds: an
    append-mode open, an ``O_APPEND`` and a call of ``util.append_keeping`` count one each, so a
    second append in a registered function changes its count (#2302 review)."""
    found: collections.Counter = collections.Counter()

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, scope + [child.name])
                continue
            if isinstance(child, ast.Call) and _is_append_call(child) or _is_o_append(child):
                found[f"{relative}::{'.'.join(scope) or '<module>'}"] += 1
            visit(child, scope)

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
