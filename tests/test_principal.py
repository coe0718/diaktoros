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
# An endpoint, not a sentence: one run of non-blank text, matched whole, that ends in ``/user`` and
# goes on only into a subpath, a query or a part run time fills. What comes before it is nothing, a
# ``scheme://host`` or bare host, or a run that holds a part only run time knows (``f"{base}/user"``,
# ``f"/{version}/user"``). So ``/user``, ``/user/emails``, ``/user?x`` and
# ``https://api.github.com/user`` are found; ``/users/x`` is not, and neither is prose that merely
# names it (``GET /user``, ``could not read /user``) or a file path (``/home/user``).
_USER_PATH = re.compile(r"(?:[^/\s]*://[^/\s]*|(?:[\w-]+\.)+[\w-]+(?::\d+)?|\S*\0\S*)?/user(?:[/?#\0]\S*)?")


_NOT_STATIC = object()                # an interpolated expression that is not a constant or a spelled string
_CONVERT = {-1: lambda value: value, ord("s"): str, ord("r"): repr, ord("a"): ascii}


def _text(node: ast.AST) -> str | None:
    """The text a string expression spells, ``_UNKNOWN`` standing for what is only known at run
    time; ``None`` when ``node`` is not a string expression. Reads a literal, an f-string (with
    its ``!s``/``!r``/``!a`` conversions and ``:spec`` applied to what is a constant), a ``+`` and
    a ``"/".join([...])``, so a path built in pieces reads as the path it makes."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.FormattedValue):
        return _formatted(node)
    if isinstance(node, ast.JoinedStr):
        return "".join(_piece(part) for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _text(node.left), _text(node.right)
        if left is None and right is None:
            return None
        return (_UNKNOWN if left is None else left) + (_UNKNOWN if right is None else right)
    if _is_join(node):
        separator = _text(node.func.value)
        if separator is not None:
            return separator.join(_piece(part) for part in node.args[0].elts)
    return None


def _piece(node: ast.AST) -> str:
    """One part of a built string: what it spells, or ``_UNKNOWN`` when only run time knows."""
    text = _text(node)
    return _UNKNOWN if text is None else text


def _is_join(node: ast.AST) -> bool:
    """``sep.join([...])`` or ``sep.join((...))``: a join of a list or tuple written in place."""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "join" and len(node.args) == 1
            and isinstance(node.args[0], ast.List | ast.Tuple))


def _static(node: ast.AST):
    """The value of an interpolated expression when a constant or a spelled string, else ``_NOT_STATIC``."""
    if isinstance(node, ast.Constant):
        return node.value
    text = _text(node)
    return _NOT_STATIC if text is None else text


def _formatted(node: ast.FormattedValue) -> str | None:
    """What one ``{value!conversion:spec}`` of an f-string spells: the value after its conversion
    and format spec, so ``{'us'!s}`` is ``us`` and ``{'user':s}`` is ``user``. ``None`` when the value
    or the spec is only known at run time; a part-known string is kept only when nothing would change it."""
    value = _static(node.value)
    spec = "" if node.format_spec is None else _text(node.format_spec)
    if value is _NOT_STATIC or spec is None or _UNKNOWN in spec:
        return None
    if isinstance(value, str) and _UNKNOWN in value:
        return value if node.conversion in (-1, ord("s")) and not spec else None
    try:
        return format(_CONVERT[node.conversion](value), spec)
    except (KeyError, TypeError, ValueError):
        return None


def _parts(node: ast.AST) -> list[ast.AST]:
    """The nodes ``_text`` reads into ``node``'s text, so a fragment of a spelled path is not
    read again as a path of its own (``f"/user{'s'}/alice"`` spells ``/users/alice``, not ``/user``)."""
    if isinstance(node, ast.JoinedStr):
        return list(node.values)
    if isinstance(node, ast.FormattedValue):
        return [node.value] + ([node.format_spec] if node.format_spec is not None else [])
    if isinstance(node, ast.BinOp):
        return [node.left, node.right]
    if _is_join(node):
        return [node.func, node.func.value, node.args[0], *node.args[0].elts]
    return []


def user_reads(source: str) -> list[int]:
    """The lines of ``source`` that spell a ``/user`` endpoint (``_USER_PATH``): a literal, or one
    built in an f-string, a concatenation or a join. The whole expression is read, never its
    fragments on their own; an interpolation or operand that is not a string (a call) is searched
    for its own paths. Message text that names the endpoint (``"GET /user"``, anything with a blank
    in it) is not an endpoint, and a bare string statement (a docstring) is prose, not a call. A
    path assembled from a variable or a call's result is out of its reach."""
    tree = ast.parse(source)
    prose = {id(node.value) for node in ast.walk(tree)
             if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)}
    lines, fragments, pending = set(), set(), [tree]
    while pending:                    # a parent is read before its children, so it claims its fragments
        node = pending.pop()
        if id(node) in prose:
            continue
        text = _text(node)
        if text is not None:
            if id(node) not in fragments and _USER_PATH.fullmatch(text):
                lines.add(node.lineno)
            fragments.update(id(part) for part in _parts(node))
        pending.extend(ast.iter_child_nodes(node))
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
        "a conversion on a literal in an f-string": "api(loop, f\"/{'us'!s}er\")",
        "a format spec on a literal": "api(loop, f\"/{'user':s}\")",
        "a padding spec on a literal": "api(loop, f\"/{'use':r<4}\")",
        "a conversion and a spec on a literal": "api(loop, f\"/{'user'!s:>4}\")",
        "a concatenation interpolated into an f-string": "api(loop, f\"/{'us' + 'er'}\")",
        "a call inside the interpolation of a users path": "api(loop, f\"/users/{get('/user')}\")",
        "a call among the operands of a concatenation": 'api(loop, "/users/" + get("/user"))',
        "a scheme and a host built at run time": 'get(f"{scheme}://{host}/user")',
        "a base and a query": 'api(loop, f"{base}/user?page=1")',
        "a version segment before it": 'api(loop, f"/{version}/user")',
        "two run time segments before it": 'api(loop, f"{base}/{version}/user")',
        "a host with no scheme": 'get("api.github.com/user")',
    }
    CLEAN = {
        "the users endpoint": 'api(loop, "/users/someone")',
        "the users endpoint in an f-string": 'api(loop, f"/users/{login}")',
        "a field called user": 'pr.get("user")',
        "a longer segment": 'api(loop, "/user_agent")',
        "a docstring naming it": '"""GET /user answers who the token is."""',
        "a comment naming it": "x = 1  # GET /user",
        "a message naming it": 'say(f"{login}: GET /user failed")',
        "a message ending in it": 'say("GET /user")',
        "a message ending in it and a question mark": 'say("Use GET /user?")',
        "a message ending in it and a colon": 'say("could not read /user:")',
        "a bare message ending in it": 'say("read /user")',
        "an f-string message ending in it": 'say(f"{login} cannot call GET /user")',
        "a message with a blank after the query mark": 'say("GET /user? no")',
        "a message ending in a path of its own": 'say("see /home/user")',
        "a file path of its own": 'home = "/home/user"',
        "a filesystem join": 'os.path.join(home, "user")',
        "a users path whose s is interpolated": "api(loop, f\"/user{'s'}/alice\")",
        "a users path split by a concatenation": 'api(loop, "/user" + "s/alice")',
        "a users path split by a join": 'api(loop, "".join(["/user", "s", "/alice"]))',
        "a users path built from a literal conversion": "api(loop, f\"/{'users'!s}/alice\")",
        "a literal quoted by an ascii conversion": "api(loop, f\"/{'user'!a}\")",
        "a literal quoted by its conversion": "api(loop, f\"/{'user'!r}\")",
        "a number after it": 'api(loop, f"/user{1}")',
        "a spec that pads it": "api(loop, f\"/{'user':_<6}\")",
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

    def test_a_diagnostic_that_names_the_endpoint_is_not_a_finding_in_a_scanned_file(self):
        """The walk the principal test runs: message text ending in ``/user`` passes, a read beside it does not."""
        source = 'say("GET /user")\nsay("Use GET /user?")\nsay(f"{login} cannot call GET /user")\nurl = base + "/user"\n'
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "scripts").mkdir()
            (Path(tmp) / "scripts" / "watchdog.py").write_text(source)
            self.assertEqual(user_calls(Path(tmp)), ['scripts/watchdog.py:4: url = base + "/user"'])


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
