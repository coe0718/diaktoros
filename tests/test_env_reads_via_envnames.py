"""Every ``DIAKTOROS_``/``REVIEW_LOOP_`` environment read goes through ``envnames`` (#2300).

#542 was a variable read by its new name only, which missed installs still on the old
``REVIEW_LOOP_`` spelling. ``diaktoros/envnames.py`` reads both, so a direct
``os.environ.get(...)``, ``os.environ[...]`` or ``os.getenv(...)`` whose key is a literal with
either prefix, or an ``envnames.name(...)`` value, fails this scan anywhere in the repository's own
Python (``_sources``: ``diaktoros/``, ``scripts/`` and the rest, #1489) but ``envnames.py``
itself. Python source held in a string constant (a script run in a child process) is scanned
too. ``ALLOWED`` names the few reads that must stay direct, each with its reason.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _sources  # noqa: E402  every *.py the scans read (#1489)

PREFIXES = ("DIAKTOROS_", "REVIEW_LOOP_")
HOME = Path("diaktoros/envnames.py")

# (file, variable) -> why the read stays direct. Each is a script generated as a string and run
# by a bare interpreter in a child process, which cannot import ``envnames``.
ALLOWED = {
    ("diaktoros/trace.py", "DIAKTOROS_GH_STUB"):
        "_HARNESS runs in a child process (python -c); it reads both spellings by hand",
    ("diaktoros/trace.py", "REVIEW_LOOP_GH_STUB"):
        "_HARNESS runs in a child process (python -c); it reads both spellings by hand",
    ("diaktoros/safe_push.py", "DIAKTOROS_TOKEN_FILE"):
        "_ASKPASS is Git's credential prompt, a child-process script; safe_push itself sets this "
        "one spelling in that child's environment, so there is no old name to miss",
}


def _is_environ(node) -> bool:
    """``os.environ`` (or a bare ``environ``)."""
    return ((isinstance(node, ast.Attribute) and node.attr == "environ"
             and isinstance(node.value, ast.Name) and node.value.id == "os")
            or (isinstance(node, ast.Name) and node.id == "environ"))


def _key(node):
    """The variable a read names when this guard covers it, else ``None``: a string literal with
    either prefix, or ``envnames.name("X")`` (reported as ``DIAKTOROS_X``)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if node.value.startswith(PREFIXES) else None
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "name" and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "envnames"):
        arg = node.args[0] if node.args else None
        short = arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else "?"
        return "DIAKTOROS_" + short
    return None


def _read_key(node):
    """The covered variable ``node`` reads directly, else ``None``. Setting one
    (``os.environ[...] = ...``) is not a read."""
    if (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
            and _is_environ(node.value)):
        return _key(node.slice)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
        func = node.func
        if func.attr == "get" and _is_environ(func.value):
            return _key(node.args[0])
        if func.attr == "getenv" and isinstance(func.value, ast.Name) and func.value.id == "os":
            return _key(node.args[0])
    return None


def direct_reads(source: str, depth: int = 0) -> list[str]:
    """The covered variables ``source`` reads directly, in order, including in Python source held
    in its string constants (a generated child-process script; adjacent literals are already one
    constant)."""
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return []
    found = []
    for node in ast.walk(tree):
        key = _read_key(node)
        if key:
            found.append(key)
        elif (depth < 2 and isinstance(node, ast.Constant) and isinstance(node.value, str)
              and ("environ" in node.value or "getenv" in node.value)):
            found.extend(direct_reads(node.value, depth + 1))
    return found


def scan(root: Path = _sources.ROOT) -> dict[tuple[str, str], int]:
    """Every direct read outside ``envnames.py``: (file, variable) -> count."""
    hits: dict[tuple[str, str], int] = {}
    for path in _sources.python_sources(root):
        rel = path.relative_to(root)
        if rel == HOME:
            continue
        for key in direct_reads(path.read_text(errors="replace")):
            hits[(rel.as_posix(), key)] = hits.get((rel.as_posix(), key), 0) + 1
    return hits


class EnvReadsViaEnvnames(unittest.TestCase):
    def test_only_allowlisted_direct_reads(self):
        stray = sorted(set(scan()) - set(ALLOWED))
        self.assertEqual(stray, [], "read these through diaktoros.envnames.get (both spellings, "
                                    "#542), or add them to ALLOWED with the reason they cannot")

    def test_allowlist_has_no_stale_entries(self):
        stale = sorted(set(ALLOWED) - set(scan()))
        self.assertEqual(stale, [], "these ALLOWED reads are gone: drop them from the list")

    def test_every_read_shape_is_caught(self):
        cases = {
            'os.environ.get("DIAKTOROS_X")': "DIAKTOROS_X",
            "os.environ['REVIEW_LOOP_X']": "REVIEW_LOOP_X",
            'os.getenv("DIAKTOROS_X", "")': "DIAKTOROS_X",
            'os.environ.get(envnames.name("X"))': "DIAKTOROS_X",
            'os.environ[envnames.name("X")]': "DIAKTOROS_X",
            'SCRIPT = "import os\\nprint(os.environ[\'DIAKTOROS_X\'])\\n"': "DIAKTOROS_X",
            'SCRIPT = ("import os\\n"\n          "v = os.getenv(\'REVIEW_LOOP_X\')\\n")': "REVIEW_LOOP_X",
        }
        for source, key in cases.items():
            with self.subTest(source=source):
                self.assertEqual(direct_reads(source), [key])

    def test_reads_that_are_fine_pass(self):
        for source in ('envnames.get("X")', 'os.environ.get("HOME")',
                       '[os.environ.get(n) for n in envnames.both("X")]',
                       '{**os.environ, envnames.name("X"): "1"}',
                       'os.environ["DIAKTOROS_X"] = "1"', 'print("DIAKTOROS_X")'):
            with self.subTest(source=source):
                self.assertEqual(direct_reads(source), [])

    def test_a_new_direct_read_fails_the_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "diaktoros").mkdir()
            (root / "diaktoros" / "envnames.py").write_text('os.environ.get("DIAKTOROS_X")\n')
            (root / "scripts").mkdir()
            (root / "scripts" / "new.py").write_text('import os\nos.environ.get("DIAKTOROS_X")\n')
            self.assertEqual(scan(root), {("scripts/new.py", "DIAKTOROS_X"): 1})


if __name__ == "__main__":
    unittest.main()
