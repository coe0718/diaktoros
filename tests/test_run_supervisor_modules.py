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
import symtable
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import run_supervisor  # noqa: E402

PACKAGE = pathlib.Path(run_supervisor.__file__).resolve().parent
ROOT = PACKAGE.parents[1]
SUBMODULES = ("pr_facts", "turn_prompts", "records", "views", "notices", "intake", "workers", "claims",
              "issue_inputs", "ledger_schema", "waits", "merge_hold")
# Every real submodule, for the shadow rules. ``turns`` is not in SUBMODULES: the package
# deliberately re-exports its state classes (_ClaimRead, _Turn, _Launch), so the one-home rules
# check it through ONE_HOME_SUBMODULES with those three exempt (#1332), and a local named
# ``turns`` shadows the module like any other (#1528).
ALL_SUBMODULES = tuple(sorted(
    p.stem for p in PACKAGE.glob("*.py") if p.stem not in ("__init__", "__main__")))
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


def _local_submodule_shadows(source: str, filename: str) -> list[tuple[str, str]]:
    """(function, name) for each function in the source with a local binding named like any
    run_supervisor submodule: one not imported, or a local import of it that the function reads
    the name before (#2509), which raises UnboundLocalError."""
    found = []
    functions = _function_nodes(ast.parse(source))

    def walk(table):
        for child in table.get_children():
            if child.get_type() == "function":
                node = functions.get((child.get_name(), child.get_lineno()))
                for sub in ALL_SUBMODULES:
                    symbol = child.lookup(sub) if sub in child.get_identifiers() else None
                    if symbol is None or not symbol.is_local():
                        continue
                    if not symbol.is_imported() or (
                            node is not None and _read_before_local_import(node, sub) is not None):
                        found.append((child.get_name(), sub))
            walk(child)
    walk(symtable.symtable(source, filename, "exec"))
    return found


def _submodule_import(node: ast.AST, alias: ast.alias) -> bool:
    """Whether this import alias binds a run_supervisor submodule: ``from
    diaktoros.run_supervisor import views [as v]`` or ``import diaktoros.run_supervisor.views as
    v`` (#1665)."""
    if isinstance(node, ast.ImportFrom):
        return node.module == "diaktoros.run_supervisor" and alias.name in ALL_SUBMODULES
    prefix = "diaktoros.run_supervisor."
    return (alias.asname is not None and alias.name.startswith(prefix)
            and alias.name[len(prefix):] in ALL_SUBMODULES)


def _submodule_aliases(tree: ast.AST) -> set[str]:
    """Every name the source binds to a run_supervisor submodule by an import, at any scope and
    under any alias."""
    return {a.asname or a.name for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for a in node.names if _submodule_import(node, a)}


# Symtable names of the comprehension scopes. Before Python 3.12 (PEP 709) every comprehension
# is a child "function" scope of this name; from 3.12 only a generator expression still is.
_COMPREHENSION_SCOPES = ("listcomp", "setcomp", "dictcomp", "genexpr")


def _scope_nodes(scope: ast.AST):
    """Each AST node that belongs directly to one module or function scope. Nested function,
    lambda and class bodies are their own scopes and are not entered; their decorators, bases
    and defaults are. At module level a comprehension's own targets are skipped as well: Python
    gives a comprehension its own scope, so ``[views for views in ()]`` there rebinds nothing in
    the module (#2509). Inside a function they are kept, as the package guard and this guard
    before #1665 count them."""
    module = isinstance(scope, ast.Module)
    stack = list(scope.body) if isinstance(scope.body, list) else [scope.body]
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.extend(node.decorator_list)
            if isinstance(node, ast.ClassDef):
                stack.extend([*node.bases, *node.keywords])
            else:
                stack.extend([*node.args.defaults, *(d for d in node.args.kw_defaults if d)])
        elif isinstance(node, ast.Lambda):
            stack.extend([*node.args.defaults, *(d for d in node.args.kw_defaults if d)])
        elif module and isinstance(node, ast.comprehension):
            stack.extend([node.iter, *node.ifs])
        else:
            stack.extend(ast.iter_child_nodes(node))


