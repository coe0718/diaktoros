"""Duplication audit (B6): every file the plugin publishes goes through ``util.atomic_write``.

About a dozen writers repeated temp file, fsync, replace, directory fsync by hand, and some
skipped steps: the loop-config writer and the gate shims never synced the directory, the runtime
file and the pacing state were never fsynced and used one fixed ``.tmp`` name, so two writers
could publish each other's half-written file. Now the rule has one home; the writers that differ
on purpose (the route registry's own durability error, a streamed restore, migration renames, log
rotation, a cache directory swap) are named, function by function, in ``DIFFERENT``.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
from collections import Counter
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diaktoros import util  # noqa: E402
import _sources  # noqa: E402  every *.py the scans read (#1489)

ROOT = Path(__file__).resolve().parents[1]
# The renames that publish on their own, each for a reason the helper does not cover: the exact
# operations, counted, in a function named by its qualified name. Another rename in the same file,
# in the same function or in a second function of that name is not excused (#1473 review).
DIFFERENT = {
    "diaktoros/util.py::atomic_write": (
        "the helper itself", {"tempfile.mkstemp": 1, "os.replace": 1}),
    "diaktoros/routes.py::_write_registry": (
        "RegistryDurabilityError: published, directory unsynced",
        {"tempfile.mkstemp": 1, "os.replace": 1}),
    "diaktoros/backup.py::_put": ("a restore streams a member to its target", {"os.replace": 1}),
    "diaktoros/migrate.py::move_state_dir": (
        "renames a loop's state directory; nothing is written", {"os.rename": 1}),
    "diaktoros/migrate.py::_move_ledger_file": (
        "renames a ledger file and its sidecar; nothing is written", {"os.rename": 2}),
    "diaktoros/migrate.py::files_step": ("renames a loop's files; nothing is written",
                                         {"os.rename": 1}),
    "diaktoros/run_supervisor/workers.py::WorkerLifecycle._worker_log": (
        "rotates a worker log", {"os.replace": 1}),
    "diaktoros/deps.py::prefetch_rust": ("retires and restores a dependency-cache directory",
                                         {"cache.rename": 1, "retired.rename": 1}),
}
# The writers that publish through the helper; a writer that stops calling it fails here.
CALLERS = {
    "diaktoros/app_manifest.py::save_session", "diaktoros/app_tokens.py::save",
    "diaktoros/ci_cache.py::save", "diaktoros/cli/common.py::_write_config_locked",
    "diaktoros/cli/common.py::_restore_config_locked", "diaktoros/gate_decisions.py::record",
    "diaktoros/gate_shims.py::_write", "diaktoros/gate_shims.py::heal_watchdog_shim",
    "diaktoros/gh_cache.py::store", "diaktoros/gh_calls.py::flush",
    "diaktoros/observer.py::_save", "diaktoros/onboarding.py::record", "diaktoros/pacing.py::_save",
    "diaktoros/route_intent.py::_write", "diaktoros/run_supervisor/__init__.py::Supervisor.__init__",
    "diaktoros/runtime_detect.py::write", "diaktoros/state.py::_atomic_publish",
    "diaktoros/state.py::LoopState._transition_write",
    "docs/site/publish-history.py::_write",
}
BY_HAND = {"os.replace", "os.rename", "tempfile.mkstemp", "tempfile.NamedTemporaryFile",
           "shutil.move"}
# ``Path.replace(tmp, target)`` / ``Path.rename(tmp, target)``: the unbound two-argument form.
PATH_CLASS = {"pathlib.Path", "Path"}


def sources(root: Path) -> list[Path]:
    """The Python the scans read: every ``*.py`` but the suite's (``_sources``, #1489)."""
    return _sources.python_sources(root)


def calls(source: str):
    """Each call in ``source`` as ``(qualified name of its function, line, call node)``."""
    found = []

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute | ast.Name):
                found.append((scope, child.lineno, child))
            inner = isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            visit(child, f"{scope}.{child.name}".lstrip(".") if inner else scope)

    visit(ast.parse(source), "")
    return found


