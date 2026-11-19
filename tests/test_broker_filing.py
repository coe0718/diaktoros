"""The broker filing split (#1349 shape, #2117): the issues the broker files live in
``broker.filing``, and nowhere else.

``filing`` reads what it shares with the broker's other writes (``_write``, ``_audit``,
``_signed``, ``_denied``, ``BrokerDenied``) through the ``broker`` module at call time, so a
``mock.patch.object(broker, ...)`` still reaches the moved writes. Pinned here: a moved name is
not also defined in ``broker`` (one home for the filing bounds the prompts and the enforcement
must agree on), ``filing`` imports none of ``broker``'s names by name, either module may be
imported first, and a patch on ``broker._write`` / ``broker._audit`` reaches ``file_issue`` and
``file_shadow_issue``.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import broker  # noqa: E402
from diaktoros.broker import filing as broker_filing  # noqa: E402

CORE = ROOT / "diaktoros" / "broker" / "__init__.py"
MOVED = ROOT / "diaktoros" / "broker" / "filing.py"
REPO, HEAD = "acme/widgets", "a" * 40


def _defined(tree: ast.Module) -> set[str]:
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


class Split(unittest.TestCase):
    def test_the_filing_names_have_one_home(self):
        moved = _defined(ast.parse(MOVED.read_text()))
        core = _defined(ast.parse(CORE.read_text()))
        self.assertTrue({"FILED_ISSUES_MAX", "FILED_ISSUE_BODY_MAX", "file_issue",
                         "file_shadow_issue", "file_ruling_issue", "_shadow_labels"} <= moved)
        for name in sorted(moved):
            with self.subTest(name=name):
                self.assertFalse(name in core, f"{name} defined in both modules")
                self.assertFalse(hasattr(broker, name), f"call broker_filing.{name}")

    def test_filing_imports_no_broker_name_by_itself(self):
        for node in ast.walk(ast.parse(MOVED.read_text())):
            if not isinstance(node, ast.ImportFrom):
                continue
            # filing.py is diaktoros/broker/filing.py: resolve the import to an absolute module.
            base = ["diaktoros", "broker"][:max(0, 3 - node.level)] if node.level else []
            module = ".".join([*base, *(node.module or "").split(".")]).strip(".")
            if module == "diaktoros.broker":
                self.fail(f"broker/filing.py:{node.lineno} imports "
                          f"{[a.name for a in node.names]} from broker by name")

    def test_either_module_may_be_imported_first(self):
        for first in ("diaktoros.broker.filing", "diaktoros.broker"):
            with self.subTest(first=first):
                probe = (f"import {first}\nfrom diaktoros import broker\n"
                         "from diaktoros.broker import filing\n"
                         "print(filing.file_issue.__module__, filing.broker is broker)")
                out = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, text=True,
                                     capture_output=True, timeout=60)
                self.assertEqual(out.stdout.strip(), "diaktoros.broker.filing True", out.stderr)

    def test_a_patch_on_broker_reaches_the_moved_writes(self):
        loop = {"repo": REPO, "read_token": "reader"}
        issue = {"number": 101, "labels": [{"name": "bug"}, {"name": "shadow-found"}]}
        with mock.patch.object(broker, "_write", return_value=(issue, "")) as write, \
                mock.patch.object(broker, "_audit") as audit, \
                mock.patch.object(broker, "_signed", side_effect=lambda _l, text, *_a: text):
            self.assertEqual(broker_filing.file_issue(
                loop, repo=REPO, number=7, head=HEAD, login="review", title="t", body="b",
                labels=["bug"], depth=1), 101)
            self.assertEqual(broker_filing.file_shadow_issue(
                loop, repo=REPO, number=7, head=HEAD, title="t", body="b", labels=["bug"],
                model="m", depth=1), (101, ""))
        self.assertEqual([c.args[1] for c in write.call_args_list],
                         [f"/repos/{REPO}/issues"] * 2)
        self.assertEqual([c.args[5:7] for c in audit.call_args_list],
                         [("reviewer", "file_issue"), ("reader", "file_shadow_issue")])


if __name__ == "__main__":
    unittest.main()
