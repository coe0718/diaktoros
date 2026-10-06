#!/usr/bin/env python3
"""The tests that depend on a change, for a fixer turn that cannot run the whole suite (#362).

A fix turn has a fixed budget and this suite takes minutes, so the fixer used to run only the
tests it touched, and CI went red on tests elsewhere that depended on what it changed (#336,
#337, #343, #356, #358). Given the changed paths, this picks:

* the changed test files themselves;
* every ``tests/test_*.py`` that imports a changed ``review_loop`` module, directly or through
  another ``review_loop`` module that imports it (direct importers first);
* the tests that load a changed gate script, or a changed test helper;
* always ``test_home_guard``, and the harness (``run_tests.py``) for any code, script or docs
  change: both are suite-wide. The harness runs right after the direct dependents.

``python3 tests/affected.py PATH…`` lists them; ``--run`` runs them one module at a time and
stops starting new ones past ``--budget`` seconds (default 360), naming what it skipped — CI runs
everything. The loop's fixer prompt passes the changed paths as ``$CHANGED``:
``python3 tests/affected.py --run $CHANGED``.
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
PACKAGE = ROOT / "review_loop"
ALWAYS = ("test_home_guard",)
HARNESS = "run_tests"
DOC_TESTS = ("test_commands_doc",)


def _module_imports(path: Path) -> set[str]:
    """The ``review_loop`` modules a ``review_loop`` module imports (``from . import x`` too)."""
    found: set[str] = set()
    try:
        tree = ast.parse(path.read_text())
    except (OSError, SyntaxError, ValueError):
        return found
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level == 1 and not base:
                found.update(alias.name for alias in node.names)
            elif node.level == 1:
                found.add(base.split(".")[0])
            elif base == "review_loop":
                found.update(alias.name for alias in node.names)
            elif base.startswith("review_loop."):
                found.add(base.split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("review_loop."):
                    found.add(alias.name.split(".")[1])
    return found


def _test_mentions(path: Path) -> set[str]:
    """The ``review_loop`` modules a test file uses: imports and ``"review_loop.x…"`` patch
    targets alike."""
    text = path.read_text(errors="replace")
    found = set(re.findall(r"\breview_loop\.(\w+)", text))
    for match in re.finditer(r"^\s*from review_loop import \(?([^)\n]*(?:\n[^)\n]*)*?)\)?\s*(?:#|$)",
                             text, re.M):
        found.update(name.strip().split(" as ")[0] for name in match.group(1).split(",")
                     if name.strip())
    return found


def _loads_script(text: str, script: str) -> bool:
    """Whether a test runs or imports a gate script: by file name (``"watchdog.py"``) or as a
    module (``from scripts import watchdog``, ``scripts.watchdog``) — #378's lock broke tests that
    import it the second way."""
    stem = re.escape(script[:-3] if script.endswith(".py") else script)
    return bool(re.search(rf"\b{stem}\.py\b|\bscripts\.{stem}\b"
                          rf"|^\s*from scripts import [^\n]*\b{stem}\b", text, re.M))


def dependents(changed_modules: set[str]) -> list[set[str]]:
    """Layers of ``review_loop`` modules: the changed ones, then each wave that imports them."""
    graph = {path.stem: _module_imports(path) for path in PACKAGE.glob("*.py")}
    seen = set(changed_modules)
    layers = [set(changed_modules)]
    while True:
        wave = {name for name, imports in graph.items() if name not in seen and imports & layers[-1]}
        if not wave:
            return layers
        seen |= wave
        layers.append(wave)


def select(changed: list[str]) -> list[str]:
    """The test modules to run, in order: most direct first, the harness last."""
    tests = sorted(path.stem for path in TESTS.glob("test_*.py"))
    mentions = {name: _test_mentions(TESTS / f"{name}.py") for name in tests}
    texts = {name: (TESTS / f"{name}.py").read_text(errors="replace") for name in tests}
    order: list[str] = []

    def add(names):
        for name in names:
            if name not in order:
                order.append(name)

    modules, scripts, helpers, harness, docs = set(), set(), set(), False, False
    for raw in changed:
        path = Path(raw.strip().lstrip("./"))
        if not raw.strip():
            continue
        parts = path.parts
        if parts[:1] == ("review_loop",) and path.suffix == ".py":
            modules.add(path.stem)
        elif parts[:1] == ("scripts",) and path.suffix == ".py":
            scripts.add(path.name)
        elif parts[:1] == ("tests",) and path.name.startswith("test_") and path.suffix == ".py":
            if path.stem in mentions:
                add([path.stem])
        elif parts[:1] == ("tests",) and (path.name.startswith("_") or "harness" in parts
                                          or path.stem == HARNESS):
            if path.name.startswith("_") and path.suffix == ".py":
                helpers.add(path.stem)
            harness = True
        elif path.suffix == ".md" or path.name in ("plugin.yaml", "README.md"):
            docs = True
    add(ALWAYS)
    layers = dependents(modules) if modules else []
    if layers:
        add(name for name in tests if mentions[name] & layers[0])
    add(name for name in tests if any(_loads_script(texts[name], script) for script in scripts))
    add(name for name in tests if any(re.search(rf"^\s*import {re.escape(helper)}\b", texts[name],
                                                re.M) for helper in helpers))
    if docs:
        add(DOC_TESTS)
    # The harness is suite-wide (the gates, the CLI, the docs' commands): right after the direct
    # dependents, before the wide transitive layers a budget may cut.
    if harness or modules or scripts or docs:
        add([HARNESS])
    for layer in layers[1:]:
        add(name for name in tests if mentions[name] & layer)
    return order


def run(order: list[str], budget: float, clock=time.monotonic) -> int:
    start, failed, skipped = clock(), [], []
    for name in order:
        if clock() - start > budget:
            skipped.append(name)
            continue
        # Run as CI does: from the repository root, by discovery. Some tests start
        # `python -m review_loop...` in a subprocess, which only resolves from the root.
        if name == HARNESS:
            command = [sys.executable, str(TESTS / "run_tests.py")]
        else:
            command = [sys.executable, "-m", "unittest", "discover", "-q", "-s", "tests",
                       "-p", f"{name}.py"]
        print(f"== {name}", flush=True)
        done = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        # unittest exits 5 when a module holds no tests (a helper named test_*): not a failure.
        if done.returncode and done.returncode != 5:
            failed.append(name)
            print((done.stdout + done.stderr)[-3000:], flush=True)
    print(f"\nran {len(order) - len(skipped)} of {len(order)}: "
          + (f"FAILED {', '.join(failed)}" if failed else "all passed"))
    if skipped:
        shown = ", ".join(skipped[:15]) + (f" and {len(skipped) - 15} more" if len(skipped) > 15 else "")
        print(f"not run (past the {int(budget)} s budget; CI runs them): {shown}")
    return 1 if failed else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="*", help="changed paths, relative to the repository root")
    parser.add_argument("--run", action="store_true", help="run them instead of listing them")
    parser.add_argument("--budget", type=float, default=360,
                        help="seconds after which no new module starts (default 360)")
    args = parser.parse_args(argv)
    paths = [part for raw in args.paths for part in raw.split()]
    order = select(paths)
    if not args.run:
        print("\n".join(order))
        return 0
    return run(order, args.budget)


if __name__ == "__main__":
    sys.exit(main())
