"""#2299: every ``per_page=100`` listing is read in full, or says why one page is enough.

A full first page read as the whole list was a real bug three times (#558 hooks, #559 reviews,
#562 annotations). This scan finds every string (f-strings included, docstrings not) holding
``per_page=100`` in ``diaktoros/`` and ``scripts/`` by AST, and judges each string on its own
(#2299 review F1: one paged read in a function used to clear every other read in it). A string
passes only when that string itself reaches ``gh._read_pages``:

* it is part of the path argument of a ``_read_pages`` call in its own function (a nested
  function is a scope of its own), as the argument itself, an f-string field or a ``+``
  operand, never inside another call's arguments; or
* it is assigned to a name whose every use is part of that path argument; or
* it is returned by a path builder (``hooks_path``, ``reviews_path``) whose every call anywhere
  is part of that path argument, or only text for ``log`` or a failure record; or
* it is registered below, keyed ``"path::function::text"`` (f-string fields as ``{}``), with the
  reason it is safe: in ``SINGLE_PAGE`` (one page is enough, or a full page is refused as
  incomplete) or in ``PAGED_BY_HAND`` (the site pages itself, bounded, and never calls a prefix
  whole). An entry covers one string: a new read in a registered function is a new site.

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

# Keyed "path::function::path text" (an f-string's fields shown as ``{}``), so each entry covers
# one string, not every read in its function; a second identical string in one function is
# "...#2" and needs its own entry (#2299 review F1).
SINGLE_PAGE = {
    "diaktoros/continue_run.py::inspect::/repos/{}/pulls?state=all&per_page=100&head={}":
        "filtered to one head branch: the run needs exactly one PR, and more than one on the "
        "first page is already reported as a conflict",
    "diaktoros/cli/common.py::_hook_listing::/repos/{}/hooks?per_page=100":
        "a full page (100 hooks or more) raises HookAccessError as an incomplete listing; a "
        "short page is the whole list",
}

PAGED_BY_HAND = {
    "diaktoros/ci.py::_runner_shutdown::/repos/{}/check-runs/{}/annotations?per_page=100&page={}":
        "pages to ci.MAX_PAGES; a failed page is 'no marker' only after every page read (#562)",
    "diaktoros/ci.py::read::/repos/{}/commits/{}/check-runs?per_page=100&page={}":
        "pages to total_count (or, with no usable total_count, to a short page) within "
        "ci.MAX_PAGES; more check runs than read is None, never green",
    "diaktoros/ci.py::_read_statuses::/repos/{}/commits/{}/status?per_page=100&page={}":
        "pages to a short page or total_count within ci.MAX_PAGES; fewer contexts than "
        "total_count is None",
    "diaktoros/doctor/__init__.py::_read_list::{}?per_page=100&page={}":
        "pages until a short page; a failed page or 100 full pages is unknown",
    "diaktoros/doctor/hook_checks.py::_hook_listing::/repos/{}/hooks?per_page=100":
        "pages until a short page; a failed or malformed page is None (unknown)",
    "diaktoros/stats.py::github::/repos/{}/pulls?state=all&sort=created&direction=desc"
    "&per_page=100&page={}":
        "pages newest first until the window ends; past GITHUB_PAGES_MAX it says 'capped'",
    "diaktoros/trace.py::fetch_delivery::{}?per_page=100&page={}":
        "searches MAX_DELIVERY_PAGES pages per hook and says that bound when nothing matched",
    "diaktoros/cli/lifecycle.py::_hook_find_command::repos/{}/hooks?per_page=100":
        "not a read: a printed `gh api --paginate` command, and gh reads every page",
}

DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
# Where a path builder's result may go besides ``_read_pages``: a log line or the record of a
# failed read, directly or through the text built for one. Neither reads the listing. Text
# built for anything else (``gh.fetch(loop, f"{hooks_path(loop)}&a=1")``) is a read (#2299
# review F1, round 2).
NOT_A_READ = {"record_failure", "log"}


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    return func.id if isinstance(func, ast.Name) else (
        func.attr if isinstance(func, ast.Attribute) else None)


def _own(scope: ast.AST):
    """Every node of ``scope`` outside its nested functions and classes (they are scopes of
    their own: an outer ``_read_pages`` call says nothing about an inner read)."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, DEFS):
            stack.extend(ast.iter_child_nodes(node))


