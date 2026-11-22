"""Seventh review on #2587: a guard's list named by a module constant is read as that tuple only
when nothing binds the name again, anywhere. ``ALLOWED += ('intruder',)`` after it let a writer
record 'intruder' while the drift guard reported only the tuple's roles.

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
}


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
        roles, _ = Writers(real() + [('synthetic.py', _CONST + _WRITER)]).written()
        self.assertEqual(roles, set(broker.AUDIT_ROLES))


if __name__ == "__main__":
    unittest.main()
