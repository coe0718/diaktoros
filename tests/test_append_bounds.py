"""#2302: every file the plugin appends to has a bound, or is registered as deliberately unbounded.

#549 was a file the plugin appended to forever. This guard scans every Python source outside the
suite (``_sources.python_sources``), by AST, for an append open: ``open(..., "a"...)``,
``<path>.open("a"...)``, ``os.fdopen(..., "a"...)``, ``os.open(..., ... O_APPEND ...)`` and a call
of the shared append-and-prune ``util.append_keeping``. Each
site, keyed ``path::function``, must be in exactly one registry:

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
        "<ledger>.workers.log: the host rotates it once to .1 past WORKER_LOG_MAX; a worker "
        "appends only while waits.within_cap holds",
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
        "test_run_supervisor.Lifecycle.test_worker_stderr_goes_to_a_bounded_log_beside_the_ledger",
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
    "diaktoros/broker.py::_audit": _AUDIT,
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


def _mode_is_append(node) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and "a" in node.value


def _is_append_open(call: ast.Call) -> bool:
    """Does ``call`` open a file for appending, or append through ``util.append_keeping``? Each
    caller of that shared helper is its own site: its own file, with its own ``keep``."""
    func = call.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
    if name == "append_keeping":
        return True
    on_os = isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "os"
    if name == "open" and on_os:
        return any(isinstance(n, (ast.Name, ast.Attribute))
                   and (getattr(n, "id", None) == "O_APPEND" or getattr(n, "attr", None) == "O_APPEND")
                   for arg in call.args[1:2] for n in ast.walk(arg))
    if name not in ("open", "fdopen"):
        return False
    # builtin open(path, mode) and os.fdopen(fd, mode): the mode is second; <path>.open(mode): first.
    position = 0 if name == "open" and isinstance(func, ast.Attribute) else 1
    modes = [kw.value for kw in call.keywords if kw.arg == "mode"]
    modes += call.args[position:position + 1]
    return any(_mode_is_append(mode) for mode in modes)


def append_sites(source: str, relative: str) -> set[str]:
    """``relative::Qualified.function`` of every append open in ``source``."""
    found: set[str] = set()

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, scope + [child.name])
                continue
            if isinstance(child, ast.Call) and _is_append_open(child):
                found.add(f"{relative}::{'.'.join(scope) or '<module>'}")
            visit(child, scope)

    visit(ast.parse(source), [])
    return found


def scan() -> set[str]:
    sites: set[str] = set()
    for path in python_sources():
        sites |= append_sites(path.read_text(errors="replace"), path.relative_to(ROOT).as_posix())
    return sites


class Guard(unittest.TestCase):
    def test_every_append_site_is_registered(self):
        registered = set(APPENDS) | set(UNBOUNDED) | set(NOT_LOGS)
        found = scan()
        self.assertEqual(sorted(found - registered), [],
                         "an append open with no bound: bound it, then register it in APPENDS "
                         "with a PROOF test (or, with the reason, in UNBOUNDED or NOT_LOGS)")
        self.assertEqual(sorted(registered - found), [], "registered sites that no longer exist")

    def test_registries_do_not_overlap_and_every_bound_has_a_proof(self):
        self.assertFalse(set(APPENDS) & set(UNBOUNDED) or set(APPENDS) & set(NOT_LOGS)
                         or set(UNBOUNDED) & set(NOT_LOGS))
        self.assertEqual(set(PROOF), set(APPENDS))
        for key, test_id in PROOF.items():
            module, cls, method = test_id.split(".")
            with self.subTest(key=key):
                self.assertTrue(callable(getattr(getattr(importlib.import_module(module), cls), method)),
                                test_id)

    def test_a_new_append_open_is_found(self):
        source = ("import os\n"
                  "def a(p):\n    open(p, 'a').write('x')\n"
                  "def b(p):\n    open(p, mode='ab')\n"
                  "class C:\n    def c(self, p):\n        p.open('a+')\n"
                  "def d(p):\n    os.open(p, os.O_WRONLY | os.O_APPEND)\n"
                  "def e(fd):\n    os.fdopen(fd, 'a')\n"
                  "def f(p):\n    util.append_keeping(p, 'x', 3, p)\n"
                  "def not_appends(p):\n    open(p); open(p, 'w'); p.open('rb'); os.open(p, os.O_RDONLY)\n")
        self.assertEqual(append_sites(source, "x.py"),
                         {"x.py::a", "x.py::b", "x.py::C.c", "x.py::d", "x.py::e", "x.py::f"})


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
