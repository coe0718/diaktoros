"""Every ``observer.notify`` call names an event in ``observer.EVENTS`` (#2301).

``observer.notify`` logs and drops an event it does not know, so a misspelled event at a new call
site sends nothing. This scans every call to ``notify`` on the observer module in ``diaktoros/``
and ``scripts/`` (in the style of ``tests/test_state_junk.py::ExplainKinds``): the event argument,
positional (the third) or ``event=``, must be a string literal in ``observer.EVENTS``. A call
whose event is not a literal fails unless ``ALLOWED`` names its function and says why; the
literals that reach an allowed call are then checked instead (see ``ALLOWED``).

The scan fails closed on what it cannot read (review of #2356): the observer module or its
``notify`` imported under another name, or reached through a package alias (``dk.observer``),
is followed, and the module passed on as a value (``obs = observer``) fails; a starred or ``**``
argument that could carry the event fails; an allowed function must pass its ``event`` variable
unchanged, bound only in the forms the scan reads (any other binding, ``def event`` included,
fails); an allowed wrapper's callers are found by name, by ``module.name`` and
through ``from ... import`` (aliases too), and any other reference to the wrapper fails.

The scan, ``ALLOWED`` with it, is ``tools/observer_event_literals.py`` (with
``tools/observer_notify_ast.py``); see there why it is not in this file.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# The scan lives in tools/observer_event_literals.py and tools/observer_notify_ast.py, outside
# tests/, so the host's base-failure proof runs the tests below against the base's scan, not this
# PR's (#1973, #2536 review F1).
sys.path.insert(0, str(ROOT / "tools"))
import observer_event_literals  # noqa: E402
import observer_notify_ast  # noqa: E402
from observer_event_literals import _FUNCS, ALLOWED, _entry_used, _functions, scan  # noqa: E402


def repo_sources():
    files = sorted((ROOT / "diaktoros").rglob("*.py")) + sorted((ROOT / "scripts").rglob("*.py"))
    return {p.relative_to(ROOT).as_posix(): p.read_text() for p in files}


def with_second_failure_notice():
    """The repo's sources with a second ``failure_notice`` in the allowed file, sending its
    ``event`` parameter unchecked (#2369, #2373)."""
    sources = repo_sources()
    sources["diaktoros/run_supervisor/notices.py"] += (
        "\n\nclass Other:\n    def failure_notice(self, loop, st, event):\n"
        "        from diaktoros import observer\n"
        "        observer.notify(loop, st, event, 1)\n")
    return sources


def edited(path, old, new):
    """The repo's sources with ``old`` replaced once by ``new`` in ``path``."""
    sources = repo_sources()
    assert sources[path].count(old) == 1, f"{old!r} not found once in {path}"
    sources[path] = sources[path].replace(old, new)
    return sources


class ObserverEventLiterals(unittest.TestCase):
    def test_every_notify_event_is_declared(self):
        problems, calls = scan(repo_sources())
        self.assertGreater(calls, 20, "few observer.notify calls found: the scan is broken")
        self.assertEqual(problems, [])

    def test_allowlist_entries_are_used(self):
        # A stale entry would silently allow a non-literal event in a future function.
        sources = repo_sources()
        for path, func in ALLOWED:
            self.assertTrue(_entry_used(sources[path], func), f"stale ALLOWED entry {path}:{func}")

    def test_planted_unknown_event_fails(self):
        planted = "def f(loop, st):\n    observer.notify(loop, st, 'no-such-event', 1, '')\n"
        problems, _ = scan({"diaktoros/planted.py": planted})
        self.assertEqual(len(problems), 1)
        self.assertIn("'no-such-event'", problems[0])

    def test_planted_keyword_unknown_event_fails(self):
        planted = "observer.notify(loop, st, event='no-such-event', number=1)\n"
        self.assertEqual(len(scan({"scripts/planted.py": planted})[0]), 1)

    def test_planted_non_literal_event_fails(self):
        planted = "def g(loop, st, kind):\n    observer.notify(loop, st, kind, 1, '')\n"
        problems, _ = scan({"diaktoros/planted.py": planted})
        self.assertEqual(len(problems), 1)
        self.assertIn("not a string literal", problems[0])

    def test_planted_wrapper_caller_unknown_event_fails(self):
        sources = repo_sources()
        sources["diaktoros/planted.py"] = (
            "def h(loop, scope):\n    broker_ipc._issue_notice(loop, scope, 'trigaed', 'x')\n")
        problems, _ = scan(sources)
        self.assertEqual(problems, ["diaktoros/planted.py:2: unknown observer event 'trigaed'"])


class ScanBypasses(unittest.TestCase):
    """Review of #2356: each of these sent an unchecked event and the scan passed."""

    def assertFlags(self, sources, text):
        problems, _ = scan(sources)
        self.assertTrue(problems, "the planted bypass passed the scan")
        self.assertTrue(any(text in p for p in problems), problems)

    def test_annotated_reassignment_in_assigned_function_fails(self):
        # F1: ``event: str = "..."`` after ``event = "failed"`` in failure_notice.
        self.assertFlags(edited("diaktoros/run_supervisor/notices.py", 'event = "failed"\n',
                                'event = "failed"\n            event: str = "no-such-event"\n'),
                         "'no-such-event'")

    def test_other_rebinding_in_assigned_function_fails(self):
        # F1: any binding the scan cannot read (here ``+=``) fails rather than being skipped.
        self.assertFlags(edited("diaktoros/run_supervisor/notices.py", 'event = "failed"\n',
                                'event = "failed"\n            event += "-typo"\n'),
                         "other than a literal")

    def test_wrapper_forwarding_an_expression_fails(self):
        # F1: the wrapper sends ``event + "-typo"``, not the parameter its callers are checked on.
        old = "observer.notify(loop, state_mod.state_for(loop), event, scope.number"
        self.assertFlags(edited("diaktoros/broker/ipc.py", old, old.replace(
            ", event,", ', event + "-typo",')), "not its `event` variable unchanged")

    def test_wrapper_rebinding_its_parameter_fails(self):
        old = "    from .. import observer, state as state_mod\n    try:\n        observer.notify("
        self.assertFlags(edited("diaktoros/broker/ipc.py", old, old.replace(
            "    try:\n", '    event = "no-such-event"\n    try:\n')), "rebinds its `event`")

    def test_wrapper_imported_by_name_fails(self):
        # F2: ``from .broker.ipc import _issue_notice`` (and an alias) in another module.
        for imported, called in (("_issue_notice", "_issue_notice"),
                                 ("_issue_notice as note", "note")):
            sources = repo_sources()
            sources["diaktoros/planted.py"] = (
                f"from .broker.ipc import {imported}\n\n\ndef h(loop, scope):\n"
                f"    {called}(loop, scope, 'no-such-event', 'outcome')\n")
            self.assertFlags(sources, "diaktoros/planted.py:5: unknown observer event")

    def test_wrapper_referenced_without_a_call_fails(self):
        sources = repo_sources()
        sources["diaktoros/planted.py"] = (
            "from .broker import ipc as broker_ipc\nsend = broker_ipc._issue_notice\n")
        self.assertFlags(sources, "wrapper referenced, not called")

    def test_starred_arguments_fail(self):
        # F3: at run time the third argument is 'no-such-event', not the fourth 'held'.
        for call in ("observer.notify(*(loop, st), 'no-such-event', 'held')",
                     "observer.notify(loop, st, **kw)"):
            self.assertFlags({"diaktoros/planted.py": f"def f(loop, st, kw):\n    {call}\n"},
                             "argument could carry the event")

    def test_observer_module_alias_fails(self):
        # F4: the observer module, or its notify, under another name.
        for header, call in (("from . import observer as obs", "obs.notify"),
                             ("import diaktoros.observer as obs", "obs.notify"),
                             ("from .observer import notify as send", "send"),
                             ("import diaktoros.observer", "diaktoros.observer.notify")):
            planted = f"{header}\n\n\ndef f(loop, st):\n    {call}(loop, st, 'no-such-event', 1)\n"
            self.assertFlags({"diaktoros/planted.py": planted},
                             "diaktoros/planted.py:5: unknown observer event 'no-such-event'")

    def test_notify_referenced_without_a_call_fails(self):
        planted = "from . import observer as obs\nsend = obs.notify\n"
        self.assertFlags({"diaktoros/planted.py": planted}, "referenced, not called")

    def test_decorated_definition_rebinding_event_fails(self):
        # Review 2 of #2356: a decorator rebinds ``event`` to any value, here an unknown one.
        for definition in ("def event():\n                pass\n",
                           "class event:\n                pass\n"):
            self.assertFlags(edited(
                "diaktoros/run_supervisor/notices.py", 'event = "failed"\n',
                'event = "failed"\n            @(lambda fn: "no-such-event")\n'
                f'            {definition}'), "other than a literal")

    def test_observer_reached_through_a_package_alias_fails(self):
        # Review 2 of #2356: ``import diaktoros as dk`` then ``dk.observer.notify(...)``.
        planted = ("import diaktoros as dk\n\n\ndef f(loop, st):\n"
                   "    dk.observer.notify(loop, st, 'no-such-event', 1)\n")
        self.assertFlags({"diaktoros/planted.py": planted},
                         "diaktoros/planted.py:5: unknown observer event 'no-such-event'")

    def test_same_named_function_in_an_allowed_file_fails(self):
        # #2369, #2373: ALLOWED was keyed by the bare name and checked the first function of
        # that name, so a second ``failure_notice`` sending its ``event`` parameter passed.
        self.assertFlags(with_second_failure_notice(),
                         "not a string literal (in Other.failure_notice)")

    def test_the_owning_function_rule_is_needed(self):
        # #2536 review F1: undone (``ALLOWED`` by bare name, the first function of that name
        # checked), the second ``failure_notice`` passes, and a bare-name entry counts as used,
        # so the allowlist test could not call it stale. (The base proof runs the base's scan
        # from tools/ now; this pins what #2369's rule does.)
        real = _functions

        def by_bare_name(tree):
            first = {}
            for node in ast.walk(tree):
                if isinstance(node, _FUNCS):
                    first.setdefault(node.name, node)
            return {node: (fn.name, first[fn.name]) if fn else (None, None)
                    for node, (_, fn) in real(tree).items()}
        bare = {(path, func.rsplit(".", 1)[-1]): v for (path, func), v in ALLOWED.items()}
        notices = repo_sources()["diaktoros/run_supervisor/notices.py"]
        self.assertFlags(with_second_failure_notice(), "(in Other.failure_notice)")
        self.assertFalse(_entry_used(notices, "failure_notice"))
        with mock.patch.object(observer_event_literals, "_functions", by_bare_name), \
                mock.patch.dict(ALLOWED, bare, clear=True):
            self.assertEqual(scan(with_second_failure_notice())[0], [])
            self.assertTrue(_entry_used(notices, "failure_notice"))

    def test_a_base_proof_tests_the_base_scan(self):
        # #2536 review F1: the scan lives off the test paths, so the host's base-failure proof
        # (change_proof.apply_tests) runs the head's tests here against the base's scan. It fails
        # if either module goes back into a tests/ module, where the scan was.
        import _base_proof
        for module in (observer_event_literals, observer_notify_ast):
            with self.subTest(module=module.__name__):
                _base_proof.assert_taken_from_the_base(self, module.__file__)

    def test_observer_module_passed_on_fails(self):
        # Review 2 of #2356: ``obs = observer; obs.notify(...)`` and the like hide the call.
        for header, line in (("from . import observer", "obs = observer"),
                             ("import diaktoros as dk", "obs = dk.observer"),
                             ("from . import observer as o", "send = getattr(o, 'notify')"),
                             ("from . import observer", "relay(observer)")):
            planted = (f"{header}\n\n\ndef f(loop, st):\n    {line}\n"
                       "    obs.notify(loop, st, 'no-such-event', 1)\n")
            self.assertFlags({"diaktoros/planted.py": planted},
                             "diaktoros/planted.py:5: the observer module is passed on")


if __name__ == "__main__":
    unittest.main()
