"""The declared loop-file keys are exactly the top-level keys ``config.normalize`` reads (#2306).

``blocks.check_known_keys`` refuses a key that is not declared (#540). This is the reverse: a
declared key (``DEFAULTS`` or ``LOOP_EXTRA_KEYS``) that nothing in ``normalize`` reads is a
dead setting that ``show`` and the form still offer, and a key ``normalize`` reads that is not
declared is one the loop file can never hold.

How the reads are found, with no list kept by hand:

* ``normalize``'s own body is read by AST: ``loop["k"]``, ``loop.get("k")``, ``"k" in loop``;
* every helper ``normalize`` hands ``loop`` to (found in the same AST) is wrapped for the
  call, so it gets a dict subclass that records each key it, or anything it passes ``loop``
  on to, reads; what the helper wrote is copied back, so ``normalize`` runs unchanged.

The reads are the union over ``CONFIGS``; every declared key is read on the minimal loop's
path today, so a key that comes to be read only on one branch needs a config here that takes it.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import inspect
from pathlib import Path
import sys
import textwrap
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import config  # noqa: E402
from diaktoros.config import blocks, flags, lists, loop_checks  # noqa: E402

# Declared keys normalize does not read, each with where it is read instead and why.
READ_ELSEWHERE = {
    "base": "read raw at each use (loop['base']: gh, watch_stacked, merge checks); normalize "
            "keeps the default and checks nothing",
    "inflight_ttl_min": "read through state.LoopState.minutes, which heals a bad value to the "
                        "schema default at every read; normalize does not coerce it by design",
    "observer_disabled": "written by `dk set --observer off` (cli/set_command.py) and read back "
                         "by it and by cli/operations.py to restore the observer",
}

BASE = {"repo": "o/n", "fixers": ["f"], "reviewers": ["r"], "read_token": "rd",
        "tokens": {"rd": "/x"},
        "seats": {"reviewer": {"route": "a", "profile": "p"},
                  "fixer": {"route": "b", "profile": "q"}}}
CONFIGS = (BASE,)

MODULES = {"blocks": blocks, "flags": flags, "lists": lists, "loop_checks": loop_checks}


class Reads(dict):
    """A dict that adds every key looked up in it to ``seen``."""

    def __init__(self, data, seen):
        super().__init__(data)
        self.seen = seen

    def __getitem__(self, key):
        self.seen.add(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.seen.add(key)
        return super().get(key, default)

    def __contains__(self, key):
        self.seen.add(key)
        return super().__contains__(key)

    def setdefault(self, key, default=None):
        self.seen.add(key)
        return super().setdefault(key, default)

    def pop(self, key, *default):
        self.seen.add(key)
        return super().pop(key, *default)


def _is_loop(node):
    return isinstance(node, ast.Name) and node.id == "loop"


def normalize_ast():
    """``(keys normalize's body reads directly, [(module, function, arg index) given loop]``."""
    body = ast.parse(textwrap.dedent(inspect.getsource(config.normalize))).body[0]
    direct, handed = set(), []
    for node in ast.walk(body):
        if (isinstance(node, ast.Subscript) and _is_loop(node.value)
                and isinstance(node.ctx, ast.Load) and isinstance(node.slice, ast.Constant)):
            direct.add(node.slice.value)
        elif (isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant)
              and any(_is_loop(c) for c in node.comparators)):
            direct.add(node.left.value)
        elif isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute) and _is_loop(func.value) and node.args
                    and isinstance(node.args[0], ast.Constant)):
                direct.add(node.args[0].value)
            for index, arg in enumerate(node.args):
                if _is_loop(arg):
                    handed.append((ast.unparse(func), index))
    return direct, handed


def keys_read(configs=CONFIGS):
    """Every top-level key ``normalize`` and the helpers it hands ``loop`` to read."""
    direct, handed = normalize_ast()
    seen = set(direct)
    patches = []
    for name, index in handed:
        module, _, attr = name.partition(".")
        if module not in MODULES:
            raise AssertionError(f"normalize hands loop to {name}, which this test cannot "
                                 "wrap: add its module to MODULES")
        original = getattr(MODULES[module], attr)

        def tracked(*args, _original=original, _index=index, **kwargs):
            args = list(args)
            real = args[_index]
            args[_index] = spy = Reads(real, seen)
            try:
                return _original(*args, **kwargs)
            finally:
                real.clear()
                real.update(dict(dict.items(spy)))
        patches.append(mock.patch.object(MODULES[module], attr, tracked))
    for patch in patches:
        patch.start()
    try:
        for raw in configs:
            config.normalize(dict(raw))
    finally:
        for patch in patches:
            patch.stop()
    return {k for k in seen if isinstance(k, str)}


def declared():
    return set(config.DEFAULTS) | blocks.LOOP_EXTRA_KEYS


def gaps(read):
    """``(declared but unread, read but undeclared)``, minus the named exceptions."""
    return (sorted(declared() - read - set(READ_ELSEWHERE)), sorted(read - declared()))


class NormalizeReadsEveryDeclaredKey(unittest.TestCase):
    def test_the_declared_keys_are_the_keys_normalize_reads(self):
        unread, undeclared = gaps(keys_read())
        self.assertEqual(unread, [], "declared loop-file keys normalize never reads: a dead "
                                     "setting, or one to name in READ_ELSEWHERE with where")
        self.assertEqual(undeclared, [], "keys normalize reads that no loop file may hold: "
                                         "declare them in DEFAULTS or LOOP_EXTRA_KEYS")

    def test_every_exception_is_declared_and_really_unread(self):
        read = keys_read()
        for key, why in READ_ELSEWHERE.items():
            self.assertIn(key, declared(), f"{key}: an exception for a key nobody declares")
            self.assertNotIn(key, read, f"{key}: normalize reads it now; drop the exception")
            self.assertTrue(why.strip(), key)

    def test_a_planted_declared_key_nothing_reads_fails(self):
        with mock.patch.dict(config.DEFAULTS, {"zz_dead_setting": 1}):
            self.assertEqual(gaps(keys_read()), (["zz_dead_setting"], []))

    def test_a_planted_read_of_an_undeclared_key_fails(self):
        original = loop_checks._check_limits

        def reads_more(loop, where):
            loop.get("zz_undeclared")
            return original(loop, where)
        with mock.patch.object(loop_checks, "_check_limits", reads_more):
            self.assertEqual(gaps(keys_read()), ([], ["zz_undeclared"]))


if __name__ == "__main__":
    unittest.main()
