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
import itertools
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

# What the /user scan below reads as a call on the authenticated user: the path itself, with an
# optional sub-path or query after it (``/user/emails``) or a host before it
# (``https://api.github.com/user``); never ``/users/<login>`` or ``/userinfo``.
USER_PATH = re.compile(r"(?:https?://[^/]+)?/user(?:[/?#].*)?\Z", re.DOTALL)
CANDIDATES = 64     # most strings the scan tracks for one expression or name


def _combos(parts):
    """Every way to pick one string from each set in ``parts``: none when a part is unknown (an
    empty set) or the product is large."""
    size = 1
    for part in parts:
        size *= len(part)
    return itertools.product(*parts) if 0 < size <= CANDIDATES else ()


def _filled(template, args, keywords=None, percent=False):
    """``template`` with ``args`` (and ``keywords``) filled in, or ``None`` when they do not fit."""
    try:
        return template % tuple(args) if percent else template.format(*args, **(keywords or {}))
    except (TypeError, ValueError, KeyError, IndexError):
        return None


def _formatted(call, env):
    """``_strings`` of ``"...".format(*args, **keywords)`` over known strings."""
    names = [kw.arg for kw in call.keywords]
    values = call.args + [kw.value for kw in call.keywords]
    done = (_filled(chosen[0], chosen[1:len(call.args) + 1],
                    dict(zip(names, chosen[len(call.args) + 1:])))
            for chosen in _combos([_strings(call.func.value, env)]
                                  + [_strings(value, env) for value in values]))
    return {text for text in done if text is not None}


def _strings(node, env):
    """The strings ``node`` can statically be (an empty set when it cannot be known): a literal
    (adjacent literals are already one ``Constant`` to ``ast``), a name bound to such strings in
    ``env``, and ``+``, an f-string, ``%`` and ``.format`` over them."""
    if isinstance(node, ast.Constant):
        return {node.value} if isinstance(node.value, str) else set()
    if isinstance(node, ast.Name):
        return set(env.get(node.id, ()))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return {left + right for left, right in
                _combos([_strings(node.left, env), _strings(node.right, env)])}
    if isinstance(node, ast.JoinedStr):
        values = [part.value if isinstance(part, ast.FormattedValue) and part.conversion == -1
                  and part.format_spec is None else part for part in node.values]
        return {"".join(chosen) for chosen in _combos([_strings(v, env) for v in values])}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        args = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
        done = (_filled(chosen[0], chosen[1:], percent=True) for chosen in _combos(
            [_strings(node.left, env)] + [_strings(arg, env) for arg in args]))
        return {text for text in done if text is not None}
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format" and all(kw.arg for kw in node.keywords)):
        return _formatted(node, env)
    return set()


def _bindings(tree):
    """``(name, expression)`` for each simple assignment in ``tree``: ``=`` (tuple unpacking
    too), ``:=``, an annotated ``=``, and ``name += value`` (as ``name + value``)."""
    pairs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            targets, value = [node.target], node.value
        elif (isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add)
              and isinstance(node.target, ast.Name)):
            targets, value = [node.target], ast.BinOp(
                left=ast.Name(id=node.target.id), op=ast.Add(), right=node.value)
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                pairs.append((target.id, value))
            elif (isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple)
                  and len(target.elts) == len(value.elts)):
                pairs.extend((t.id, v) for t, v in zip(target.elts, value.elts)
                             if isinstance(t, ast.Name))
    return pairs


def _environment(tree):
    """Name -> every string it is bound to anywhere in ``tree``. One table for the whole file,
    not one per scope: a name reused for another value only adds candidates, never hides one.
    A name that keeps growing (``p += "x"`` in a loop) stops at ``CANDIDATES`` strings."""
    pairs, env, changed = _bindings(tree), {}, True
    while changed:
        changed = False
        for name, value in pairs:
            known = env.setdefault(name, set())
            new = sorted(_strings(value, env) - known)[:max(CANDIDATES - len(known), 0)]
            if new:
                known.update(new)
                changed = True
    return env


def user_paths(source: str) -> list[int]:
    """The line numbers in ``source`` that hold, or statically build, a ``/user`` path: the
    literal, adjacent literals (``"/us" "er"``), a ``+`` / f-string / ``%`` / ``.format`` of
    literals, and a name bound to any of those (``path = "/us"; api(loop, path + "er")``).

    This is a tripwire, not a gate (the broker authorizers check identities themselves). It
    cannot see a path it cannot compute from the file's own literals: one that arrives as an
    argument from another module, is read from data or config, or is built by a function call,
    a loop or a comprehension. A hit is the reviewer's cue; a miss is not proof."""
    tree = ast.parse(source)
    env = _environment(tree)
    return sorted({node.lineno for node in ast.walk(tree) if isinstance(node, ast.expr)
                   and not isinstance(getattr(node, "ctx", None), (ast.Store, ast.Del))
                   and any(USER_PATH.match(text) for text in _strings(node, env))})


