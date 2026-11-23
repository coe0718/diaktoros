"""Seventh review on #2587: a guard's list named by a module constant is read as that tuple only
when nothing binds the name again, anywhere. ``ALLOWED += ('intruder',)`` after it let a writer
record 'intruder' while the drift guard reported only the tuple's roles. Eighth: nor through a
namespace dict (``globals()['ALLOWED'] = …``), nor by a name the guard cannot read, which fails
every constant closed. Ninth: a ``setattr`` of a computed name on anything not shown never to be
a scanned module (an ``except`` capture, an import from another package) fails them closed too.
Tenth: a parameter that shadows such an import is not shown so, since a caller may pass a module.
Eleventh: nor is a name imported from another package (``from sys import x``, ``import a.b as x``),
which is that package's attribute and may be a scanned module it exported. Twelfth: nor through
``setattr`` spelled any way but a direct call (bound to another name, read from ``builtins``,
passed on, or ``__setattr__``), whose writes the guard cannot read.

Each case runs beside every real writer (``_audit_writers.Writers``), as the vocabulary
assertion in ``test_audit_vocabulary`` does.
"""
import _home_guard  # noqa: F401
import unittest

from _audit_writers import ROOT, Unresolved, Writers
from diaktoros import broker

_CONST = "ALLOWED_2587 = ('reviewer',)\n"
_WRITER = ("def write_const_2587(loop, role):\n    if role not in ALLOWED_2587:\n"
           "        raise ValueError(role)\n"
           "    _audit(loop, 'acme/w', 1, 'a' * 40, '', role, 'review', 'x')\n")
REBOUND = {
    'augmented after it': "ALLOWED_2587 += ('intruder',)\n",
    'assigned again': "ALLOWED_2587 = ('intruder',)\n",
    'from a function by global': "def widen():\n    global ALLOWED_2587\n"
                                 "    ALLOWED_2587 = ('intruder',)\n",
    'as an attribute of its module': "import synthetic\nsynthetic.ALLOWED_2587 = ('intruder',)\n",
    'by setattr': "import synthetic\nsetattr(synthetic, 'ALLOWED_2587', ('intruder',))\n",
    # Eighth review: through a namespace dict, by a literal name or one the guard cannot read.
    'in globals()': "globals()['ALLOWED_2587'] = ('reviewer', 'intruder')\n",
    'in vars() of its module': "import synthetic\nvars(synthetic)['ALLOWED_2587'] = ('intruder',)\n",
    'in its module __dict__': "import sys\n"
                              "sys.modules['synthetic'].__dict__['ALLOWED_2587'] = ('intruder',)\n",
    'by globals().update': "globals().update(ALLOWED_2587=('intruder',))\n",
    'through an alias': "ns = globals()\nns['ALLOWED_2587'] = ('intruder',)\n",
    'under a computed key': "key = 'ALLOWED_' + '2587'\nglobals()[key] = ('intruder',)\n",
    'by setattr of a computed name': "import synthetic\nname = 'ALLOWED_2587'\n"
                                     "setattr(synthetic, name, ('intruder',))\n",
    'by setattr on sys.modules': "import sys\nname = 'ALLOWED_2587'\n"
                                 "setattr(sys.modules['synthetic'], name, ('intruder',))\n",
    # Ninth review: on a module reached through a read of a namespace, or any other name.
    'by setattr on a module read from globals()': "import sys\nme = sys.modules[__name__]\n"
        "name = 'ALLOWED_2587'\nsetattr(globals().get('me'), name, ('reviewer', 'intruder'))\n",
    'by setattr on an alias of its module': "import synthetic\nm = synthetic\n"
                                            "name = 'ALLOWED_2587'\nsetattr(m, name, ('x',))\n",
    # Tenth: on a parameter that shadows a safe import, which a caller may pass its module.
    'by setattr on a parameter shadowing an import': "import os\ndef widen(os, name):\n"
        "    setattr(os, name, ('reviewer', 'intruder'))\n",
    'by setattr on a lambda parameter shadowing an import': "import os\n"
        "widen = lambda os, name: setattr(os, name, ('reviewer', 'intruder'))\n",
    # Eleventh: on an attribute imported from another package, which may be a scanned module.
    'by setattr on a name imported from another package': "import sys, synthetic\n"
        "sys.target_2587 = synthetic\nfrom sys import target_2587 as external_2587\n"
        "name = 'ALLOWED_2587'\nsetattr(external_2587, name, ('reviewer', 'intruder'))\n",
    'by setattr on a submodule imported under a name': "import os.path as sub\n"
        "name = 'ALLOWED_2587'\nsetattr(sub, name, ('reviewer', 'intruder'))\n",
    # Twelfth: through setattr under another spelling, even with a literal name.
    'by setattr bound to another name': "import sys\nset_attr = setattr\n"
        "set_attr(sys.modules[__name__], 'ALLOWED_2587', ('reviewer', 'intruder'))\n",
    'by setattr imported under a name': "from builtins import setattr as put\nimport synthetic\n"
        "put(synthetic, 'ALLOWED_2587', ('intruder',))\n",
    'by setattr read from builtins': "import builtins, synthetic\n"
        "getattr(builtins, 'setattr')(synthetic, 'ALLOWED_2587', ('intruder',))\n",
    'by setattr passed on': "import functools, synthetic\n"
        "functools.partial(setattr, synthetic)('ALLOWED_2587', ('intruder',))\n",
    'by __setattr__ of its module': "import synthetic\n"
        "synthetic.__setattr__('ALLOWED_2587', ('intruder',))\n",
}
# Reads of a namespace dict, and a computed setattr on another package's module or on a caught
# exception, in the scope that name resolves in, rebind nothing here.
READ_ONLY = ("x = globals().get('ALLOWED_2587')\ny = vars(object())['a']\n"
             "import os\nname = 'getcwd'\nsetattr(os, name, os.getcwd)\n"
             "try:\n    pass\nexcept ValueError as exc:\n    setattr(exc, name, True)\n"
             # Tenth: a parameter of the same name in another function shadows nothing here.
             "def other(os, exc):\n    return os, exc\n"
             "def safe(name):\n    setattr(os, name, os.getcwd)\n")


def real():
    return [(str(path), path.read_text()) for path in sorted((ROOT / 'diaktoros').rglob('*.py'))]


class GuardConstants(unittest.TestCase):
    def test_a_guard_constant_bound_again_fails_the_full_vocabulary(self):
        sources = real()
        for shape, extra in REBOUND.items():
            with self.subTest(shape, where='same module'), self.assertRaises(Unresolved):
                Writers(sources + [('synthetic.py', _CONST + extra + _WRITER)]).written()
            with self.subTest(shape, where='another module'), self.assertRaises(Unresolved):
                Writers(sources + [('synthetic.py', _CONST + _WRITER),
                                   ('other.py', extra)]).written()

    def test_a_guard_constant_bound_once_bounds_the_writer(self):
        for extra in ('', READ_ONLY):
            with self.subTest(read_only=bool(extra)):
                roles, _ = Writers(real() + [('synthetic.py', _CONST + extra + _WRITER)]).written()
                self.assertEqual(roles, set(broker.AUDIT_ROLES))


if __name__ == "__main__":
    unittest.main()
