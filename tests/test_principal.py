"""One place resolves who a credential is (#1163 M1a): ``gh.principal``.

Every seat and reader identity check (the broker's authorizers, the review receipt, safe_push's
fixer identity, trusted_fetch) asks ``gh.principal``, each with its own transport. A GitHub App
identity cannot call ``GET /user`` with its installation token, so the App path (M1b) has one
function to change; a new ``/user`` call elsewhere would bypass it. The fixer's git identity
accepts a GitHub App bot login (``name[bot]``) and its noreply address.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import broker, gh, safe_push  # noqa: E402

# The only modules that may still name /user: the seam itself, and the read-only diagnostics
# that read a PAT's own response headers (expiry, scopes), which M1b makes App-aware.
USER_CALLERS = {"diaktoros/gh.py", "diaktoros/doctor/identity_checks.py", "diaktoros/selftest.py"}
# The Python that ships: the package, the gate/watchdog scripts, and the dashboard and desktop pages.
SCANNED = ("diaktoros", "scripts", "dashboard", "desktop")
_UNKNOWN = "\0"                       # a part of a string only known at run time
_USER_PATH = re.compile(r"/user(?=$|[/?#\0])")    # ``/user``, ``/user/emails``, ``/user?x``; not ``/users``


def _text(node: ast.AST) -> str | None:
    """The text a string expression spells, ``_UNKNOWN`` standing for what is only known at run
    time; ``None`` when ``node`` is not a string expression. Reads a literal, an f-string (and a
    literal interpolated into one), a ``+`` and a ``"/".join([...])``, so a path built in pieces
    reads as the path it makes."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.FormattedValue):
        plain = node.conversion == -1 and node.format_spec is None
        return _text(node.value) if plain else None
    if isinstance(node, ast.JoinedStr):
        return "".join(_piece(part) for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _text(node.left), _text(node.right)
        return None if left is None and right is None else _piece(node.left) + _piece(node.right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "join" and len(node.args) == 1 \
            and isinstance(node.args[0], ast.List | ast.Tuple):
        separator = _text(node.func.value)
        if separator is not None:
            return separator.join(_piece(part) for part in node.args[0].elts)
    return None


def _piece(node: ast.AST) -> str:
    """One part of a built string: what it spells, or ``_UNKNOWN`` when only run time knows."""
    text = _text(node)
    return _UNKNOWN if text is None else text


def user_reads(source: str) -> list[int]:
    """The lines of ``source`` that spell a ``/user`` path: a literal, or one built in an f-string,
    a concatenation or a join. A bare string statement (a docstring) is prose, not a call. A path
    assembled from a variable or a call's result is out of its reach."""
    tree = ast.parse(source)
    prose = {id(node.value) for node in ast.walk(tree)
             if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)}
    lines = set()
    for node in ast.walk(tree):
        text = None if id(node) in prose else _text(node)
        if text and _USER_PATH.search(text):
            lines.add(node.lineno)
    return sorted(lines)


def user_calls(root: Path, allowed=USER_CALLERS) -> list[str]:
    """``path:line: source`` for every ``/user`` path in the Python under ``root`` that ships,
    apart from the ``allowed`` files (relative paths)."""
    found = []
    for top in SCANNED:
        for path in sorted((root / top).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if rel in allowed or "__pycache__" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            lines = source.splitlines()
            found += [f"{rel}:{number}: {lines[number - 1].strip()}" for number in user_reads(source)]
    return found


class Seam(unittest.TestCase):
    def test_principal_returns_the_account_or_none(self):
        with mock.patch.object(gh, "api", return_value={"login": "fix", "id": 7}) as api:
            self.assertEqual(gh.principal({}, "fix"), {"login": "fix", "id": 7})
        api.assert_called_once_with({}, "/user", login="fix")
        for answer in (None, [], "Bad credentials"):
            with self.subTest(answer=answer), mock.patch.object(gh, "api", return_value=answer):
                self.assertIsNone(gh.principal({}, "fix"))

    def test_the_callers_transport_is_used_and_its_failure_kept(self):
        seen = []
        self.assertEqual(gh.principal({}, "fix", fetch=lambda path: seen.append(path) or {"id": 1}),
                         {"id": 1})
        self.assertEqual(seen, ["/user"])

        def raises(path):
            raise broker.BrokerDenied("GitHub read request failed")
        with self.assertRaisesRegex(broker.BrokerDenied, "GitHub read request failed"):
            gh.principal({}, "fix", fetch=raises)

    def test_no_identity_check_calls_user_directly(self):
        self.assertEqual(user_calls(ROOT), [], "resolve identities through gh.principal")


class UserScan(unittest.TestCase):
    """The scan above must not be one a new ``/user`` read can walk around."""

    FORMS = {
        "double-quoted literal": 'api(loop, "/user")',
        "single-quoted literal": "api(loop, '/user')",
        "a full URL": 'get("https://api.github.com/user")',
        "a /user subpath": 'api(loop, "/user/emails")',
        "a query string": 'api(loop, "/user?per_page=1")',
        "an f-string": 'api(loop, f"{base}/user")',
        "an f-string with a query": 'api(loop, f"/user?page={page}")',
        "an f-string with a suffix": 'api(loop, f"/user{suffix}")',
        "an f-string interpolating the segment": "api(loop, f\"{base}/{'user'}\")",
        "a concatenation": 'api(loop, base + "/user")',
        "a concatenation split inside the segment": 'api(loop, "/us" + "er")',
        "a join": 'api(loop, "/".join([base, "user"]))',
        "a join of a tuple": 'api(loop, "/".join((base, "user")))',
    }
    CLEAN = {
        "the users endpoint": 'api(loop, "/users/someone")',
        "the users endpoint in an f-string": 'api(loop, f"/users/{login}")',
        "a field called user": 'pr.get("user")',
        "a longer segment": 'api(loop, "/user_agent")',
        "a docstring naming it": '"""GET /user answers who the token is."""',
        "a comment naming it": "x = 1  # GET /user",
        "a message naming it": 'say(f"{login}: GET /user failed")',
        "a filesystem join": 'os.path.join(home, "user")',
    }

    def test_every_form_of_a_user_read_is_caught(self):
        for name, source in self.FORMS.items():
            with self.subTest(name):
                self.assertEqual(user_reads(f"x = 1\n{source}\n"), [2])

    def test_prose_and_other_endpoints_are_not_mistaken_for_one(self):
        for name, source in self.CLEAN.items():
            with self.subTest(name):
                self.assertEqual(user_reads(source + "\n"), [])

    def test_a_docstring_that_names_it_is_prose_but_the_function_body_is_not(self):
        source = 'def f(base):\n    """GET /user."""\n    return base + "/user"\n'
        self.assertEqual(user_reads(source), [3])

    def test_the_whole_tree_that_ships_is_scanned_with_the_same_allowlist(self):
        read = 'def f(base):\n    return f"{base}/user"\n'
        flagged = ("diaktoros/new.py", "diaktoros/sub/deeper.py", "scripts/watchdog.py",
                   "dashboard/plugin_api.py", "desktop/page.py",
                   "scripts/gh.py")                    # the allowlist is by path, not by name
        quiet = ("diaktoros/gh.py", "diaktoros/doctor/identity_checks.py", "diaktoros/selftest.py",
                 "tests/test_x.py", "tools/spike.py")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in (*flagged, *quiet):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(read)
            found = {line.split(":")[0] for line in user_calls(root)}
        self.assertEqual(found, set(flagged))

    def test_a_finding_names_the_file_and_the_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "scripts").mkdir()
            (Path(tmp) / "scripts" / "watchdog.py").write_text('x = 1\nurl = base + "/user"\n')
            self.assertEqual(user_calls(Path(tmp)), ['scripts/watchdog.py:2: url = base + "/user"'])


class FixerIdentity(unittest.TestCase):
    def identity(self, account, login="fix", **kw):
        with mock.patch.object(safe_push, "_api", return_value=account):
            return safe_push._fixer_identity({}, login, **kw)

    def test_a_user_and_an_app_bot_get_their_noreply_address(self):
        self.assertEqual(self.identity({"login": "fix", "id": 9}),
                         {"name": "fix", "email": "9+fix@users.noreply.github.com"})
        self.assertEqual(self.identity({"login": "fix-bot[bot]", "id": 346015539},
                                       login="fix-bot[bot]"),
                         {"name": "fix-bot[bot]",
                          "email": "346015539+fix-bot[bot]@users.noreply.github.com"})

    def test_another_account_or_a_bad_id_is_refused(self):
        for account in ({"login": "other", "id": 9}, {"login": "fix", "id": 0},
                        {"login": "fix", "id": True}, {"login": "fix"}, {"id": 9}):
            with self.subTest(account=account), \
                    self.assertRaisesRegex(broker.BrokerDenied, "fixer identity changed"):
                self.identity(account)

    def test_a_login_git_would_not_take_is_refused_with_the_callers_reason(self):
        for name in ("fix bot", "fix[bot]x", "fix[robot]", "fix;rm"):
            with mock.patch.object(safe_push, "_api", return_value={"login": name, "id": 9}):
                with self.subTest(name=name), \
                        self.assertRaisesRegex(broker.BrokerDenied, "invalid fixer identity"):
                    safe_push._fixer_identity({}, name)
                with self.assertRaisesRegex(broker.BrokerDenied, "fixer identity changed"):
                    safe_push._fixer_identity({}, name, invalid="fixer identity changed")


if __name__ == "__main__":
    unittest.main()