def stray_user_paths(root: Path, allowed=USER_CALLERS) -> list[str]:
    """``file:line: source`` for each ``/user`` path under ``root/diaktoros`` outside ``allowed``."""
    found = []
    for path in sorted((root / "diaktoros").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in allowed:
            continue
        text = path.read_text()
        lines = text.splitlines()
        found.extend(f"{rel}:{number}: {lines[number - 1].strip()}" for number in user_paths(text))
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
        self.assertEqual(stray_user_paths(ROOT), [], "resolve identities through gh.principal")


class UserScan(unittest.TestCase):
    """The scan behind ``test_no_identity_check_calls_user_directly`` (#1175): a ``/user`` that
    is not written as one quoted literal on one line still counts."""

    FOUND = (
        'gh.api(loop, "/user", login=login)',
        "gh.api(loop, '/user', login=login)",
        'gh.api(loop, "/us" "er", login=login)',
        'gh.api(loop, ("/us"\n "er"), login=login)',
        'path = "/us"\ngh.api(loop, path + "er", login=login)',
        'path = "/us"\npath += "er"\ngh.api(loop, path, login=login)',
        'a, b = "/us", "er"\ngh.api(loop, a + b, login=login)',
        'path = "/user"\ngh.api(loop, path, login=login)',
        'kind = "user"\ngh.api(loop, f"/{kind}", login=login)',
        'gh.api(loop, f"/{\'user\'}", login=login)',
        'gh.api(loop, "/%s" % "user", login=login)',
        'gh.api(loop, "/{}".format("user"), login=login)',
        'gh.api(loop, "/{k}".format(k="user"), login=login)',
        'gh.api(loop, "/user/emails", login=login)',
        'gh.api(loop, "https://api.github.com/user", login=login)',
    )
    LEFT_ALONE = (
        'gh.api(loop, f"/users/{login}", login=login)',
        'gh.api(loop, "/users/" + login)',
        'gh.api(loop, "/userinfo")',
        'gh.api(loop, "/us" "ers")',
        'role = "user"\nmessages = [{"role": role}]',
        'path = "/us"\nprint(path)',
        '"""GET /user answers with the account."""',
        'gh.api(loop, path, login=login)',
        'gh.api(loop, build() + "er")',
        'gh.api(loop, "/{}".format(1, 2), "%s %s" % ("user",))',
    )

    def test_a_user_path_is_found_however_it_is_written(self):
        for source in self.FOUND:
            with self.subTest(source=source):
                self.assertTrue(user_paths(source), "a /user path went unseen")

    def test_other_paths_and_words_are_left_alone(self):
        for source in self.LEFT_ALONE:
            with self.subTest(source=source):
                self.assertEqual(user_paths(source), [])

    def test_the_line_of_the_use_is_reported(self):
        self.assertEqual(user_paths('x = 1\ny = "/us"\nz = y + "er"\n'), [3])
        self.assertEqual(user_paths('x = 1\ngh.api(loop, "/us"\n "er")\n'), [2])

    def test_a_name_that_keeps_growing_ends_the_scan(self):
        self.assertEqual(user_paths('p = "/x"\nfor _ in range(3):\n    p += "y"\n'), [])
        self.assertEqual(user_paths('p = "/us"\nfor _ in range(3):\n    p += "er"\nuse(p)\n'), [4])

    def test_the_package_scan_reports_an_evader_and_spares_the_seam(self):
        evader = 'diaktoros/evader.py:6: return gh.api(loop, path + "er", login=login)'
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "diaktoros"
            package.mkdir()
            (package / "evader.py").write_text(
                'from . import gh\n\n\ndef who(loop, login):\n'
                '    path = "/us"\n    return gh.api(loop, path + "er", login=login)\n')
            (package / "gh.py").write_text('PROBE = "/user"\n')
            (package / "fine.py").write_text('USERS = "/users/x"\n')
            self.assertEqual(stray_user_paths(Path(raw), allowed={"diaktoros/gh.py"}), [evader])
            self.assertEqual(stray_user_paths(Path(raw), allowed=set()),
                             [evader, 'diaktoros/gh.py:1: PROBE = "/user"'])


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