def _scopes(tree: ast.Module):
    """``(qualified name, node)`` for the module and every function and class in it."""
    out = [("<module>", tree)]

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, DEFS):
                out.append((prefix + child.name, child))
                visit(child, prefix + child.name + ".")
            else:
                visit(child, prefix)
    visit(tree, "")
    return out


def _path_arg(call: ast.Call) -> ast.AST | None:
    """The ``path`` argument of a ``_read_pages(loop, path, ...)`` call."""
    if len(call.args) > 1:
        return call.args[1]
    return next((k.value for k in call.keywords if k.arg == "path"), None)


def _path_parts(node: ast.AST):
    """``node`` and every node whose text becomes part of it as a path: the fields of an
    f-string and the operands of ``+`` or ``%``. A call's arguments are not part of its result,
    so ``_read_pages(loop, fetch(loop, "/x?per_page=100"), ...)`` does not page that string."""
    yield node
    if isinstance(node, ast.JoinedStr):
        for value in node.values:
            yield from _path_parts(value)
    elif isinstance(node, ast.FormattedValue):
        yield from _path_parts(node.value)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        yield from _path_parts(node.left)
        yield from _path_parts(node.right)


def _part_ids(node: ast.AST) -> set[int]:
    return {id(part) for part in _path_parts(node)}


def _reader_paths(nodes) -> list[ast.AST]:
    return [arg for node in nodes if isinstance(node, ast.Call) and _called_name(node) == READER
            for arg in [_path_arg(node)] if arg is not None]


def _text(node: ast.AST) -> str:
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    return node.value


def _strings(scope: ast.AST) -> list[ast.AST]:
    """Each string in ``scope`` holding ``MARK`` (an f-string as one), docstrings excluded."""
    nodes = list(_own(scope))
    skip = {id(n.value) for n in nodes
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    skip |= {id(v) for n in nodes if isinstance(n, ast.JoinedStr) for v in n.values}
    found = [n for n in nodes if isinstance(n, ast.JoinedStr) and any(
        isinstance(v, ast.Constant) and MARK in str(v.value) for v in n.values)]
    found += [n for n in nodes if isinstance(n, ast.Constant) and isinstance(n.value, str)
              and MARK in n.value and id(n) not in skip]
    return sorted(found, key=lambda n: (n.lineno, n.col_offset))


def _into_reader_by_name(site: ast.AST, nodes: list, reader_args: set[int]) -> bool:
    """``path = "...per_page=100..."`` whose every later use is the path ``_read_pages`` reads
    (``path += ...`` may extend it): a name also handed to another read is not paged."""
    for node in nodes:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and id(site) in _part_ids(node.value)):
            name = node.targets[0].id
            loads = [n for n in nodes if isinstance(n, ast.Name) and n.id == name
                     and isinstance(n.ctx, ast.Load)]
            return bool(loads) and all(id(n) in reader_args for n in loads)
    return False


def _use(call: ast.Call, parents: dict[int, ast.AST], reader_parts: set[int]) -> str:
    """Where a call's result ends up: ``"read"`` when it is part of the path ``_read_pages``
    reads, ``"shown"`` when the text it is part of is only an argument of a ``NOT_A_READ`` call,
    ``"other"`` for anything else (an f-string handed to ``fetch`` is a read of one page)."""
    if id(call) in reader_parts:
        return "read"
    node = call                    # climb to the whole text this call's result is part of
    while id(node) in parents and id(node) in _part_ids(parents[id(node)]):
        node = parents[id(node)]
    consumer = parents.get(id(node))
    if (isinstance(consumer, ast.Call) and _called_name(consumer) in NOT_A_READ
            and node in consumer.args):
        return "shown"
    return "other"