def aliases(tree: ast.AST) -> dict[str, str]:
    """Each name the module's imports bind, anywhere in it, mapped to what it names.

    ``import os as o`` binds ``o`` to ``os``; ``from os import replace as r`` binds ``r`` to
    ``os.replace``. A plain ``import os`` binds ``os`` to itself.
    """
    bound = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bound[alias.asname] = alias.name
                else:
                    head = alias.name.split(".")[0]
                    bound[head] = head
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                bound[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return bound


def resolved(func: ast.expr, bound: dict[str, str]) -> str:
    """The call target ``func`` spelled through the module's import aliases."""
    named = ast.unparse(func)
    head, dot, rest = named.partition(".")
    return bound.get(head, head) + dot + rest


def renames(source: str) -> dict[str, Counter]:
    """Each function's rename-publish operations, counted, by qualified name."""
    found = {}
    bound = aliases(ast.parse(source))
    for func, _, node in calls(source):
        named = resolved(node.func, bound)
        is_move = isinstance(node.func, ast.Attribute) and node.func.attr in ("replace", "rename")
        # Path.replace / Path.rename onto another path (str.replace takes two args), or the
        # unbound ``Path.replace(tmp, target)``.
        path_move = is_move and (len(node.args) == 1 or (
            len(node.args) == 2 and named.rpartition(".")[0] in PATH_CLASS))
        if named in BY_HAND or path_move:
            found.setdefault(func, Counter())[named] += 1
    return found


def by_hand(rel: str, source: str) -> list[str]:
    """The rename-publishes in ``source`` beyond the operations ``DIFFERENT`` excuses."""
    out = []
    for func, ops in renames(source).items():
        excused = Counter(DIFFERENT.get(f"{rel}::{func}", ("", {}))[1])
        out += [f"{rel} {named} ×{extra} in {func or '<module>'}"
                for named, extra in (ops - excused).items()]
    return out


class Helper(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.path = self.dir / "state.json"

    def leftovers(self):
        return sorted(p.name for p in self.dir.iterdir() if p != self.path)

    def test_publishes_private_then_syncs_the_file_and_then_its_directory(self):
        real, seen = os.fsync, []

        def spy(fd):
            seen.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            return real(fd)

        with mock.patch.object(util.os, "fsync", spy):
            util.atomic_write(self.path, "text")
        self.assertEqual(seen, ["file", "dir"])
        self.assertEqual(self.path.read_text(), "text")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        util.atomic_write(self.path, b"bytes", mode=0o755)
        self.assertEqual((self.path.read_bytes(), stat.S_IMODE(self.path.stat().st_mode)),
                         (b"bytes", 0o755))
        self.assertEqual(self.leftovers(), [])

    def test_a_failed_check_or_write_publishes_nothing_and_leaves_no_temp_file(self):
        self.path.write_text("before")

        def refuse():
            raise OSError("changed meanwhile")

        with self.assertRaises(OSError):
            util.atomic_write(self.path, "after", before_replace=refuse)
        with mock.patch.object(util.os, "write", return_value=0), self.assertRaises(OSError):
            util.atomic_write(self.path, "after")              # a write that makes no progress
        self.assertEqual(self.path.read_text(), "before")
        self.assertEqual(self.leftovers(), [])

    def test_two_writers_never_share_a_temp_file(self):
        names = []
        real = tempfile.mkstemp

        def spy(*args, **kwargs):
            fd, name = real(*args, **kwargs)
            names.append(name)
            return fd, name

        with mock.patch.object(tempfile, "mkstemp", spy):
            util.atomic_write(self.path, "one")
            util.atomic_write(self.path, "two")
        self.assertEqual(len(set(names)), 2)


class OneHome(unittest.TestCase):
    def test_no_writer_publishes_by_rename_outside_the_helper(self):
        for path in sources(ROOT):
            rel = path.relative_to(ROOT).as_posix()
            with self.subTest(rel):
                self.assertEqual(by_hand(rel, path.read_text()), [])

    def test_an_exception_excuses_its_operations_only_and_nested_scripts_are_scanned(self):
        # #1473 review: an excused file, then an excused function, passed any further rename.
        rel, rogue = "diaktoros/routes.py", "os.replace(a, b)"
        routes = (ROOT / rel).read_text()
        func = next(node for node in ast.walk(ast.parse(routes))
                    if isinstance(node, ast.FunctionDef) and node.name == "_write_registry")
        lines = routes.splitlines(keepends=True)
        first = func.body[0]
        inside = "".join(lines[:first.lineno - 1]) + " " * first.col_offset + rogue + "\n" \
            + "".join(lines[first.lineno - 1:])
        for case, source in (("another function", f"{routes}\n\ndef other(a, b):\n    {rogue}\n"),
                             ("the excused function", inside),
                             ("a second function of its name",
                              f"{routes}\n\ndef _write_registry(a, b):\n    {rogue}\n")):
            with self.subTest(case):
                self.assertEqual(len(by_hand(rel, source)), 1, by_hand(rel, source))
        with tempfile.TemporaryDirectory() as temp:
            nested = Path(temp) / "scripts" / "nested" / "rogue.py"
            nested.parent.mkdir(parents=True)
            nested.write_text(rogue)
            self.assertEqual(sources(Path(temp)), [nested])

    def test_aliased_and_from_imports_shutil_move_and_unbound_path_replace_are_caught(self):
        # #1490: the scan matched literal spellings only, so any of these bypassed it.
        spellings = {
            "from os import replace": ("from os import replace\n", "replace(a, b)"),
            "from os import rename as r": ("from os import rename as r\n", "r(a, b)"),
            "import os as o": ("import os as o\n", "o.replace(a, b)"),
            "from tempfile import mkstemp": ("from tempfile import mkstemp\n", "mkstemp()"),
            "import tempfile as tf": ("import tempfile as tf\n", "tf.NamedTemporaryFile()"),
            "shutil.move": ("import shutil\n", "shutil.move(a, b)"),
            "from shutil import move": ("from shutil import move\n", "move(a, b)"),
            "Path.replace(tmp, target)": ("from pathlib import Path\n", "Path.replace(a, b)"),
            "pathlib.Path.rename(tmp, target)": ("import pathlib\n",
                                                 "pathlib.Path.rename(a, b)"),
        }
        for case, (imports, rogue) in spellings.items():
            with self.subTest(case):
                source = f"{imports}\n\ndef rogue(a, b):\n    {rogue}\n"
                self.assertEqual(len(by_hand("diaktoros/rogue.py", source)), 1, source)
        # str.replace and other two-argument replaces stay out.
        self.assertEqual(by_hand("diaktoros/x.py", "import os\n\ndef f(s):\n"
                                 "    return s.replace('a', 'b')\n"), [])

    def test_the_scan_reads_every_python_file_but_the_suite(self):
        # #1489: the scan walked diaktoros/ and scripts/ only, so these passed it.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for rel in ("dashboard/plugin_api.py", "tools/spike.py", "docs/site/build.py",
                        "tests/test_x.py"):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text("import os\nos.replace('a', 'b')\n")
            found = [line for path in sources(root)
                     for line in by_hand(path.relative_to(root).as_posix(), path.read_text())]
            self.assertEqual(found, [f"{rel} os.replace ×1 in <module>" for rel in
                                     ("dashboard/plugin_api.py", "docs/site/build.py",
                                      "tools/spike.py")])

    def test_every_exception_is_exactly_the_operations_its_function_has(self):
        for key, (why, ops) in DIFFERENT.items():
            rel, func = key.split("::")
            with self.subTest(key):
                self.assertTrue(why)
                self.assertEqual(renames((ROOT / rel).read_text()).get(func), Counter(ops))


class Callers(unittest.TestCase):
    def test_the_writers_that_publish_are_the_ones_that_call_the_helper(self):
        found = set()
        for path in sources(ROOT):
            rel = path.relative_to(ROOT).as_posix()
            found |= {f"{rel}::{func}" for func, _, node in calls(path.read_text())
                      if ast.unparse(node.func).split(".")[-1] == "atomic_write"}
        self.assertEqual(found, CALLERS)

    def test_runtime_and_pacing_files_sync_and_never_share_a_temp_name(self):
        # Both used one fixed ``<file>.tmp`` with no fsync: two writers could publish each
        # other's half-written file (#1472).
        from diaktoros import pacing, runtime_detect
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "state" / "file.json"
            target.parent.mkdir()
            writers = {"runtime": (runtime_detect, lambda n: runtime_detect.write({"n": n})),
                       "pacing": (pacing, lambda n: pacing._save(target, {"n": n}))}
            for name, (module, write) in writers.items():
                real_fsync, real_mkstemp, synced, temps = os.fsync, tempfile.mkstemp, [], []

                def fsync(fd):
                    synced.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
                    return real_fsync(fd)

                def mkstemp(*args, **kwargs):
                    fd, temp_name = real_mkstemp(*args, **kwargs)
                    temps.append(Path(temp_name))
                    return fd, temp_name

                with self.subTest(name), mock.patch.object(runtime_detect, "path", return_value=target), \
                        mock.patch.object(util.os, "fsync", fsync), \
                        mock.patch.object(tempfile, "mkstemp", mkstemp):
                    write(1)
                    write(2)
                    self.assertEqual(synced, ["file", "dir"] * 2)
                    self.assertEqual(len(set(temps)), 2)
                    self.assertEqual({t.parent for t in temps}, {target.parent})
                    self.assertEqual(json.loads(target.read_text()), {"n": 2})
                    self.assertEqual(sorted(p.name for p in target.parent.iterdir()), [target.name])


if __name__ == "__main__":
    unittest.main()
