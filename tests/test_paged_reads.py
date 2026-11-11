"""#2299: every ``per_page=100`` listing is read in full, or says why one page is enough.

A full first page read as the whole list was a real bug three times (#558 hooks, #559 reviews,
#562 annotations). This scan finds every string (f-strings included, docstrings not) holding
``per_page=100`` in ``diaktoros/`` and ``scripts/`` by AST. A site passes only when:

* its function calls ``gh._read_pages`` (directly, or as ``_read_pages`` inside ``gh``); or
* its function builds a path that ``gh`` passes straight into ``_read_pages`` (``hooks_path``,
  ``reviews_path``); or
* it is registered below, keyed ``"path::function"``, with the reason it is safe: in
  ``SINGLE_PAGE`` (one page is enough, or a full page is refused as incomplete) or in
  ``PAGED_BY_HAND`` (the site pages itself, bounded, and never calls a prefix whole).

A registry entry that matches no site fails too, so the registry cannot rot.

The rest of the file covers the sites #2299 moved onto ``_read_pages``: the base's rules and the
repository's rulesets (``protection``, ``doctor``) and the PR's commits for the CI-fix history.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from diaktoros import ci, ci_fix, doctor, gh, protection  # noqa: E402

MARK = "per_page=100"
SCANNED = ("diaktoros", "scripts")
READER = "_read_pages"

SINGLE_PAGE = {
    "diaktoros/continue_run.py::inspect":
        "filtered to one head branch: the run needs exactly one PR, and more than one on the "
        "first page is already reported as a conflict",
    "diaktoros/cli/common.py::_hook_listing":
        "a full page (100 hooks or more) raises HookAccessError as an incomplete listing; a "
        "short page is the whole list",
}

PAGED_BY_HAND = {
    "diaktoros/ci.py::_runner_shutdown":
        "pages to ci.MAX_PAGES; a failed page is 'no marker' only after every page read (#562)",
    "diaktoros/ci.py::read":
        "pages by total_count to ci.MAX_PAGES; more check runs than read is None, never green",
    "diaktoros/ci.py::_read_statuses":
        "pages by total_count to ci.MAX_PAGES; fewer contexts than total_count is None",
    "diaktoros/doctor/__init__.py::_read_list":
        "pages until a short page; a failed page or 100 full pages is unknown",
    "diaktoros/doctor/hook_checks.py::_hook_listing":
        "pages until a short page; a failed or malformed page is None (unknown)",
    "diaktoros/stats.py::github":
        "pages newest first until the window ends; past GITHUB_PAGES_MAX it says 'capped'",
    "diaktoros/trace.py::fetch_delivery":
        "searches MAX_DELIVERY_PAGES pages per hook and says that bound when nothing matched",
    "diaktoros/cli/lifecycle.py::_hook_find_command":
        "not a read: a printed `gh api --paginate` command, and gh reads every page",
}


def _docstrings(tree: ast.AST) -> set[int]:
    """The ids of the docstring nodes and every other bare string statement."""
    return {id(node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)}


def _called(node: ast.AST, name: str) -> bool:
    """Whether ``node`` contains a call to ``name`` (bare or as an attribute)."""
    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            func = call.func
            if (isinstance(func, ast.Name) and func.id == name) or (
                    isinstance(func, ast.Attribute) and func.attr == name):
                return True
    return False


def _feeders(tree: ast.AST) -> set[str]:
    """Names of functions called inside the arguments of a ``_read_pages`` call: path builders
    whose path is read in full (``_read_pages(loop, hooks_path(loop), ...)``)."""
    names = set()
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and _called_name(call) == READER:
            for arg in list(call.args) + [k.value for k in call.keywords]:
                names |= {_called_name(inner) for inner in ast.walk(arg)
                          if isinstance(inner, ast.Call)}
    return names - {None}


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    return func.id if isinstance(func, ast.Name) else (
        func.attr if isinstance(func, ast.Attribute) else None)


def sites(source: str, rel: str) -> dict[str, bool]:
    """``{"rel::function": paged}`` for every function holding a ``per_page=100`` string."""
    tree = ast.parse(source)
    skip, feeders = _docstrings(tree), _feeders(tree)
    found: dict[str, bool] = {}

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, scope + [child])
                continue
            for inner in ast.walk(child):
                if (isinstance(inner, ast.Constant) and isinstance(inner.value, str)
                        and MARK in inner.value and id(inner) not in skip):
                    funcs = [s for s in scope if not isinstance(s, ast.ClassDef)]
                    name = ".".join(s.name for s in scope) or "<module>"
                    outer = funcs[0] if funcs else None
                    paged = outer is not None and (
                        _called(outer, READER) or outer.name in feeders)
                    key = f"{rel}::{name}"
                    found[key] = found.get(key, True) and paged

    visit(tree, [])
    return found


def scan() -> dict[str, bool]:
    found: dict[str, bool] = {}
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            found.update(sites(path.read_text(encoding="utf-8"), rel))
    return found


def unpaged(found: dict[str, bool]) -> list[str]:
    registered = {**SINGLE_PAGE, **PAGED_BY_HAND}
    return sorted(key for key, paged in found.items() if not paged and key not in registered)


class Scan(unittest.TestCase):
    def test_every_per_page_100_read_pages_or_is_registered(self):
        missing = unpaged(scan())
        self.assertEqual(missing, [], "these per_page=100 reads neither go through "
                         "gh._read_pages nor are registered in SINGLE_PAGE/PAGED_BY_HAND "
                         "with a reason: a full first page is not the whole list")

    def test_every_registry_entry_names_a_live_unpaged_site_with_a_reason(self):
        found = scan()
        for key, why in {**SINGLE_PAGE, **PAGED_BY_HAND}.items():
            with self.subTest(key=key):
                self.assertIn(key, found, "stale entry: no per_page=100 string there any more")
                self.assertFalse(found[key], "this site reads through _read_pages now: drop it")
                self.assertGreater(len(why.split()), 3)

    def test_the_scan_sees_the_known_paged_sites(self):
        found = scan()
        for key in ("diaktoros/gh.py::issue_comments_read", "diaktoros/gh.py::hooks_path",
                    "diaktoros/gh.py::open_issues_read", "diaktoros/issue_deps.py::met",
                    "diaktoros/gh.py::branch_rules_read", "diaktoros/gh.py::rulesets_read"):
            self.assertTrue(found.get(key), key)
        self.assertNotIn("diaktoros/gh.py::_read_pages", found)        # a docstring, not a read

    def test_a_new_unpaged_read_fails_it(self):
        for source in (
                'def f(loop):\n    return gh.api(loop, f"/repos/{loop[\'repo\']}/x?per_page=100")\n',
                'def f(loop):\n    return gh.fetch(loop, "/repos/a/b/x?per_page=100")\n',
                'class C:\n    def m(self, loop):\n'
                '        p = f"/x?state=all&per_page=100&page={1}"\n        return gh.api(loop, p)\n'):
            with self.subTest(source=source):
                self.assertTrue(unpaged(sites(source, "diaktoros/new.py")))
        paged = ('def f(loop):\n    return gh._read_pages(loop, f"/r/{loop}/x?per_page=100", '
                 '"x", 3)\n')
        self.assertEqual(unpaged(sites(paged, "diaktoros/new.py")), [])


def full(n, **item):
    return [{"n": i, **item} for i in range(n)]


class Pages(unittest.TestCase):
    """A fake GitHub answering ``fetch``/``request`` by path: ``self.pages[kind]`` is the list of
    pages for that listing (beyond them: an empty page)."""

    LOOP = {"id": "w", "repo": "acme/w", "base": "main", "read_token": "reader",
            "reviewer_seat": "rev", "apps": {}, "tokens": {"rev": "/t", "reader": "/t"}}

    def setUp(self):
        self.pages: dict[str, list | str] = {}

        def answer(path):
            kind = ("rules" if "/rules/branches/" in path else
                    "rulesets" if "/rulesets" in path else
                    "commits" if "/commits?" in path else "other")
            pages = self.pages.get(kind, [])
            if isinstance(pages, str):
                status = int(pages.split()[1])
                return gh.Response(None, pages, status, {})
            page = int(path.split("&page=")[1]) if "&page=" in path else 1
            got = pages[page - 1] if page <= len(pages) else []
            if isinstance(got, str):                     # one failed page: "HTTP 502"
                return gh.Response(None, got, int(got.split()[1]), {})
            return gh.Response(got, "", 200, {})

        def fetch(loop, path, *a, **k):
            response = answer(path)
            return response.data, response.error

        def request(loop, path, method="GET", body=None, login=None):
            if method != "GET":
                return gh.Response({"id": 77}, "", 200, {})
            if path.endswith("/protection") or path.endswith("/required_status_checks"):
                return gh.Response(None, "HTTP 404 Branch not protected", 404, {})
            return answer(path)

        for name, effect in (("fetch", fetch), ("request", request)):
            patch = mock.patch.object(gh, name, side_effect=effect)
            patch.start()
            self.addCleanup(patch.stop)


def merge_rule(**params):
    return {"type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": ci.MERGE_CHECK}], **params}}


class BranchRules(Pages):
    def test_a_rule_on_the_second_page_is_read_by_protection(self):
        filler = full(100, type="creation")
        self.pages["rules"] = [filler, [merge_rule()]]
        self.assertEqual(protection.read(self.LOOP).sources, [None])

    def test_a_failed_second_page_is_unread_not_unrequired(self):
        self.pages["rules"] = [full(100, type="creation"), "HTTP 502"]
        found = protection.read(self.LOOP)
        self.assertEqual(found.sources, [])
        self.assertEqual(len(found.unread), 1)
        self.assertIn("page 2", found.unread[0])

    def test_a_404_is_no_rules_not_unread(self):
        self.pages["rules"] = "HTTP 404 Not Found"
        self.assertEqual(gh.branch_rules_read(self.LOOP, "main"), ([], ""))
        self.assertEqual(protection.read(self.LOOP).unread, [])
        self.pages["rules"] = "HTTP 403 Forbidden"
        self.assertEqual(len(protection.read(self.LOOP).unread), 1)

    def test_doctor_reads_a_strict_rule_on_the_second_page(self):
        self.pages["rules"] = [full(100, type="creation"),
                               [merge_rule(strict_required_status_checks_policy=True)]]
        self.assertEqual(doctor.base_requires_up_to_date(self.LOOP), (True, ""))

    def test_doctor_sees_a_required_check_on_the_second_page(self):
        rule = {"type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "tests"}]}}
        self.pages["rules"] = [full(100, type="creation"), [rule]]
        self.assertIsNone(doctor.check_branch_protection(
            {**self.LOOP, "required_checks": ["tests"]}, False))


class Rulesets(Pages):
    def test_the_loops_ruleset_on_the_second_page_is_updated_not_duplicated(self):
        self.pages["rulesets"] = [full(100, name="other", id=1),
                                  [{"id": 5, "name": ci.MERGE_CHECK}]]
        loop = {**self.LOOP, "reviewer_seat": "r[bot]",
                "apps": {"r[bot]": {"app_id": 9, "key_file": "/k"}}}
        done, line = protection.require(loop, "admin")
        self.assertTrue(done, line)
        writes = [c.args[1:2] + (c.kwargs.get("method"),) for c in gh.request.call_args_list
                  if c.kwargs.get("method") not in (None, "GET")]
        self.assertEqual(writes, [("/repos/acme/w/rulesets/5", "PUT")])


class CIFixCommits(Pages):
    def setUp(self):
        super().setUp()
        ci_fix._TESTS_SEEN.clear()
        self.addCleanup(ci_fix._TESTS_SEEN.clear)

    def test_a_ci_fix_commit_on_the_second_page_is_shown(self):
        fixed = "a" * 40
        commits = [{"sha": f"{i:040d}", "parents": [{"sha": f"{i + 1:040d}"}]}
                   for i in range(100)]
        self.pages["commits"] = [commits, [{"sha": "c" * 40, "parents": [{"sha": fixed}]}]]
        rows = [{"head": fixed, "state": "succeeded", "push_confirmed": 1.0}]
        detail = {"files": [{"filename": "tests/test_x.py", "status": "modified",
                             "additions": 1, "deletions": 2}]}
        with mock.patch.object(ci_fix, "rows", return_value=rows), \
                mock.patch.object(gh, "api", return_value=detail):
            text = ci_fix.fix_history(self.LOOP, 7)
        self.assertIn("`ccccccc` (CI fix of `aaaaaaa`)", text)
        self.assertNotIn("not on the PR any more", text)


if __name__ == "__main__":
    unittest.main()
