"""The run_supervisor package split (#717): each moved group lives in one submodule, with one
binding, and nothing in the package shadows the submodule it calls.

``run_supervisor/__init__.py`` is the core every module imports; groups that move out become
submodules (``pr_facts`` first) that the core calls as ``pr_facts.<name>``. A moved name must not
stay reachable as ``run_supervisor.<name>`` (a second binding a test could patch without
reaching the code), nothing may import a moved name from the package, and no function may have
a local variable named like a submodule it calls: ``facts.pr_record`` once resolved to a local
``facts`` dict in ``isolated_prompt``, which is why the submodule is ``pr_facts``.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import importlib
import pathlib
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import run_supervisor  # noqa: E402

PACKAGE = pathlib.Path(run_supervisor.__file__).resolve().parent
ROOT = PACKAGE.parents[1]
# The shadow rules live in tools/submodule_shadows.py, outside tests/, so the host's base-failure
# proof runs the tests below against the base's rules, not this PR's (#1973, #2509).
sys.path.insert(0, str(ROOT / "tools"))
import submodule_shadows  # noqa: E402
from submodule_shadows import ALL_SUBMODULES  # noqa: E402
from submodule_shadows import local_submodule_shadows as _local_submodule_shadows  # noqa: E402
from submodule_shadows import shadowed_submodules as _shadowed_submodules  # noqa: E402

SUBMODULES = ("pr_facts", "turn_prompts", "records", "views", "notices", "intake", "workers", "claims",
              "issue_inputs", "ledger_schema", "waits", "merge_hold", "shadow_turns",
              "shadow_records", "shadow_filings", "retry_terms", "shadow_owing")
# ALL_SUBMODULES is every real submodule, for the shadow rules. ``turns`` is not in SUBMODULES:
# the package deliberately re-exports its state classes (_ClaimRead, _Turn, _Launch), so the
# one-home rules check it through ONE_HOME_SUBMODULES with those three exempt (#1332), and a
# local named ``turns`` shadows the module like any other (#1528).
# The package's deliberate re-exports (#758): the only names of a submodule that may also resolve
# as ``run_supervisor.<name>``. The one-home rules check every other name of every submodule,
# ``turns`` included (#1332).
RE_EXPORTS = {"turns": frozenset({"_ClaimRead", "_Turn", "_Launch"})}
ONE_HOME_SUBMODULES = SUBMODULES + ("turns",)
# Distinct getattr defaults for the RE_EXPORTS check: an unbound name on either side must never
# be "the same object" as an unbound (or None) name on the other.
_MISSING_ON_PACKAGE = object()
_MISSING_ON_SUBMODULE = object()


def _one_home_names() -> set[tuple[str, str]]:
    """(submodule, name) for each top-level name that must have that submodule as its only home:
    every name of every ONE_HOME_SUBMODULES module, minus the package's RE_EXPORTS."""
    return {(sub, name) for sub in ONE_HOME_SUBMODULES
            for name in _top_level_names(PACKAGE / f"{sub}.py") - RE_EXPORTS.get(sub, frozenset())}


def _top_level_names(path: pathlib.Path) -> set[str]:
    names = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _moved_names() -> set[str]:
    """Names that no longer resolve as ``run_supervisor.<name>``: the one-home names of the
    submodules (``turns`` included), plus the methods of every class in any submodule, minus
    what the package itself still binds (the re-exported state classes, ``Supervisor``)."""
    moved = {name for _, name in _one_home_names()}
    for path in PACKAGE.glob("*.py"):
        if path.stem in ("__init__", "__main__"):
            continue
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.ClassDef):
                moved.update(n.name for n in node.body
                             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    return {n for n in moved if not hasattr(run_supervisor, n)}


def _one_home_violations() -> list[str]:
    """One message per one-home name that the package core defines or binds again, or that its
    submodule does not bind, and per RE_EXPORTS name the package does not bind to the submodule's
    own object (#1936): the enforcement behind test_a_moved_name_has_one_home."""
    core = _top_level_names(PACKAGE / "__init__.py")
    problems = []
    for sub, exempt in sorted(RE_EXPORTS.items()):
        module = importlib.import_module(f"diaktoros.run_supervisor.{sub}")
        for name in sorted(exempt):
            # A unique default on each side, so a name missing from both, or missing from the
            # package while the submodule binds it to None, never compares identical (#2457).
            if (name not in _top_level_names(PACKAGE / f"{sub}.py")
                    or getattr(run_supervisor, name, _MISSING_ON_PACKAGE)
                    is not getattr(module, name, _MISSING_ON_SUBMODULE)):
                problems.append(f"RE_EXPORTS exempts {sub}.{name}, but run_supervisor.{name} "
                                f"does not resolve to it")
    for sub, moved in sorted(_one_home_names()):
        module = importlib.import_module(f"diaktoros.run_supervisor.{sub}")
        if moved in core:
            problems.append(f"run_supervisor/__init__.py defines {moved}: it lives in {sub}")
        if hasattr(run_supervisor, moved):
            problems.append(f"run_supervisor.{moved} resolves: call {sub}.{moved}")
        if not hasattr(module, moved):
            problems.append(f"{sub}.{moved} does not resolve")
    return problems


def _package_imports_of_moved_names(source: str) -> list[tuple[int, list[str]]]:
    """(line, names) for each ``from [diaktoros.]run_supervisor import`` in the source that
    imports a one-home name from the package instead of from its submodule."""
    moved = {name for _, name in _one_home_names()}
    found = []
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.ImportFrom)
                and node.module in ("run_supervisor", "diaktoros.run_supervisor")
                and {alias.name for alias in node.names} & moved):
            found.append((node.lineno, sorted({a.name for a in node.names} & moved)))
    return found


