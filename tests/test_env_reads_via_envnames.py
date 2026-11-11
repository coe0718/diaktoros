"""Every ``DIAKTOROS_``/``REVIEW_LOOP_`` environment read goes through ``envnames`` (#2300).

#542 was a variable read by its new name only, which missed installs still on the old
``REVIEW_LOOP_`` spelling. ``diaktoros/envnames.py`` reads both, so a direct
``os.environ.get(...)``, ``os.environ[...]`` (a read or an augmented assignment) or
``os.getenv(...)``, positional or by keyword, whose key is a literal with either prefix, an
``envnames.name(...)`` value, or a name bound to one (``X_ENV = envnames.name("X")``, also read
as ``module.X_ENV``), fails this scan anywhere in the repository's own Python (``_sources``:
``diaktoros/``, ``scripts/`` and the rest, #1489) but ``envnames.py`` itself. Python source held
in a string constant or an f-string (a script run in a child process) is scanned too; a fragment
that does not parse is searched by pattern rather than dropped. ``ALLOWED`` names the few reads
that must stay direct, each with its reason, by file, variable and exact count, and only inside
a generated script: an ordinary read of the same name in the same file still fails.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import os
from pathlib import Path
import re
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _sources  # noqa: E402  every *.py the scans read (#1489)

PREFIXES = ("DIAKTOROS_", "REVIEW_LOOP_")
HOME = Path("diaktoros/envnames.py")
SCRIPT, CODE = "generated script", "code"

# (file, variable) -> (reads, why the read stays direct). Each is inside a script generated as a
# string and run by a bare interpreter in a child process, which cannot import ``envnames``.
# Only reads of kind SCRIPT are allowed, and only exactly this many.
ALLOWED = {
    ("diaktoros/trace.py", "DIAKTOROS_GH_STUB"): (
        1, "_HARNESS runs in a child process (python -c); it reads both spellings by hand"),
    ("diaktoros/trace.py", "REVIEW_LOOP_GH_STUB"): (
        1, "_HARNESS runs in a child process (python -c); it reads both spellings by hand"),
    ("diaktoros/safe_push.py", "DIAKTOROS_TOKEN_FILE"): (
        1, "_ASKPASS is Git's credential prompt, a child-process script; safe_push itself sets "
           "this one spelling in that child's environment, so there is no old name to miss"),
}

_FALLBACK = re.compile(r"""(?:environ\s*(?:\.get\s*\(|\[)|getenv\s*\()\s*(?:key\s*=\s*)?"""
                       r"""['"]((?:DIAKTOROS_|REVIEW_LOOP_)\w*)['"]""")


def _is_environ(node) -> bool:
    """``os.environ`` (or a bare ``environ``)."""
    return ((isinstance(node, ast.Attribute) and node.attr == "environ"
             and isinstance(node.value, ast.Name) and node.value.id == "os")
            or (isinstance(node, ast.Name) and node.id == "environ"))


def _name_call(node):
    """``envnames.name("X")`` -> ``DIAKTOROS_X``, else ``None``."""
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "name" and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "envnames"):
        arg = node.args[0] if node.args else None
        short = arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else "?"
        return "DIAKTOROS_" + short
    return None


def bound_names(tree) -> dict[str, str]:
    """Names assigned an ``envnames.name(...)`` value or a prefixed literal: ``X_ENV = ...``."""
    bound = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            value = node.value
            key = _name_call(value) or (
                value.value if isinstance(value, ast.Constant) and isinstance(value.value, str)
                and value.value.startswith(PREFIXES) else None)
            if key:
                bound[node.targets[0].id] = key
    return bound


def _key(node, bound):
    """The variable a read names when this guard covers it, else ``None``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if node.value.startswith(PREFIXES) else None
    if isinstance(node, ast.Name):
        return bound.get(node.id)
    if isinstance(node, ast.Attribute):          # gate_failures.REDRIVE_ENV
        return bound.get(node.attr)
    return _name_call(node)


def _call_key(node, bound):
    arg = node.args[0] if node.args else next(
        (kw.value for kw in node.keywords if kw.arg == "key"), None)
    return _key(arg, bound) if arg is not None else None


def _read_key(node, bound):
    """The covered variable ``node`` reads directly, else ``None``. Setting one
    (``os.environ[...] = ...``) is not a read; ``os.environ[...] += ...`` is."""
    if (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
            and _is_environ(node.value)):
        return _key(node.slice, bound)
    if (isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Subscript)
            and _is_environ(node.target.value)):
        return _key(node.target.slice, bound)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        func = node.func
        if func.attr == "get" and _is_environ(func.value):
            return _call_key(node, bound)
        if func.attr == "getenv" and isinstance(func.value, ast.Name) and func.value.id == "os":
            return _call_key(node, bound)
    return None


def _script_text(node):
    """The source a string constant or f-string holds (an interpolation becomes ``0``)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "0" for v in node.values)
    return None


def direct_reads(source: str, bound=None, depth: int = 0) -> list[tuple[str, str]]:
    """``(variable, kind)`` for every covered direct read in ``source``, in walk order; kind is
    ``SCRIPT`` inside Python held in a string (a generated child-process script), else ``CODE``."""
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        if depth == 0:
            return []
        return [(key, SCRIPT) for key in _FALLBACK.findall(source)]
    bound = {**(bound or {}), **bound_names(tree)}
    kind = SCRIPT if depth else CODE
    inner = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    found = []
    for node in ast.walk(tree):
        key = _read_key(node, bound)
        if key:
            found.append((key, kind))
            continue
        text = None if id(node) in inner else _script_text(node)
        if depth < 2 and text and ("environ" in text or "getenv" in text):
            found.extend((k, SCRIPT) for k, _ in direct_reads(text, None, depth + 1))
    return found


def scan(root: Path = _sources.ROOT) -> dict[tuple[str, str, str], int]:
    """Every direct read outside ``envnames.py``: (file, variable, kind) -> count. A name bound
    to a covered variable in any scanned file counts in every file (``module.X_ENV``)."""
    paths = [p for p in _sources.python_sources(root) if p.relative_to(root) != HOME]
    texts = {p: p.read_text(errors="replace") for p in paths}
    bound: dict[str, str] = {}
    for text in texts.values():
        try:
            bound.update(bound_names(ast.parse(text)))
        except SyntaxError:
            pass
    hits: dict[tuple[str, str, str], int] = {}
    for path, text in texts.items():
        for key, kind in direct_reads(text, bound):
            entry = (path.relative_to(root).as_posix(), key, kind)
            hits[entry] = hits.get(entry, 0) + 1
    return hits


def stray(hits) -> list:
    """The reads ``ALLOWED`` does not cover: any ordinary read, a script read not listed, or a
    listed one read more often than listed."""
    return sorted((entry, n) for entry, n in hits.items()
                  if entry[2] != SCRIPT or ALLOWED.get(entry[:2], (0,))[0] != n)


class EnvReadsViaEnvnames(unittest.TestCase):
    def test_only_allowlisted_direct_reads(self):
        self.assertEqual(stray(scan()), [], "read these through diaktoros.envnames.get (both "
                         "spellings, #542), or add them to ALLOWED with the reason they cannot")

    def test_allowlist_has_no_stale_entries(self):
        found = {entry[:2] for entry in scan() if entry[2] == SCRIPT}
        self.assertEqual(sorted(set(ALLOWED) - found), [],
                         "these ALLOWED reads are gone: drop them from the list")

    def test_every_read_shape_is_caught(self):
        cases = {
            'os.environ.get("DIAKTOROS_X")': "DIAKTOROS_X",
            "os.environ['REVIEW_LOOP_X']": "REVIEW_LOOP_X",
            'os.getenv("DIAKTOROS_X", "")': "DIAKTOROS_X",
            'os.getenv(key="DIAKTOROS_X")': "DIAKTOROS_X",
            'os.environ.get(key="DIAKTOROS_X")': "DIAKTOROS_X",
            'os.environ["DIAKTOROS_X"] += "x"': "DIAKTOROS_X",
            'os.environ.get(envnames.name("X"))': "DIAKTOROS_X",
            'os.environ[envnames.name("X")]': "DIAKTOROS_X",
            'X_ENV = envnames.name("X")\nos.environ.get(X_ENV)': "DIAKTOROS_X",
            'os.environ.get(gate_failures.REDRIVE_ENV)': "DIAKTOROS_GATE_REDRIVE",
        }
        bound = {"REDRIVE_ENV": "DIAKTOROS_GATE_REDRIVE"}
        for source, key in cases.items():
            with self.subTest(source=source):
                self.assertEqual(direct_reads(source, bound), [(key, CODE)])

    def test_reads_in_generated_scripts_are_caught(self):
        cases = {
            'SCRIPT = "import os\\nprint(os.environ[\'DIAKTOROS_X\'])\\n"': "DIAKTOROS_X",
            'SCRIPT = ("import os\\n"\n          "v = os.getenv(\'REVIEW_LOOP_X\')\\n")': "REVIEW_LOOP_X",
            # An interpolation that splits a statement: no fragment parses alone (F4).
            "S = f\"value = os.getenv('DIAKTOROS_X')\\nprint({42})\\n\"": "DIAKTOROS_X",
            "S = f\"v = [os.getenv('DIAKTOROS_X'), {n}\"": "DIAKTOROS_X",   # unparsable script
        }
        for source, key in cases.items():
            with self.subTest(source=source):
                self.assertEqual(direct_reads(source), [(key, SCRIPT)])

    def test_reads_that_are_fine_pass(self):
        for source in ('envnames.get("X")', 'os.environ.get("HOME")',
                       '[os.environ.get(n) for n in envnames.both("X")]',
                       '{**os.environ, envnames.name("X"): "1"}',
                       'X_ENV = envnames.name("X")\nenv[X_ENV] = "1"',
                       'os.environ["DIAKTOROS_X"] = "1"', 'print("DIAKTOROS_X")'):
            with self.subTest(source=source):
                self.assertEqual(direct_reads(source), [])

    def scan_tree(self, files: dict[str, str]):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for rel, text in {"diaktoros/envnames.py": 'os.environ.get("DIAKTOROS_X")\n',
                          **files}.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        return scan(root)

    def test_a_new_direct_read_fails_the_scan(self):
        hits = self.scan_tree({"scripts/new.py": 'import os\nos.environ.get("DIAKTOROS_X")\n'})
        self.assertEqual(hits, {("scripts/new.py", "DIAKTOROS_X", CODE): 1})
        self.assertTrue(stray(hits))

    def test_a_bound_name_read_from_another_module_fails_the_scan(self):
        hits = self.scan_tree({
            "diaktoros/a.py": 'from . import envnames\nA_ENV = envnames.name("A")\n',
            "diaktoros/b.py": 'import os\nfrom . import a\nv = os.environ.get(a.A_ENV)\n'})
        self.assertEqual(stray(hits), [(("diaktoros/b.py", "DIAKTOROS_A", CODE), 1)])

    def test_an_ordinary_read_of_an_allowlisted_name_fails_the_scan(self):
        # F2: the allowlisted script read stays allowed; a second, ordinary read does not.
        harness = "_HARNESS = '''\nimport os\nstub = os.environ.get(\"DIAKTOROS_GH_STUB\")\n'''\n"
        hits = self.scan_tree({"diaktoros/trace.py": harness})
        self.assertEqual(stray(hits), [])
        hits = self.scan_tree({"diaktoros/trace.py": harness
                               + 'import os\nx = os.environ.get("DIAKTOROS_GH_STUB")\n'})
        self.assertEqual(stray(hits), [(("diaktoros/trace.py", "DIAKTOROS_GH_STUB", CODE), 1)])
        hits = self.scan_tree({"diaktoros/trace.py": harness + "_MORE = '''\nimport os\n"
                               "y = os.environ.get(\"DIAKTOROS_GH_STUB\")\n'''\n"})
        self.assertEqual(stray(hits), [(("diaktoros/trace.py", "DIAKTOROS_GH_STUB", SCRIPT), 2)])


class OldSpellingIsRead(unittest.TestCase):
    """The reads F1 found by a bound name now go through ``envnames.get``: either spelling."""

    def env(self, **values):
        clean = {k: v for k, v in os.environ.items()
                 if not k.endswith(("_WORKER", "_GATE_REDRIVE"))}
        return mock.patch.dict(os.environ, {**clean, **values}, clear=True)

    def test_worker_mark_in_either_spelling(self):
        from diaktoros import hostdirs
        for name in ("DIAKTOROS_WORKER", "REVIEW_LOOP_WORKER"):
            with self.subTest(name=name), self.env(**{name: "1"}):
                self.assertTrue(hostdirs.in_worker())
        with self.env():
            self.assertFalse(hostdirs.in_worker())

    def test_redrive_mark_in_either_spelling(self):
        from diaktoros import app_ingress
        for name in ("DIAKTOROS_GATE_REDRIVE", "REVIEW_LOOP_GATE_REDRIVE"):
            with self.subTest(name=name), self.env(**{name: "k"}):
                self.assertTrue(app_ingress.redriven())
        with self.env():
            self.assertFalse(app_ingress.redriven())


if __name__ == "__main__":
    unittest.main()