def _scope_bindings(scope: ast.AST) -> list[tuple[int, str, bool]]:
    """(line, name, is_submodule_import) for each name bound directly in a module or function
    scope (see _scope_nodes for what belongs to it): assignment, for/with/walrus/comprehension
    targets, arguments, ``except ... as``, imports, def/class names and match captures."""
    found = []
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = scope.args
        for arg in (*args.posonlyargs, *args.args, args.vararg, *args.kwonlyargs, args.kwarg):
            if arg is not None:
                found.append((arg.lineno, arg.arg, False))
    for node in _scope_nodes(scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append((node.lineno, node.name, False))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            found.append((node.lineno, node.id, False))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            found.append((node.lineno, node.name, False))
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            found.append((node.lineno, node.name, False))
        elif isinstance(node, ast.MatchMapping) and node.rest:
            found.append((node.lineno, node.rest, False))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                found.append((node.lineno, a.asname or a.name.split(".")[0],
                              _submodule_import(node, a)))
    return found


def _read_before_local_import(func: ast.AST, name: str) -> int | None:
    """The line of the first read of ``name`` in this function that comes before the function's
    first import of that name, or None (#2509). A local import makes the name local to the whole
    function, so such a read raises UnboundLocalError even when the module imports the very same
    submodule. This is source order, not control flow: a read that follows the import in the
    text but runs first (a later loop iteration) is not seen."""
    nodes = list(_scope_nodes(func))
    imports = [(n.lineno, n.col_offset) for n in nodes
               if isinstance(n, (ast.Import, ast.ImportFrom))
               and any((a.asname or a.name.split(".")[0]) == name for a in n.names)]
    if not imports:
        return None
    early = [(n.lineno, n.col_offset) for n in nodes
             if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id == name
             and (n.lineno, n.col_offset) < min(imports)]
    return min(early)[0] if early else None


def _function_nodes(tree: ast.AST) -> dict[tuple[str, int], ast.AST]:
    """Each function and lambda of the tree, by the (name, line) symtable gives its scope."""
    return {("lambda" if isinstance(n, ast.Lambda) else n.name, n.lineno): n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))}


def _shadowed_submodules(source: str, filename: str = "<source>") -> list[tuple[int, str]]:
    """(line, name) for each binding, at module level or in a function, of a name the source
    imports as a run_supervisor submodule, other than such an import itself (#1458, #1665), and
    for each read of such a name in a function before that function's own import of it (#2509).

    Function locals come from symtable, as in _local_submodule_shadows, so every binding shape
    counts; _scope_bindings only supplies the line and tells a submodule import from any other
    binding of the name. A local symtable reports that has no binding line found is reported at
    the function's own line. A comprehension at module level is its own scope and rebinds
    nothing in the module (#2509); one inside a function counts as part of that function."""
    tree = ast.parse(source)
    aliases = _submodule_aliases(tree)
    if not aliases:
        return []
    functions = _function_nodes(tree)
    found = {(line, name) for line, name, submodule in _scope_bindings(tree)
             if name in aliases and not submodule}

    def walk(table, in_function):
        for child in table.get_children():
            if child.get_type() != "function":
                walk(child, in_function)
                continue
            if child.get_name() in _COMPREHENSION_SCOPES and not in_function:
                walk(child, False)      # before Python 3.12: a module-level comprehension
                continue
            node = functions.get((child.get_name(), child.get_lineno()))
            found.update(_function_shadows(child, node, aliases))
            walk(child, True)
    walk(symtable.symtable(source, filename, "exec"), False)
    return sorted(found)


def _function_shadows(table, node, aliases) -> set[tuple[int, str]]:
    """(line, name) for each name of ``aliases`` that one function scope (its symtable and, when
    found, its AST node) rebinds, or reads before its own import of it (#2509)."""
    bindings = _scope_bindings(node) if node is not None else []
    found = set()
    for name in aliases & set(table.get_identifiers()):
        symbol = table.lookup(name)
        # A `global`/`nonlocal` name assigned here rebinds the import as well.
        if not (symbol.is_local() or symbol.is_declared_global() or symbol.is_nonlocal()):
            continue
        hits = {(line, n) for line, n, submodule in bindings if n == name and not submodule}
        if not hits and symbol.is_local() and not symbol.is_imported():
            hits = {(table.get_lineno(), name)}
        early = _read_before_local_import(node, name) if node is not None else None
        if early is not None:
            hits.add((early, name))
        found.update(hits)
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
        from diaktoros.run_supervisor import Supervisor, intake, notices, records, turns, workers
        mixins = (records.LedgerRecords, turns.ClaimAndLaunch, notices.OperatorNotices,
                  intake.RunIntake, workers.WorkerLifecycle)
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


if __name__ == "__main__":
    unittest.main()