def _prose_names_of_moved(text: str) -> list[tuple[int, str]]:
    """(line, name) for each ``run_supervisor.<name>`` in the text that names a moved name (see
    _moved_names) where it should say ``run_supervisor.<submodule>.<name>``."""
    moved = _moved_names()
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        for name in re.findall(r"run_supervisor\.(\w+)", line):
            if name not in SUBMODULES and name in moved:
                found.append((number, name))
    return found



class Split(unittest.TestCase):
    def test_a_moved_name_has_one_home(self):
        for problem in _one_home_violations():
            with self.subTest(problem=problem):
                self.fail(problem)

    def test_the_one_home_rules_cover_the_turns_submodule(self):
        # turns' own names have one home like any submodule's (#1332); only the package's
        # deliberate re-exports of its state classes (#758) are exempt.
        names = _one_home_names()
        for name in ("ClaimAndLaunch", "_turn_pool", "_turn_role", "_cap_held", "_ci_hold_text",
                     "_nudged", "ci_wait_expired"):
            self.assertIn(("turns", name), names)
        for name in ("_ClaimRead", "_Turn", "_Launch"):
            self.assertNotIn(("turns", name), names)
            self.assertIs(getattr(run_supervisor, name), getattr(run_supervisor.turns, name))
        self.assertEqual(set(ONE_HOME_SUBMODULES), set(ALL_SUBMODULES))
        self.assertEqual(set(RE_EXPORTS), {"turns"})

    def test_the_re_exports_are_exactly_the_state_classes(self):
        # RE_EXPORTS is subtractive, so an extra exempt name silently drops a turns name from the
        # one-home rules (#1936). The exempt set is pinned to the three state classes (#758):
        # widening it means changing this line, not only the constant.
        self.assertEqual(RE_EXPORTS, {"turns": frozenset({"_ClaimRead", "_Turn", "_Launch"})})

    def test_the_one_home_guard_refuses_an_exemption_the_package_does_not_bind(self):
        # Drives the guard's enforcement (#1936): an exempt name must be one the package really
        # binds to the submodule's own object. A stale or wrong exemption is refused, so the
        # exempt set always equals what the package re-exports.
        from diaktoros.run_supervisor import turns
        with mock.patch.dict(RE_EXPORTS, {"turns": RE_EXPORTS["turns"] | {"_nudged"}}):
            self.assertIn("RE_EXPORTS exempts turns._nudged, but run_supervisor._nudged does "
                          "not resolve to it", _one_home_violations())
            with mock.patch.object(run_supervisor, "_nudged", lambda: None, create=True):
                self.assertIn("RE_EXPORTS exempts turns._nudged, but run_supervisor._nudged does "
                              "not resolve to it", _one_home_violations())
            with mock.patch.object(run_supervisor, "_nudged", turns._nudged, create=True):
                self.assertEqual(_one_home_violations(), [])
        self.assertEqual(_one_home_violations(), [])

    def test_the_one_home_guard_refuses_an_exemption_bound_on_neither_side_or_to_none(self):
        # #2457 review F1: an exempt name the package does not bind must be refused even when
        # the submodule's value is None, or when neither side binds it.
        from diaktoros.run_supervisor import turns
        refused = ("RE_EXPORTS exempts turns._ClaimRead, but run_supervisor._ClaimRead does not "
                   "resolve to it")
        with mock.patch.object(run_supervisor, "_ClaimRead"):
            delattr(run_supervisor, "_ClaimRead")
            with mock.patch.object(turns, "_ClaimRead", None):
                self.assertIn(refused, _one_home_violations())
            with mock.patch.object(turns, "_ClaimRead"):
                delattr(turns, "_ClaimRead")
                self.assertIn(refused, _one_home_violations())
        self.assertEqual(_one_home_violations(), [])

    def test_the_one_home_guard_refuses_an_exemption_that_is_not_a_submodule_definition(self):
        # #2457 review F2: an exempt name bound to the same object on both sides is still
        # refused when the submodule's source does not define it at top level.
        from diaktoros.run_supervisor import turns
        shared = object()
        with mock.patch.dict(RE_EXPORTS, {"turns": RE_EXPORTS["turns"] | {"_not_defined"}}), \
                mock.patch.object(turns, "_not_defined", shared, create=True), \
                mock.patch.object(run_supervisor, "_not_defined", shared, create=True):
            self.assertIn("RE_EXPORTS exempts turns._not_defined, but run_supervisor._not_defined "
                          "does not resolve to it", _one_home_violations())
        self.assertEqual(_one_home_violations(), [])

    def test_the_one_home_guard_refuses_a_second_binding_of_a_turns_name(self):
        # Drives the guard's enforcement itself (#1933 review): a turns function bound again on
        # the package is refused, while the deliberate re-exports (#758) stay accepted.
        from diaktoros.run_supervisor import turns
        self.assertEqual(_one_home_violations(), [])
        with mock.patch.object(run_supervisor, "ci_wait_expired", turns.ci_wait_expired,
                               create=True):
            self.assertIn("run_supervisor.ci_wait_expired resolves: call turns.ci_wait_expired",
                          _one_home_violations())
        self.assertFalse(hasattr(run_supervisor, "ci_wait_expired"))

    def test_the_import_guard_refuses_a_turns_name_from_the_package(self):
        # A turns function imported from the package is refused like any moved name; the
        # re-exported state classes may still be imported from it.
        for module in ("diaktoros.run_supervisor", "run_supervisor"):
            source = f"from {module} import ci_wait_expired, _turn_pool\n"
            self.assertEqual(_package_imports_of_moved_names(source),
                             [(1, ["_turn_pool", "ci_wait_expired"])])
        self.assertEqual(_package_imports_of_moved_names(
            "from diaktoros.run_supervisor import _ClaimRead, _Turn, _Launch\n"), [])

    def test_the_prose_guard_refuses_a_turns_function_named_on_the_package(self):
        # Prose naming run_supervisor.ci_wait_expired points at a path that does not resolve;
        # run_supervisor.turns.ci_wait_expired and the re-exported run_supervisor._Turn are fine.
        text = ("see run_supervisor.ci_wait_expired\n"
                "see run_supervisor.turns.ci_wait_expired\n"
                "see run_supervisor._Turn\n")
        self.assertEqual(_prose_names_of_moved(text), [(1, "ci_wait_expired")])

    def test_the_record_methods_live_only_in_the_mixin(self):
        # Supervisor inherits records.LedgerRecords: a method defined again in Supervisor's own
        # body would silently shadow the mixin's (and a test patching one would miss the other).
        from diaktoros.run_supervisor import Supervisor, records
        self.assertIn(records.LedgerRecords, Supervisor.__mro__)
        mixin = {name for name in vars(records.LedgerRecords) if not name.startswith("__")}
        self.assertTrue(mixin)
        self.assertFalse(mixin & set(vars(Supervisor)), "defined on both Supervisor and the mixin")

    def test_the_claim_and_turn_methods_live_only_in_their_mixin(self):
        # Same guard for turns.ClaimAndLaunch (#758): every claim and turn step has one home,
        # the mixin, and Supervisor (or the other mixin) does not define it again.
        from diaktoros.run_supervisor import Supervisor, records, turns
        self.assertIn(turns.ClaimAndLaunch, Supervisor.__mro__)
        mixin = {name for name in vars(turns.ClaimAndLaunch) if not name.startswith("__")}
        for step in ("_claim", "_claim_commit", "_run_production", "_production_turn",
                     "_pacing_hold", "_ci_hold", "_prepare_launch", "_budget_kill_error",
                     "_production_finish"):
            self.assertIn(step, mixin)
        self.assertFalse(mixin & set(vars(Supervisor)), "defined on both Supervisor and the mixin")
        self.assertFalse(mixin & set(vars(records.LedgerRecords)), "defined on both mixins")
        # The state classes live in turns; the package re-exports the same objects.
        for name in ("_ClaimRead", "_Turn", "_Launch"):
            self.assertIs(getattr(run_supervisor, name), getattr(turns, name))
            self.assertIn(name, _top_level_names(PACKAGE / "turns.py"))
            self.assertNotIn(f"class {name}", (PACKAGE / "__init__.py").read_text())

    def test_every_mixins_methods_live_only_there(self):
        # The notice, intake and worker mixins (#1349) too: each method has one home, and neither
        # Supervisor nor another mixin defines it again.
        from diaktoros.run_supervisor import Supervisor, intake, notices, records, shadow_turns
        from diaktoros.run_supervisor import turns, workers
        mixins = (records.LedgerRecords, turns.ClaimAndLaunch, shadow_turns.ShadowTurns,
                  notices.OperatorNotices, intake.RunIntake, workers.WorkerLifecycle)
        own = {cls: {n for n in vars(cls) if not n.startswith("__")} for cls in mixins}
        for cls in mixins:
            self.assertIn(cls, Supervisor.__mro__)
            self.assertTrue(own[cls])
            self.assertFalse(own[cls] & set(vars(Supervisor)), f"{cls.__name__}: also on Supervisor")
            for other in mixins:
                if other is not cls:
                    self.assertFalse(own[cls] & own[other], f"{cls.__name__} and {other.__name__}")
        self.assertIn("notify", own[notices.OperatorNotices])
        self.assertIn("submit", own[intake.RunIntake])
        self.assertIn("recover", own[workers.WorkerLifecycle])

    def test_the_shadow_steps_live_only_in_their_mixin(self):
        # The shadow's turn steps left turns at its size limit: each is defined once, in
        # shadow_turns.ShadowTurns, and Supervisor reaches that one definition.
        from diaktoros.run_supervisor import Supervisor, shadow_turns, turns
        steps = ("_claim_read_shadow", "_shadow_verdict_recorded", "_shadow_escalation",
                 "_shadow_ci_gate", "_prepare_shadow_fix_launch")
        for step in steps:
            self.assertIn(step, vars(shadow_turns.ShadowTurns))
            self.assertNotIn(step, vars(turns.ClaimAndLaunch))
            self.assertIs(getattr(Supervisor, step), vars(shadow_turns.ShadowTurns)[step])
        self.assertIn("_verdict_in", _top_level_names(PACKAGE / "shadow_turns.py"))
        self.assertNotIn("_verdict_in", _top_level_names(PACKAGE / "turns.py"))

    def test_nothing_imports_a_moved_name_from_the_package(self):
        files = [*(ROOT / "diaktoros").rglob("*.py"), *(ROOT / "scripts").glob("*.py"),
                 *(ROOT / "tests").rglob("*.py")]
        for path in files:
            for lineno, names in _package_imports_of_moved_names(path.read_text()):
                self.fail(f"{path.relative_to(ROOT)}:{lineno} imports {names} from the package")

    def test_no_function_shadows_a_submodule_it_calls(self):
        for path in PACKAGE.glob("*.py"):
            for function, sub in _local_submodule_shadows(path.read_text(), str(path)):
                with self.subTest(file=path.name, function=function, name=sub):
                    self.fail(f"a local `{sub}` shadows the submodule")

    def test_the_package_shadow_rule_flags_a_local_turns(self):
        # Drives the package-side rule on source text, so it does not depend on the package being
        # shadow-free today: with the rule filtering by SUBMODULES, `turns` slips through (#1528).
        local = "def f():\n    turns = []\n    return turns\n"
        self.assertEqual(_local_submodule_shadows(local, "x.py"), [("f", "turns")])
        argument = "def g(turns):\n    return turns\n"
        self.assertEqual(_local_submodule_shadows(argument, "x.py"), [("g", "turns")])
        nested = "def outer():\n    def inner():\n        views = 1\n        return views\n"
        self.assertEqual(_local_submodule_shadows(nested, "x.py"), [("inner", "views")])
        self.assertEqual(_local_submodule_shadows("def h():\n    return turns.x\n", "x.py"), [])
        self.assertEqual(_local_submodule_shadows("def h():\n    import turns\n", "x.py"), [])

    def test_prose_names_a_moved_function_by_its_submodule(self):
        # Docs and docstrings that write `run_supervisor.<name>` for a moved name point at a path
        # that no longer resolves (#1458): they must say `run_supervisor.<submodule>.<name>`.
        files = [*(ROOT / "docs").glob("*.md"), *(ROOT / "diaktoros").rglob("*.py")]
        for path in files:
            for number, name in _prose_names_of_moved(path.read_text()):
                self.fail(f"{path.relative_to(ROOT)}:{number} names run_supervisor.{name}, "
                          f"which moved to a submodule")

    def test_the_moved_set_holds_mixin_methods(self):
        # `run_supervisor._spawn` is a method of workers.WorkerLifecycle, so it is as unresolvable
        # as a moved function (#1663); the package's own bindings stay out of the set.
        moved = _moved_names()
        for method in ("_spawn", "_heartbeat", "_worker_log", "_claim"):
            self.assertIn(method, moved)
        self.assertNotIn("Supervisor", moved)
        self.assertNotIn("_Turn", moved)

    def test_a_test_does_not_shadow_a_submodule_it_imports(self):
        # The package's own shadow rule, applied to the tests (#1458): a function that binds a
        # local named like a submodule its file imports turns a later `views.<name>` into an
        # UnboundLocalError.
        for path in (ROOT / "tests").rglob("*.py"):
            for lineno, name in _shadowed_submodules(path.read_text(), str(path)):
                self.fail(f"{path.relative_to(ROOT)}:{lineno} binds a local `{name}` that "
                          f"shadows the imported submodule")

    def test_the_shadow_check_flags_a_local_that_reuses_an_imported_submodule(self):
        # Drives the check on source text, so it does not depend on what the tests/ files hold:
        # the old test_partial_view_no_approve.py:597 shape must be reported.
        shadowing = (
            "from diaktoros.run_supervisor import views\n"
            "def test_x():\n"
            "    views = [line for line in ()]\n"
            "    return views\n")
        self.assertEqual(_shadowed_submodules(shadowing), [(3, "views")])
        as_argument = ("from diaktoros.run_supervisor import views\n"
                       "def helper(views):\n    return views\n")
        self.assertEqual(_shadowed_submodules(as_argument), [(2, "views")])
        clean = ("from diaktoros.run_supervisor import views\n"
                 "def test_x():\n    view_lines = []\n    return views.next_step, view_lines\n")
        self.assertEqual(_shadowed_submodules(clean), [])
        self.assertEqual(_shadowed_submodules("def test_x():\n    views = []\n"), [])

    def test_the_shadow_check_covers_the_turns_submodule(self):
        # turns is a real submodule that SUBMODULES leaves out (its state classes are re-exported
        # on purpose); the shadow check must still see it (#1528).
        self.assertNotIn("turns", SUBMODULES)
        self.assertEqual(set(SUBMODULES) | {"turns"}, set(ALL_SUBMODULES))
        shadowing = ("from diaktoros.run_supervisor import turns\n"
                     "def f():\n    turns = []\n    return turns\n")
        self.assertEqual(_shadowed_submodules(shadowing), [(3, "turns")])
        as_argument = ("from diaktoros.run_supervisor import turns\n"
                       "def f(turns):\n    return turns\n")
        self.assertEqual(_shadowed_submodules(as_argument), [(2, "turns")])

    def test_the_shadow_check_flags_every_binding_shape(self):
        # #1665/#1664: except-as, with-as, module-level rebinding, aliased imports, imports
        # inside a function and `import diaktoros.run_supervisor.views as views` all shadow.
        imp = "from diaktoros.run_supervisor import views\n"
        cases = {
            "except_as": (imp + "def t():\n    try:\n        pass\n"
                          "    except Exception as views:\n        return views\n", [(5, "views")]),
            "with_as": (imp + "def t():\n    with open('x') as views:\n        pass\n",
                        [(3, "views")]),
            "module_level": (imp + "views = []\n", [(2, "views")]),
            "import_alias": ("from diaktoros.run_supervisor import views as v\n"
                             "def t():\n    v = 1\n    return v\n", [(3, "v")]),
            "import_in_function": (imp + "def t():\n    import json as views\n    return views\n",
                                   [(3, "views")]),
            "from_import_in_function": (
                imp + "def t():\n    from json import dumps as views\n    return views\n",
                [(3, "views")]),
            "alias_import_in_function": (
                "def t():\n    from diaktoros.run_supervisor import views as v\n    return v\n"
                "def u():\n    v = 2\n    return v\n", [(5, "v")]),
            "dotted_import": ("import diaktoros.run_supervisor.views as views\n"
                              "def t():\n    views = 1\n    return views\n", [(3, "views")]),
            "match_capture": (imp + "def t(x):\n    match x:\n        case [*views]:\n"
                              "            return views\n", [(4, "views")]),
            "comprehension": (imp + "def t():\n    return [views for views in ()]\n",
                              [(3, "views")]),
            "global_rebinding": (imp + "def t():\n    global views\n    views = 1\n",
                                 [(4, "views")]),
        }
        for label, (source, expected) in cases.items():
            with self.subTest(shape=label):
                self.assertEqual(_shadowed_submodules(source), expected)
        # Importing the submodule itself, at any scope or alias, is not a shadow.
        clean = ("import diaktoros.run_supervisor.views as views\n"
                 "from diaktoros.run_supervisor import turns as t\n"
                 "def f():\n    from diaktoros.run_supervisor import views\n"
                 "    return views.x, t.y\n")
        self.assertEqual(_shadowed_submodules(clean), [])

    def test_a_module_level_comprehension_does_not_shadow(self):
        # #2509 F1: a comprehension has its own scope, so its target at module level leaves the
        # imported module bound; only a comprehension inside a function counts (shape
        # `comprehension` above).
        source = ("from diaktoros.run_supervisor import views\n"
                  "items = [views for views in ()]\n"
                  "pairs = {views: 1 for views in ()}\n"
                  "first = next((views for views in ()), None)\n"
                  "def t():\n    return views.next_step\n")
        self.assertEqual(_shadowed_submodules(source), [])
        # Its iterable and element still belong to the module: a walrus there does rebind it.
        walrus = ("from diaktoros.run_supervisor import views\n"
                  "items = [(views := x) for x in ()]\n"
                  "more = [x for x in () if (views := x)]\n")
        self.assertEqual(_shadowed_submodules(walrus), [(2, "views"), (3, "views")])

    def test_a_read_before_a_local_reimport_is_a_shadow(self):
        # #2509 F2: a function-local import makes the name local to the whole function, so a
        # read before it raises UnboundLocalError although the module imports the same module.
        for imp in ("from diaktoros.run_supervisor import views as v\n",
                    "import diaktoros.run_supervisor.views as v\n"):
            for local in ("    from diaktoros.run_supervisor import views as v\n",
                          "    import diaktoros.run_supervisor.views as v\n"):
                with self.subTest(module=imp, local=local):
                    read_first = imp + "def f():\n    print(v)\n" + local + "    return v\n"
                    self.assertEqual(_shadowed_submodules(read_first), [(3, "v")])
                    namespace = {}
                    exec(compile(read_first, "<f>", "exec"), namespace)
                    with self.assertRaises(UnboundLocalError):
                        namespace["f"]()
                    read_after = imp + "def f():\n" + local + "    return v\n"
                    self.assertEqual(_shadowed_submodules(read_after), [])
        plain = ("from diaktoros.run_supervisor import views\n"
                 "def f():\n    print(views)\n    from diaktoros.run_supervisor import views\n")
        self.assertEqual(_shadowed_submodules(plain), [(3, "views")])

    def test_the_package_shadow_rule_flags_a_read_before_a_local_import(self):
        # The package-side rule agrees with the tests/ rule on #2509 F2: a local import of the
        # submodule is allowed only when nothing in the function reads it first.
        early = "def f():\n    x = turns.a\n    from . import turns\n    return x\n"
        self.assertEqual(_local_submodule_shadows(early, "x.py"), [("f", "turns")])
        late = "def f():\n    from . import turns\n    return turns.a\n"
        self.assertEqual(_local_submodule_shadows(late, "x.py"), [])

    def test_a_read_after_an_import_that_may_not_run_is_a_shadow(self):
        # #2509 round 2 F2: an import nested in an `if` (or try/with/loop) may never run, so a
        # read after it in the text can still find the local unbound. Both rules flag it; an
        # import that is a statement of the function body itself still binds every later read.
        imp = "from diaktoros.run_supervisor import views\n"
        local = "from diaktoros.run_supervisor import views\n"
        for opener in ("    if False:\n", "    for _ in ():\n", "    try:\n"):
            closer = "    except ImportError:\n        pass\n" if opener.startswith("    try") else ""
            source = imp + "def f():\n" + opener + "        " + local + closer + "    return views\n"
            with self.subTest(block=opener.strip()):
                last = len(source.splitlines())
                self.assertEqual(_shadowed_submodules(source), [(last, "views")])
                self.assertEqual(_local_submodule_shadows(source, "x.py"), [("f", "views")])
                if opener.startswith("    if") or opener.startswith("    for"):
                    namespace = {}
                    exec(compile(source, "<f>", "exec"), namespace)
                    with self.assertRaises(UnboundLocalError):
                        namespace["f"]()
        straight = imp + "def f():\n    if True:\n        pass\n    " + local + "    return views\n"
        self.assertEqual(_shadowed_submodules(straight), [])
        self.assertEqual(_local_submodule_shadows(straight, "x.py"), [])

    def test_a_read_in_the_else_of_a_try_that_imports_it_is_not_a_shadow(self):
        # #2522: a `try`'s `else` runs only after its body finished, so the body's own import has
        # bound the name there. An import nested deeper in the body may not have run.
        head = "from diaktoros.run_supervisor import views\ndef f(c):\n    try:\n"
        tail = "    except ImportError:\n        return None\n    else:\n        return views\n"
        safe = head + "        from diaktoros.run_supervisor import views\n" + tail
        self.assertEqual(_shadowed_submodules(safe), [])
        self.assertEqual(_local_submodule_shadows(safe, "x.py"), [])
        namespace = {}
        exec(compile(safe, "<f>", "exec"), namespace)
        self.assertEqual(namespace["f"](True).__name__, "diaktoros.run_supervisor.views")
        nested = head + "        if c:\n            from diaktoros.run_supervisor import views\n" + tail
        self.assertEqual(_shadowed_submodules(nested), [(len(nested.splitlines()), "views")])
        self.assertEqual(_local_submodule_shadows(nested, "x.py"), [("f", "views")])
        # #2536 review F6: a ``del`` in the body unbinds it again, so the else's read raises.
        deleted = (head + "        from diaktoros.run_supervisor import views\n"
                   "        del views\n" + tail)
        self.assertEqual(_shadowed_submodules(deleted), [(len(deleted.splitlines()), "views")])
        self.assertEqual(_local_submodule_shadows(deleted, "x.py"), [("f", "views")])
        namespace = {}
        exec(compile(deleted, "<f>", "exec"), namespace)
        with self.assertRaises(UnboundLocalError):
            namespace["f"](True)

    def test_a_del_in_the_else_of_a_try_that_imports_it_unbinds_later_reads(self):
        # #2536 review round 5 F6: the else is bound by the body's import only until a ``del`` in
        # the else itself; a read from that statement on raises. A read before it is still bound.
        head = ("from diaktoros.run_supervisor import views\ndef f():\n    try:\n"
                "        from diaktoros.run_supervisor import views\n"
                "    except ImportError:\n        return None\n    else:\n")
        deleted = head + "        del views\n        return views\n"
        self.assertEqual(_shadowed_submodules(deleted), [(len(deleted.splitlines()), "views")])
        self.assertEqual(_local_submodule_shadows(deleted, "x.py"), [("f", "views")])
        namespace = {}
        exec(compile(deleted, "<f>", "exec"), namespace)
        with self.assertRaises(UnboundLocalError):
            namespace["f"]()
        before = head + "        name = views.__name__\n        del views\n        return name\n"
        self.assertEqual(_shadowed_submodules(before), [])
        self.assertEqual(_local_submodule_shadows(before, "x.py"), [])
        namespace = {}
        exec(compile(before, "<f>", "exec"), namespace)
        self.assertEqual(namespace["f"](), "diaktoros.run_supervisor.views")

    def test_an_except_as_of_the_name_unbinds_a_try_else(self):
        # #2536 review round 6 F6: Python deletes an ``except ... as`` target when the handler
        # ends, so one named like the import, in the try body or in the else, leaves the else's
        # later read unbound. A handler bound to another name keeps the import.
        imp = "from diaktoros.run_supervisor import views\n"
        def source(body, orelse):
            return (imp + "def f():\n    try:\n        " + imp + body
                    + "    except ImportError:\n        return None\n    else:\n" + orelse)
        catch = "{0}try:\n{0}    raise ValueError()\n{0}except ValueError as {1}:\n{0}    pass\n"
        cases = {"body": source(catch.format(" " * 8, "views"), "        return views\n"),
                 "else": source("", catch.format(" " * 8, "views") + "        return views\n")}
        for where, unbound in cases.items():
            with self.subTest(where=where):
                self.assertIn((len(unbound.splitlines()), "views"), _shadowed_submodules(unbound))
                self.assertEqual(_local_submodule_shadows(unbound, "x.py"), [("f", "views")])
                namespace = {}
                exec(compile(unbound, "<f>", "exec"), namespace)
                with self.assertRaises(UnboundLocalError):
                    namespace["f"]()
        other = source(catch.format(" " * 8, "err"), catch.format(" " * 8, "err")
                       + "        return views\n")
        self.assertEqual(_shadowed_submodules(other), [])
        self.assertEqual(_local_submodule_shadows(other, "x.py"), [])
        namespace = {}
        exec(compile(other, "<f>", "exec"), namespace)
        self.assertEqual(namespace["f"]().__name__, "diaktoros.run_supervisor.views")

    def test_an_unbinding_in_a_nested_scope_counts_only_under_nonlocal(self):
        # #2536 review round 7 F7: a nested function's ``except ... as`` or ``del`` unbinds its own
        # (or a global) name, not the enclosing import, unless it declares the name nonlocal.
        imp = "from diaktoros.run_supervisor import views\n"
        def source(decl, unbind):
            return (imp + "def f():\n    try:\n        " + imp + "        def inner():\n"
                    + f"            {decl} views\n" + unbind + "        inner()\n"
                    + "    except ImportError:\n        return None\n    else:\n        return views\n")
        catch = ("            try:\n                raise ValueError()\n"
                 "            except ValueError as views:\n                pass\n")
        for unbind in (catch, "            views = 1\n            del views\n"):
            with self.subTest(unbind=unbind.split()[0], decl="global"):
                safe = source("global", unbind)
                self.assertNotIn((len(safe.splitlines()), "views"), _shadowed_submodules(safe))
                self.assertEqual(_local_submodule_shadows(safe, "x.py"), [])
                namespace = {}
                exec(compile(safe, "<f>", "exec"), namespace)
                self.assertEqual(namespace["f"]().__name__, "diaktoros.run_supervisor.views")
            with self.subTest(unbind=unbind.split()[0], decl="nonlocal"):
                unbound = source("nonlocal", unbind)
                self.assertIn((len(unbound.splitlines()), "views"), _shadowed_submodules(unbound))
                self.assertEqual(_local_submodule_shadows(unbound, "x.py"), [("f", "views")])
                namespace = {}
                exec(compile(unbound, "<f>", "exec"), namespace)
                with self.assertRaises(NameError):
                    namespace["f"]()

    def test_a_nonlocal_unbinding_reaches_through_any_depth_of_nested_scope(self):
        # #2536 review round 8 F8: a deeper function's ``nonlocal`` reaches the enclosing import
        # through an intermediate function that does not bind the name itself, and a method's
        # through its class. A middle function that binds it takes the nonlocal for its own.
        imp = "from diaktoros.run_supervisor import views\n"
        def source(nested, call):
            return (imp + "def f():\n    try:\n        " + imp + nested + call
                    + "    except ImportError:\n        return None\n    else:\n        return views\n")
        inner = ("            def inner():\n                nonlocal views\n"
                 "                del views\n            inner()\n")
        cases = {
            "chain": (source("        def middle():\n" + inner, "        middle()\n"), True),
            "class": (source("        class C:\n            def m(self):\n"
                             "                nonlocal views\n                del views\n",
                             "        C().m()\n"), True),
            "middle binds": (source("        def middle():\n            views = 1\n" + inner,
                                    "        middle()\n"), False)}
        for case, (code, unbound) in cases.items():
            with self.subTest(case=case):
                last = (len(code.splitlines()), "views")
                namespace = {}
                exec(compile(code, "<f>", "exec"), namespace)
                if unbound:
                    self.assertIn(last, _shadowed_submodules(code))
                    self.assertEqual(_local_submodule_shadows(code, "x.py"), [("f", "views")])
                    with self.assertRaises(NameError):
                        namespace["f"]()
                else:   # middle's own ``views = 1`` is still a shadow; f's else read is not
                    self.assertNotIn(last, _shadowed_submodules(code))
                    self.assertEqual(_local_submodule_shadows(code, "x.py"), [("middle", "views")])
                    self.assertEqual(namespace["f"]().__name__, "diaktoros.run_supervisor.views")

    def test_a_function_named_like_a_comprehension_scope_is_checked(self):
        # #2509 round 2 F3: before Python 3.12 symtable names a comprehension scope `listcomp`
        # (etc.); a real function of that name is still a function and is checked.
        for name in ("listcomp", "setcomp", "dictcomp", "genexpr"):
            with self.subTest(name=name):
                source = ("from diaktoros.run_supervisor import views\n"
                          f"def {name}():\n    print(views)\n    views = 1\n")
                self.assertEqual(_shadowed_submodules(source), [(4, "views")])
                self.assertEqual(_local_submodule_shadows(source, "x.py"), [(name, "views")])

    def test_a_global_reimport_after_a_read_is_not_a_shadow(self):
        # #2509 round 2 F4: under `global`, a later import rebinds the module's name; the read
        # before it finds the module's import, so it runs. Only a local import leaves it unbound.
        source = ("from diaktoros.run_supervisor import views\n"
                  "def f():\n    global views\n    print(views)\n"
                  "    from diaktoros.run_supervisor import views\n    return views\n")
        self.assertEqual(_shadowed_submodules(source), [])
        self.assertEqual(_local_submodule_shadows(source, "x.py"), [])
        namespace = {}
        exec(compile(source.replace("print(views)", "views.__name__"), "<f>", "exec"), namespace)
        namespace["f"]()

    def test_a_base_proof_tests_the_base_shadow_rules(self):
        # #2509 round 2 F5: the rules live off the test paths, so the host's base-failure proof
        # (change_proof.apply_tests) runs the head's tests above against the base's rules. It
        # fails if the rules go back into a tests/ module, where they were.
        import _base_proof
        _base_proof.assert_taken_from_the_base(self, submodule_shadows.__file__)


if __name__ == "__main__":
    unittest.main()
