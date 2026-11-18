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

The scan is ``tools/paged_reads.py`` (with ``tools/paged_read_names.py``); see there why it is
not in this file. The registry is here: it is what this test asserts of the repository.

The rest of the file covers the sites #2299 moved onto ``_read_pages``: the base's rules and the
repository's rulesets (``protection``, ``doctor``) and the PR's commits for the CI-fix history.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# The scan lives in tools/paged_reads.py and tools/paged_read_names.py, outside tests/, so the
# host's base-failure proof runs the tests below against the base's scan, not this PR's (#1973,
# #2536 review F1).
sys.path.insert(0, str(ROOT / "tools"))

from diaktoros import ci, ci_fix, doctor, gh, protection  # noqa: E402
import paged_read_names  # noqa: E402
import paged_reads  # noqa: E402
from paged_reads import SCANNED, scan, sites  # noqa: E402

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

    def test_a_name_captured_by_a_nested_unpaged_read_is_unpaged(self):
        # #2363: a closure reading the outer name with fetch read only its first page.
        closure = ('from diaktoros import gh\ndef f(loop):\n    path = "/unsafe?per_page=100"\n'
                   '    def first_page():\n        return gh.fetch(loop, path)\n'
                   '    gh._read_pages(loop, path, "x", 3)\n    return first_page()\n')
        self.assertEqual(_one(closure), ["diaktoros/new.py::f::/unsafe?per_page=100"])
        lam = ('def f(loop):\n    path = "/l?per_page=100"\n'
               '    gh._read_pages(loop, path, "x", 3)\n    return lambda: gh.fetch(loop, path)\n')
        self.assertEqual(_one(lam), ["diaktoros/new.py::f::/l?per_page=100"])
        # A closure that pages the name, or one that reads its own rebound name, is fine.
        paged = ('def f(loop):\n    path = "/p?per_page=100"\n'
                 '    def g():\n        return gh._read_pages(loop, path, "x", 3)\n    return g()\n')
        self.assertEqual(_one(paged), [])
        rebound = ('def f(loop):\n    path = "/r?per_page=100"\n'
                   '    def g(path):\n        return gh.fetch(loop, path)\n'
                   '    return gh._read_pages(loop, path, "x", 3), g\n')
        self.assertEqual(_one(rebound), [])

    def test_a_comprehension_target_does_not_rebind_a_captured_name(self):
        # #2387 review F3: a comprehension's ``for`` target is its own scope, so ``g`` still
        # reads the outer ``path`` with fetch; a real assignment or ``:=`` in g does rebind.
        head = 'def f(loop):\n    path = "/bad?per_page=100"\n    def g():\n'
        tail = ('        return gh.fetch(loop, path)\n'
                '    return gh._read_pages(loop, path, "x", 3), g()\n')
        for comp in ('[path for path in ["other"]]', '{path for path in ["other"]}',
                     '{path: 1 for path in ["other"]}', 'list(path for path in ["other"])',
                     '[x for x in [[path for path in ["o"]]]]'):
            with self.subTest(comp=comp):
                self.assertEqual(_one(f'{head}        unrelated = {comp}\n{tail}'),
                                 ["diaktoros/new.py::f::/bad?per_page=100"])
        for own in ('        path = "/own"\n', '        [(path := "/own") for _ in "x"]\n',
                    '        for path in ["/own"]:\n            pass\n'):
            with self.subTest(own=own):
                self.assertEqual(_one(head + own + tail), [])

    def test_a_global_under_a_shadowing_function_reads_the_module_name(self):
        # #2423: ``outer`` rebinds ``path``, but ``inner`` (or one nested in it) declares it
        # global and reads the module's ``path`` with fetch: unpaged. Without the ``global``,
        # ``inner`` reads ``outer``'s own and the module's stays paged.
        head = 'from diaktoros import gh\npath = "/unsafe?per_page=100"\ndef outer():\n'
        tail = '    return inner()\ngh._read_pages(None, path, "x", 3)\nouter()\n'
        for inner in ('    path = "/own"\n    def inner():\n        global path\n'
                      '        return gh.fetch(None, path)\n',
                      '    path = "/own"\n    def inner():\n        global path\n'
                      '        def deeper():\n            return gh.fetch(None, path)\n'
                      '        return deeper()\n',
                      '    path = "/own"\n    class C:\n        def inner(self):\n'
                      '            global path\n            return gh.fetch(None, path)\n'):
            with self.subTest(inner=inner):
                self.assertEqual(_one(head + inner + tail),
                                 ["diaktoros/new.py::<module>::/unsafe?per_page=100"])
        own = '    path = "/own"\n    def inner():\n        return gh.fetch(None, path)\n'
        self.assertEqual(_one(head + own + tail), [])

    def test_a_global_under_a_shadow_under_a_global_reads_the_module_name(self):
        # #2536 review F3: ``inner`` declares ``path`` global, ``shadow`` rebinds it, and
        # ``deeper`` declares it global again and fetches it: the module's ``path``, unpaged.
        # The search for a ``global`` stopped at ``inner`` and never looked past ``shadow``.
        source = ('from diaktoros import gh\npath = "/unsafe?per_page=100"\ndef outer():\n'
                  '    path = "/own"\n    def inner():\n        global path\n'
                  '        def shadow():\n            path = "/inner"\n'
                  '            def deeper():\n                global path\n'
                  '                return gh.fetch(None, path)\n'
                  '            return deeper()\n        return shadow()\n    return inner()\n'
                  'gh._read_pages(None, path, "x", 3)\n')
        bad = ["diaktoros/new.py::<module>::/unsafe?per_page=100"]
        self.assertEqual(_one(source), bad)
        # Without ``deeper``'s ``global`` it reads ``shadow``'s own ``path``: still paged.
        self.assertEqual(_one(source.replace("                global path\n", "")), [])
        # Dropping ``module`` (the scan before F3) loses the read.
        real = paged_read_names._name_loads
        with mock.patch.object(paged_read_names, "_name_loads",
                               lambda scope, name, module=False: real(scope, name)):
            self.assertEqual(_one(source), [])

    def test_the_global_and_comprehension_rules_are_each_needed(self):
        # #2536 review F1: undone, the scan misses #2423's global read and reports #2424's
        # comprehension's own ``path``. (The base proof runs the base's scan from tools/ now;
        # these pin what each rule does.)
        glob = ('from diaktoros import gh\npath = "/unsafe?per_page=100"\ndef outer():\n'
                '    path = "/own"\n    def inner():\n        global path\n'
                '        return gh.fetch(None, path)\n    return inner()\n'
                'gh._read_pages(None, path, "x", 3)\n')
        comp = ('from diaktoros import gh\ndef f(loop):\n    path = "/safe?per_page=100"\n'
                '    x = [gh.fetch(loop, path) for path in ["/other"]]\n'
                '    gh._read_pages(loop, path, "x", 3)\n')
        self.assertEqual(_one(glob), ["diaktoros/new.py::<module>::/unsafe?per_page=100"])
        self.assertEqual(_one(comp), [])
        with mock.patch.object(paged_read_names, "_global_loads", lambda node, name: []):
            self.assertEqual(_one(glob), [])
        with mock.patch.object(paged_read_names, "_comp_binds", lambda node, name: False):
            self.assertEqual(_one(comp), ["diaktoros/new.py::f::/safe?per_page=100"])

    def test_a_comprehension_binding_the_name_reads_its_own(self):
        # #2424: ``[gh.fetch(loop, path) for path in ...]`` reads the comprehension's ``path``,
        # not the enclosing one, nested or in the same scope; its first ``iter`` still reads
        # the enclosing name, and one that does not bind ``path`` stays reported.
        bad = ["diaktoros/new.py::f::/safe?per_page=100"]
        head = 'from diaktoros import gh\ndef f(loop):\n    path = "/safe?per_page=100"\n'
        tail = '    gh._read_pages(loop, path, "x", 3)\n'
        for comp, found in (('[gh.fetch(loop, path) for path in ["/other"]]', []),
                            ('{gh.fetch(loop, path) for x, path in [("a", "/o")]}', []),
                            ('[gh.fetch(loop, x) for x in [gh.fetch(loop, path)]]', bad),
                            ('[x for path in [gh.fetch(loop, path)]]', bad),
                            ('[gh.fetch(loop, path) for x in ["/other"]]', bad)):
            for form in (f'    def g():\n        return {comp}\n', f'    x = {comp}\n'):
                with self.subTest(form=form):
                    self.assertEqual(_one(head + form + tail), found)

    def test_a_rebinding_scope_still_reads_the_outer_name_where_it_is_defined(self):
        # #2387 review F1: defaults, decorators, annotations and a class body run in the
        # enclosing scope, so a rebinding def or class there still reads the outer name.
        head = 'def f(loop):\n    path = "/bad?per_page=100"\n'
        tail = '    return gh._read_pages(loop, path, "x", 3), g\n'
        bad = ["diaktoros/new.py::f::/bad?per_page=100"]
        for nested in (
                '    def g(path=gh.fetch(loop, path)):\n        return path\n',
                '    def g(*, path=gh.fetch(loop, path)):\n        return path\n',
                '    g = lambda path=gh.fetch(loop, path): path\n',
                '    @gh.fetch(loop, path)\n    def g(path):\n        return path\n',
                '    def g(path: gh.fetch(loop, path)):\n        return path\n',
                '    class g(gh.fetch(loop, path)):\n        path = "/own"\n',
                '    class g:\n        path = "/own"\n'
                '        def m(self):\n            return gh.fetch(loop, path)\n'):
            with self.subTest(nested=nested):
                self.assertEqual(_one(head + nested + tail), bad)
        # A module-level class body reads the module's name until it assigns its own.
        module = ('path = "/mod?per_page=100"\nclass C:\n    first = gh.fetch(None, path)\n'
                  '    path = "/own"\ngh._read_pages(None, path, "x", 3)\n')
        self.assertEqual(_one(module), ["diaktoros/new.py::<module>::/mod?per_page=100"])
        for nested in (
                '    def g(path=None):\n        return gh.fetch(loop, path)\n',
                '    class g:\n        def m(self, path):\n            return gh.fetch(loop, path)\n'):
            with self.subTest(nested=nested):
                self.assertEqual(_one(head + nested + tail), [])

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

    def test_a_base_proof_tests_the_base_scan(self):
        # #2536 review F1: the scan lives off the test paths, so the host's base-failure proof
        # (change_proof.apply_tests) runs the head's tests above against the base's scan. It fails
        # if either module goes back into a tests/ module, where the scan was.
        import _base_proof
        for module in (paged_reads, paged_read_names):
            with self.subTest(module=module.__name__):
                _base_proof.assert_taken_from_the_base(self, module.__file__)


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