def _feeders(trees: dict[str, ast.Module]) -> set[str]:
    """Path builders whose every call, anywhere scanned, is the path ``_read_pages`` reads (or
    is only logged or recorded): ``hooks_path`` and ``reviews_path``."""
    uses: dict[str, set[str]] = {}         # name -> {"read", "shown", "other"}
    for tree in trees.values():
        parents = {id(c): p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
        reader_parts = set().union(*(_part_ids(a) for a in _reader_paths(ast.walk(tree))))
        for call in ast.walk(tree):
            if isinstance(call, ast.Call) and _called_name(call) is not None:
                uses.setdefault(_called_name(call), set()).add(
                    _use(call, parents, reader_parts))
    return {name for name, kinds in uses.items() if "read" in kinds and "other" not in kinds}


def sites(sources: dict[str, str]) -> dict[str, bool]:
    """``{"rel::function::text": paged}`` for every ``per_page=100`` string in ``sources``
    (``{rel: source}``). A string is paged only when that string itself reaches ``_read_pages``:
    inside its path argument, through a name used for nothing else, or returned by a path
    builder (``_feeders``) whose every call is such an argument."""
    trees = {rel: ast.parse(source) for rel, source in sources.items()}
    feeders = _feeders(trees)
    found: dict[str, bool] = {}
    for rel, tree in trees.items():
        for qualname, scope in _scopes(tree):
            nodes = list(_own(scope))
            reader_args = set().union(*(_part_ids(a) for a in _reader_paths(nodes)))
            returned = set().union(*(_part_ids(n.value) for n in nodes
                                     if isinstance(n, ast.Return) and n.value is not None))
            for site in _strings(scope):
                key = base = f"{rel}::{qualname}::{_text(site)}"
                count = 1
                while key in found:
                    count += 1
                    key = f"{base}#{count}"
                found[key] = (id(site) in reader_args
                              or _into_reader_by_name(site, nodes, reader_args)
                              or (id(site) in returned and isinstance(scope, ast.FunctionDef)
                                  and scope.name in feeders))
    return found


def scan() -> dict[str, bool]:
    sources = {}
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*.py")):
            if "__pycache__" not in path.parts:
                sources[path.relative_to(ROOT).as_posix()] = path.read_text(encoding="utf-8")
    return sites(sources)


def unpaged(found: dict[str, bool]) -> list[str]:
    registered = {**SINGLE_PAGE, **PAGED_BY_HAND}
    return sorted(key for key, paged in found.items() if not paged and key not in registered)


def _one(source: str, rel: str = "diaktoros/new.py") -> list[str]:
    return unpaged(sites({rel: source}))


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
                self.assertIn(key, found, "stale entry: no such per_page=100 string any more")
                self.assertFalse(found[key], "this site reads through _read_pages now: drop it")
                self.assertGreater(len(why.split()), 3)

    def test_the_scan_sees_the_known_paged_sites(self):
        found = scan()
        for key in ("diaktoros/gh.py::issue_comments_read::/repos/{}/issues/{}/comments"
                    "?per_page=100",
                    "diaktoros/gh.py::hooks_path::/repos/{}/hooks?per_page=100",
                    "diaktoros/gh.py::reviews_path::{}/reviews?per_page=100",
                    "diaktoros/gh.py::pr_files_read::{}/files?per_page=100",
                    "diaktoros/stats_public.py::closed_listing::/repos/{}/issues?state=closed"
                    "&per_page=100",
                    "diaktoros/issue_deps.py::met::/repos/{}/issues/{}/events?per_page=100",
                    "diaktoros/gh.py::branch_rules_read::/repos/{}/rules/branches/{}"
                    "?per_page=100",
                    "diaktoros/gh.py::rulesets_read::/repos/{}/rulesets?per_page=100"
                    "&includes_parents=false"):
            self.assertIs(found.get(key), True, key)
        self.assertFalse([k for k in found if "::_read_pages::" in k])   # a docstring

    def test_a_new_unpaged_read_fails_it(self):
        for source in (
                'def f(loop):\n    return gh.api(loop, f"/repos/{loop[\'repo\']}/x?per_page=100")\n',
                'def f(loop):\n    return gh.fetch(loop, "/repos/a/b/x?per_page=100")\n',
                'class C:\n    def m(self, loop):\n'
                '        p = f"/x?state=all&per_page=100&page={1}"\n        return gh.api(loop, p)\n'):
            with self.subTest(source=source):
                self.assertTrue(_one(source))
        paged = ('def f(loop):\n    return gh._read_pages(loop, f"/r/{loop}/x?per_page=100", '
                 '"x", 3)\n')
        self.assertEqual(_one(paged), [])
        by_name = ('def f(loop):\n    path = f"/r/{loop}/x?per_page=100"\n    path += "&a=1"\n'
                   '    return gh._read_pages(loop, path, "x", 3)\n')
        self.assertEqual(_one(by_name), [])

    def test_a_paged_read_does_not_cover_an_unpaged_one_beside_it(self):
        # The review's counterexample: one _read_pages call used to clear every site in f.
        mixed = ('def f(loop):\n    gh._read_pages(loop, "/safe?per_page=100", "x", 3)\n'
                 '    return gh.fetch(loop, "/unsafe?per_page=100")\n')
        self.assertEqual(_one(mixed), ["diaktoros/new.py::f::/unsafe?per_page=100"])

    def test_an_outer_paged_read_does_not_cover_a_nested_function(self):
        nested = ('def f(loop):\n    def g():\n        return gh.api(loop, "/in?per_page=100")\n'
                  '    return gh._read_pages(loop, "/out?per_page=100", "x", 3), g\n')
        self.assertEqual(_one(nested), ["diaktoros/new.py::f.g::/in?per_page=100"])

    def test_a_name_read_both_paged_and_unpaged_is_unpaged(self):
        shared = ('def f(loop):\n    p = "/x?per_page=100"\n'
                  '    gh._read_pages(loop, p, "x", 3)\n    return gh.fetch(loop, p)\n')
        self.assertEqual(_one(shared), ["diaktoros/new.py::f::/x?per_page=100"])

    def test_a_path_builder_also_read_unpaged_is_unpaged(self):
        builder = 'def hp(loop):\n    return "/hooks?per_page=100"\n'
        paged = builder + 'def r(loop):\n    return _read_pages(loop, hp(loop), "h", 3)\n'
        self.assertEqual(_one(paged), [])
        shown = paged + 'def s(loop):\n    log(f"GET {hp(loop)}")\n'
        self.assertEqual(_one(shown), [])
        self.assertEqual(_one(paged + 'def u(loop):\n    return api(loop, hp(loop))\n'),
                         ["diaktoros/new.py::hp::/hooks?per_page=100"])

    def test_a_path_builder_inside_an_unpaged_fstring_read_is_unpaged(self):
        # #2299 review F1, round 2: an f-string around the builder used to count as "shown",
        # whatever read it. Driven through the real tree and the real gh.hooks_path.
        sources = {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8")
                   for top in SCANNED for p in sorted((ROOT / top).rglob("*.py"))
                   if "__pycache__" not in p.parts}
        self.assertEqual(unpaged(sites(sources)), [])
        hooks = "diaktoros/gh.py::hooks_path::/repos/{}/hooks?per_page=100"
        for read in ('gh.fetch(loop, f"{gh.hooks_path(loop)}&active=1")',
                     'gh.fetch(loop, gh.hooks_path(loop) + "&active=1")',
                     'gh.log(f"x {gh.fetch(loop, f\'{gh.hooks_path(loop)}&a=1\')}")'):
            with self.subTest(read=read):
                added = {**sources, "diaktoros/new.py":
                         f"from diaktoros import gh\ndef read_hooks(loop):\n    return {read}\n"}
                self.assertEqual(unpaged(sites(added)), [hooks])
        logged = 'def s(loop):\n    gh.log(f"GET {gh.hooks_path(loop)} failed")\n'
        self.assertEqual(unpaged(sites({**sources, "diaktoros/new.py": logged})), [])

    def test_a_string_read_by_another_call_inside_the_path_is_unpaged(self):
        inner = ('def f(loop):\n    return gh._read_pages('
                 'loop, gh.fetch(loop, "/bad?per_page=100"), "h", 3)\n')
        self.assertEqual(_one(inner), ["diaktoros/new.py::f::/bad?per_page=100"])
        named = ('def f(loop):\n    p = gh.fetch(loop, "/bad?per_page=100")\n'
                 '    return gh._read_pages(loop, p, "h", 3)\n')
        self.assertEqual(_one(named), ["diaktoros/new.py::f::/bad?per_page=100"])
        joined = ('def f(loop):\n    return gh._read_pages(loop, f"{loop}" + "/ok?per_page=100",'
                  ' "h", 3)\n')
        self.assertEqual(_one(joined), [])

    def test_a_new_read_in_a_registered_function_fails_it(self):
        rel = "diaktoros/continue_run.py"
        source = (ROOT / rel).read_text(encoding="utf-8")
        self.assertEqual(_one(source, rel), [])
        anchor = "    pr = None\n"
        self.assertEqual(source.count(anchor), 1)
        for extra, key in (
                ('gh.fetch(loop, f"/repos/{repo}/issues?per_page=100")',
                 f"{rel}::inspect::/repos/{{}}/issues?per_page=100"),
                ('gh.fetch(loop, f"/repos/{repo}/pulls?state=all&per_page=100&head={branch}")',
                 f"{rel}::inspect::/repos/{{}}/pulls?state=all&per_page=100&head={{}}#2")):
            with self.subTest(extra=extra):
                added = source.replace(anchor, f"{anchor}    {extra}\n")
                self.assertEqual(_one(added, rel), [key])


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
